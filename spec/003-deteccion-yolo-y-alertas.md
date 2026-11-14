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

Todas las rutas exigen el token `EYES_STREAM_TOKEN` (mín. 16 caracteres) en la cabecera `X-SARI-Stream-Token` o en `?token=`, comparado en tiempo constante; sin token → `401`. No se envían cabeceras CORS. Bind configurable con `STREAM_BIND`/`STREAM_PORT`. El `stream_url` registrado en el Cerebro debe incluir `?token=` para que su bot de Telegram pueda pedir `/snapshot`.

### 3.1 Endpoint `/snapshot`
- **Método**: `GET`
- **Respuesta**: Fotograma JPEG actual capturado en el hilo de procesamiento.
- **Cabecera**: `Content-Type: image/jpeg`
- **Uso**: Consumido por el Agente SARI Cerebro bajo demanda o integraciones de telemetría visual.

### 3.2 Endpoint `/mjpeg` (alias `/video_feed`, `/stream`)
- **Método**: `GET`
- **Respuesta**: Stream de video MJPEG (`multipart/x-mixed-replace; boundary=frame`) con bounding boxes y retícula PTZ superpuesta.

---

## 4. Contrato de Alertas MQTT

- **Canal principal**: `POST CEREBRO_HTTP_EVENT_URL` (`/api/alerts/event`, puerto 7000) con cabecera `X-SARI-Node-Key: $SARI_NODE_KEY`. Sin IPs de respaldo codificadas y sin seguir redirecciones.
- **Fallback**: `sari/nodes/{NODE_ID}/alerts`, QoS `1` (solo si REST falla; el Cerebro toma el nodo del tópico).
- **Entrega**: cola persistente en disco con reintentos y fallback Telegram (SPEC-004).
- **Esquema de Payload** (un único snapshot; el Cerebro decide la sirena):
  ```json
  {
    "node_id": "Jetson-PTZ_1",
    "module_name": "Jetson-PTZ_1",
    "camara_id": "PTZ_1",
    "event_type": "intrusion",
    "severity": "high",
    "reason": "persona_mas_de_5s",
    "message": "intrusion (persona_mas_de_5s) for 6.0s at 85% confidence",
    "confidence": 0.85,
    "duration": 6.0,
    "timestamp": 1763000000.0,
    "snapshot": "<JPEG_BASE64>"
  }
  ```
- **Sabotaje**: `event_type: "tamper"` con `reason` = `rtsp_loss` | `black_frame` | `frozen_frame` | `occluded` (máx. una vez por minuto por motivo).

---

## 5. Control Dinámico y Operación Manual desde SARI Cerebro

### 5.1 Interruptor de Seguimiento de Humanos (Tracking Toggle)
- **Tópico de Entrada**: `sari/nodes/{NODE_ID}/tracking`
- **QoS**: `1`
- **Esquema de Payload**:
  ```json
  {
    "enabled": false
  }
  ```
- **Comportamiento**:
  - `enabled: true` — Activa el bucle automático de seguimiento continuo y centrado en la persona detectada por YOLO.
  - `enabled: false` — Pausa inmediatamente el seguimiento PTZ y detiene los motores. La cámara mantiene la posición fija actual o la seleccionada manualmente por el operador táctico, manteniendo la inferencia YOLO y streaming activos.

### 5.2 Comandos de Movimiento Manual PTZ (D-Pad / Click & Drag / Zoom)
- **Tópico de Entrada**: `sari/nodes/{NODE_ID}/ptz`
- **QoS**: `0`
- **Esquemas Aceptados**:
  - **D-Pad / Acciones Fijas**:
    ```json
    {
      "action": "up" | "down" | "left" | "right" | "center" | "stop" | "zoom_in" | "zoom_out"
    }
    ```
  - **Modo Proporcional / Click & Drag / Joystick**:
    ```json
    {
      "action": "drag",
      "pan_delta": 45,
      "tilt_delta": -20,
      "zoom_delta": 0
    }
    ```
- **Comportamiento**:
  - Los pulsos de D-Pad o deltas aplican movimiento con velocidad ajustada y auto-detención tras ventana de 0.35s de inactividad para evitar desbordes de ángulo.
  - `stop` detiene inmediatamente cualquier movimiento en curso.
  - Validación: acción desconocida, deltas no numéricos o fuera de ±1000 rechazan el mensaje completo; los deltas se recortan a la velocidad de la cámara (±100).

### 5.3 Configuración en Vivo de Parámetros
- **Tópico de Entrada**: `sari/nodes/{NODE_ID}/config`
- **QoS**: `1`
- **Esquema Aceptado**:
  ```json
  {
    "ptz_tracking": false,
    "confidence_threshold": 0.65
  }
  ```
- **Acción**: Permite ajustar en caliente el umbral de confianza YOLO (`confidence_threshold` o `yolo_confidence`) o pausar tracking sin reiniciar el contenedor.
- **Validación**: el umbral debe estar en `0.25–0.9`; los flags de tracking deben ser booleanos reales. Un campo inválido rechaza el mensaje completo.
