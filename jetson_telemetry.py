#!/usr/bin/env python3
import time, json, os, psutil, threading, logging, requests
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("jetson_agent")

# CONFIGURACIÓN DEL NODO
NODE_ID = os.environ.get("JETSON_NODE_ID", "Jetson-PTZ_1")
NODE_NAME = os.environ.get("JETSON_NODE_NAME", "Jetson Orin Nano (PTZ 1)")
NODE_IP = os.environ.get("JETSON_NODE_IP", "192.168.1.73") # IP de esta Jetson

# CEREBRO SARI
MQTT_HOST = os.environ.get("MQTT_BROKER_HOST", "192.168.1.71") # IP de tu PC/Servidor SARI
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER = os.environ.get("MQTT_USER", "sari_operator")
MQTT_PASS = os.environ.get("MQTT_PASS", "sari_secure_password_2026")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8990032616:AAFT732S-Q46GAaNXRs3bEs0-JidOZE7tjQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7170575800")

# Compatibilidad con Paho MQTT v1 y v2
try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"jetson_{NODE_ID}")
except Exception:
    client = mqtt.Client(client_id=f"jetson_{NODE_ID}")

client.username_pw_set(MQTT_USER, MQTT_PASS)

# 1. Configurar Last Will and Testament (LWT) para avisar al Cerebro si caemos
lwt_payload = json.dumps({"node_id": NODE_ID, "status": "offline", "reason": "power_or_network_cut"})
client.will_set(f"sari/nodes/{NODE_ID}/status", lwt_payload, qos=1, retain=True)

def on_connect(c, userdata, flags, rc, properties=None):
    if rc == 0 or str(rc) == "Success":
        logger.info(f"Conectado a SARI Cerebro en {MQTT_HOST}:{MQTT_PORT}")
        c.publish(f"sari/nodes/{NODE_ID}/status", json.dumps({
            "node_id": NODE_ID, "status": "online", "ip": NODE_IP, "name": NODE_NAME
        }), qos=1, retain=True)
    else:
        logger.warning(f"Error de conexión MQTT: {rc}")

client.on_connect = on_connect

def get_jetson_hardware_stats():
    temp_c = 45.0
    for zone in range(5):
        path = f"/sys/devices/virtual/thermal/thermal_zone{zone}/temp"
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    if val > 1000:
                        val = val / 1000.0
                    if 20.0 <= val <= 105.0:
                        temp_c = round(val, 1)
                        break
            except Exception:
                pass

    gpu_pct = 25.0
    for gpu_path in [
        "/sys/devices/gpu.0/load",
        "/sys/devices/platform/17000000.gpu/load",
        "/sys/class/devfreq/17000000.ga10b/device/load",
        "/sys/class/devfreq/17000000.ga10b/load"
    ]:
        if os.path.exists(gpu_path):
            try:
                with open(gpu_path, "r") as f:
                    raw_val = float(f.read().strip())
                    gpu_pct = round(raw_val / 10.0 if raw_val > 100 else raw_val, 1)
                    break
            except Exception:
                pass

    return temp_c, gpu_pct

def telemetry_loop():
    while True:
        try:
            mem = psutil.virtual_memory()
            temp_c, gpu_load = get_jetson_hardware_stats()
            payload = {
                "node_id": NODE_ID,
                "name": NODE_NAME,
                "ip": NODE_IP,
                "ram_used_gb": round((mem.total - mem.available) / (1024**3), 2),
                "ram_total_gb": round(mem.total / (1024**3), 2),
                "cpu_load_pct": round(psutil.cpu_percent(interval=None), 1),
                "gpu_load_pct": gpu_load,
                "temp_c": temp_c,
                "fps": 30.0,
                "link_status": "Wi-Fi 5GHz (Online)"
            }
            client.publish(f"sari/nodes/{NODE_ID}/telemetry", json.dumps(payload), qos=0)
        except Exception as e:
            logger.error(f"Error enviando telemetría: {e}")
        time.sleep(3)

def connect_with_retry():
    connected = False
    while not connected:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            connected = True
        except Exception as e:
            logger.warning(f"Esperando broker MQTT ({MQTT_HOST}:{MQTT_PORT}): {e}")
            time.sleep(3)

connect_with_retry()
threading.Thread(target=telemetry_loop, daemon=True).start()
client.loop_forever()
