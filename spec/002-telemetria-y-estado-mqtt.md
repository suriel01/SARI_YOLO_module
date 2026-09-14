# SPEC-002: Telemetría Perimetral y Reporte de Estado MQTT

- **ID**: `SPEC-002`
- **Título**: Telemetría de Salud del Nodo Edge y Ciclo de Vida MQTT
- **Estado**: `Implementado`
- **Módulo**: `Módulo Ojos (jetson_telemetry.py / sari-jetson.service)`
- **Última Actualización**: 2026-09-14

---

## 1. Propósito
Garantizar la visibilidad en tiempo real del estado operativo, salud de hardware (RAM, CPU, GPU Tegra, Temperatura) y conectividad del nodo Edge Jetson ante el Módulo Cerebro.

---

## 2. Contrato de Mensajería MQTT

- **Broker**: `192.168.1.71:1883` (o variable `MQTT_BROKER_HOST`)
- **Autenticación**: Usuario `sari_operator`, Clave `sari_secure_password_2026`
- **Client ID**: `jetson_{NODE_ID}`

### 2.1 Mensaje Last Will and Testament (LWT)
- **Tópico**: `sari/nodes/{NODE_ID}/status`
- **QoS**: `1`, **Retain**: `True`
- **Payload**:
  ```json
  {
    "node_id": "Jetson-PTZ_1",
    "status": "offline",
    "reason": "power_or_network_cut"
  }
  ```

### 2.2 Mensaje de Conexión / Nacimiento (Birth)
- **Tópico**: `sari/nodes/{NODE_ID}/status`
- **QoS**: `1`, **Retain**: `True`
- **Disparo**: Inmediatamente tras `on_connect` con `rc == 0`.
- **Payload**:
  ```json
  {
    "node_id": "Jetson-PTZ_1",
    "status": "online",
    "ip": "192.168.1.77",
    "name": "Jetson Orin Nano (PTZ 1)"
  }
  ```

### 2.3 Telemetría Continua de Hardware
- **Tópico**: `sari/nodes/{NODE_ID}/telemetry`
- **QoS**: `0`, **Frecuencia**: Cada 3.0 segundos
- **Payload**:
  ```json
  {
    "node_id": "Jetson-PTZ_1",
    "name": "Jetson Orin Nano (PTZ 1)",
    "ip": "192.168.1.77",
    "ram_used_gb": 3.42,
    "ram_total_gb": 7.42,
    "cpu_load_pct": 18.5,
    "gpu_load_pct": 28.0,
    "temp_c": 46.5,
    "fps": 30.0,
    "link_status": "Wi-Fi 5GHz (Online)"
  }
  ```

---

## 3. Criterios de Aceptación
1. **Persistencia**: El agente debe correr como servicio systemd (`sari-jetson.service`) habilitado en el arranque.
2. **Tolerancia a Fallos**: Si el broker MQTT o el servidor SARI Cerebro se reinicia, el script debe intentar reconectar indefinidamente en bucle `sleep(3)` sin terminar el proceso.
3. **Métricas Reales**: Lectura de temperatura desde `/sys/devices/virtual/thermal/` y GPU desde devfreq/Tegra con fallbacks seguros.
