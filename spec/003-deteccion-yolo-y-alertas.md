# SPEC-003: Detección YOLO26n, PTZ y Alertas con Snapshot

- **ID**: `SPEC-003`
- **Título**: Percepción YOLO Edge, Tracking PTZ, Servidor de Snapshots y Emisión de Alertas MQTT
- **Estado**: `Implementado`
- **Módulo**: `Módulo Ojos (camara_ptz.py / Docker container modulo_ojo)`
- **Última Actualización**: 2026-09-14

---

## 1. Propósito
Especificar el flujo de inferencia acelerada por GPU, el seguimiento PTZ Hikvision, la exposición HTTP de evidencia visual en vivo y el esquema unificado de alertas MQTT hacia el Módulo Cerebro.

---

## 2. Inferencia y Tracking PTZ
- **Modelo**: Ultralytics YOLO26n compilado a motor TensorRT FP16 (`model_cache/yolo26n.engine`).
- **Clase Objetivo**: `person` (ID 0).
- **Frecuencia Inferencia**: ~25-35 FPS en NVIDIA Jetson Orin Nano.
- **Frecuencia Comandos PTZ**: 25 Hz (`cooldown = 0.04s`), `deadzone = 0.08` horizontal/vertical.
- **Persistencia de Detección**: Requiere detección sostenida de al menos 5.0 segundos continuos para calificar como evento de intrusión (evitando falsos positivos transitorios).

---

## 3. Interfaces HTTP Locales (Puerto 8080)

### 3.1 Endpoint `/snapshot`
- **Método**: `GET`
- **Respuesta**: Fotograma JPEG actual capturado en el hilo de procesamiento.
- **Cabecera**: `Content-Type: image/jpeg`
- **Uso**: Consumido por el Agente SARI Cerebro bajo demanda o integraciones de telemetría visual.

### 3.2 Endpoint `/video_feed`
- **Método**: `GET`
- **Respuesta**: Stream de video MJPEG (`multipart/x-mixed-replace; boundary=frame`) con bounding boxes y retícula PTZ superpuesta.

---

## 4. Contrato de Alertas MQTT

- **Tópico**: `sari/alerts`
- **QoS**: `1`
- **Esquema de Payload**:
  ```json
  {
    "node_id": "Jetson-PTZ_1",
    "event_type": "intrusion",
    "severity": "high",
    "message": "Intrusión verificada en perímetro",
    "confidence": 0.85,
    "snapshot": "<IMAGEN_JPEG_CODIFICADA_EN_BASE64>"
  }
  ```

---

## 5. Control Dinámico desde el Cerebro

- **Tópico de Entrada**: `sari/nodes/{NODE_ID}/config`
- **Esquema Aceptado**:
  ```json
  {
    "ptz_tracking": false,
    "confidence_threshold": 0.65
  }
  ```
- **Acción**: Si `ptz_tracking` es `false`, la Jetson frena los comandos continuos de movimiento PTZ, permitiendo que el Módulo Cerebro mantenga la vista fija en la zona de intrusión.
