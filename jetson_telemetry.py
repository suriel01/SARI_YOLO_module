#!/usr/bin/env python3
"""
SARI (Sistema Autónomo de Respuesta a Intrusiones) — Telemetría del Módulo Ojos (Jetson Orin)
Envía periódicamente métricas de salud de hardware y estado de enlace al Módulo Cerebro.
"""

import time
import os
import psutil
import requests
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Telemetry) %(message)s"
)

# Configuración de URLs
URL_PRIMARY = "http://192.168.55.100:8000/api/telemetry/node"  # USB Direct
URL_BACKUP = "http://192.168.1.71:8000/api/telemetry/node"     # Wi-Fi Fallback
INTERVAL_SECONDS = 3.0

NODE_ID = "Jetson-PTZ_1"
NODE_NAME = "Jetson Orin Nano (PTZ 1)"
NODE_IP = "192.168.55.1"


def get_gpu_load() -> float:
    """Lee la carga de GPU Tegra en porcentaje."""
    gpu_paths = [
        "/sys/devices/gpu.0/load",
        "/sys/devices/platform/17000000.gpu/load",
        "/sys/class/devfreq/17000000.ga10b/device/load"
    ]
    for p in gpu_paths:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    val = float(f.read().strip())
                    # Si el valor viene en base 1000 (0 a 1000), dividimos entre 10
                    return round(val / 10.0 if val > 100 else val, 1)
            except Exception:
                pass
    return 0.0


def get_temperature() -> float:
    """Lee la temperatura del sensor térmico principal en grados Celsius."""
    temp_paths = [
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone0/temp"
    ]
    for p in temp_paths:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    val = float(f.read().strip())
                    # Si viene en miligrados (> 1000), dividimos entre 1000
                    return round(val / 1000.0 if val > 1000 else val, 1)
            except Exception:
                pass
    return 0.0


def collect_metrics() -> dict:
    """Recolecta el estado del sistema Jetson."""
    vm = psutil.virtual_memory()
    ram_used_gb = round(vm.used / (1024 ** 3), 2)
    ram_total_gb = round(vm.total / (1024 ** 3), 2)
    cpu_load_pct = round(psutil.cpu_percent(interval=None), 1)
    gpu_load_pct = get_gpu_load()
    temp_c = get_temperature()

    return {
        "node_id": NODE_ID,
        "name": NODE_NAME,
        "ip": NODE_IP,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "cpu_load_pct": cpu_load_pct,
        "gpu_load_pct": gpu_load_pct,
        "temp_c": temp_c,
        "fps": 30.0,
        "link_status": "USB Direct (1Gbps)"
    }


def send_telemetry(payload: dict):
    """Envía la telemetría probando primero la ruta USB y luego Wi-Fi de respaldo."""
    headers = {"Content-Type": "application/json"}
    
    # 1. Intentar primario (USB Direct)
    try:
        resp = requests.post(URL_PRIMARY, json=payload, headers=headers, timeout=2.0)
        if resp.status_code == 200:
            logging.info(f"Enviado (USB Primario) -> CPU: {payload['cpu_load_pct']}% | GPU: {payload['gpu_load_pct']}% | RAM: {payload['ram_used_gb']}/{payload['ram_total_gb']}GB | Temp: {payload['temp_c']}°C")
            return
        else:
            logging.warning(f"Primario retornó código HTTP {resp.status_code}")
    except Exception as e:
        logging.debug(f"Primario inalcanzable ({e}), intentando respaldo...")

    # 2. Intentar respaldo (Wi-Fi)
    try:
        resp = requests.post(URL_BACKUP, json=payload, headers=headers, timeout=2.0)
        if resp.status_code == 200:
            logging.info(f"Enviado (Wi-Fi Respaldo) -> CPU: {payload['cpu_load_pct']}% | GPU: {payload['gpu_load_pct']}% | RAM: {payload['ram_used_gb']}/{payload['ram_total_gb']}GB | Temp: {payload['temp_c']}°C")
            return
        else:
            logging.warning(f"Respaldo retornó código HTTP {resp.status_code}")
    except Exception as e:
        logging.warning(f"Error enviando telemetría a ambos destinos (Primario y Respaldo): {e}")


def main():
    logging.info("Iniciando servicio de telemetría hacia Módulo Cerebro...")
    # Inicializar psutil cpu_percent
    psutil.cpu_percent(interval=None)
    
    while True:
        try:
            payload = collect_metrics()
            send_telemetry(payload)
        except Exception as e:
            logging.error(f"Excepción en ciclo de telemetría: {e}")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
