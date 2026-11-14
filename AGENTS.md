# SARI (Sistema Autónomo de Respuesta a Intrusiones) — Módulo Ojos

Microservicio autónomo de visión por computadora, detección acelerada YOLO26n (TensorRT/CUDA FP16) y seguimiento PTZ ultra-fluido ejecutándose en NVIDIA Jetson Orin.

## Stack
- **Lenguaje**: Python 3
- **Visión / IA**: OpenCV, Ultralytics YOLO26n, PyTorch CUDA FP16 / TensorRT Engine
- **Comunicación**: REST (`requests`, cabecera `X-SARI-Node-Key`), MQTT (`paho-mqtt`, tópicos por nodo), Docker + NVIDIA Container Toolkit
- **Módulo Cerebro**: Integrado con `SARI_brain_agent_module` (https://github.com/suriel01/SARI_brain_agent_module.git)

## Comandos
- `docker compose up --build -d` — arranca el Módulo Ojos en la Jetson
- `docker compose logs -f` — monitorea métricas de FPS en tiempo real y logs del contenedor
- `docker compose down` — detiene el microservicio de cámara
- `.venv/bin/pytest -q` — ejecuta las pruebas (ver `requirements-dev.txt`)

## Estructura del proyecto
- `camara_ptz.py` — Punto de entrada del contenedor; toda la lógica vive en el paquete `sari_eyes/` (captura, detector, ptz, stream, tamper, cola de alertas, transportes).
- `jetson_telemetry.py` — Agente de telemetría del host (`deploy/sari-jetson.service`).
- `telegram_alert.py` — CLI de prueba de Telegram; el fallback automático está en `sari_eyes/alert_queue.py`.
- `tests/` — Pruebas `pytest -q` (cámara, YOLO y red simulados).
- `docker-compose.yml` — Despliegue con acceso a GPU NVIDIA y configuración de red.
- `model_cache/` — Persistencia del motor compilado `yolo26n.engine`.

## Convenciones
- **Estilo**: `snake_case` para variables y funciones.
- **Rendimiento**: El hilo de captura OpenCV y el hilo de control PTZ no deben bloquear el hilo de inferencia YOLO.
- **Comunicación**: alertas por `POST CEREBRO_HTTP_EVENT_URL` (puerto 7000, `X-SARI-Node-Key`) con fallback MQTT `sari/nodes/<id>/alerts`; telemetría/estado en `sari/nodes/<id>/telemetry|status`. No hay WebSocket.
- **Seguridad**: secretos solo en `.env`/`/etc/sari/jetson.env` (nunca en código ni git); `MQTT_USER` = `JETSON_NODE_ID`; el stream exige `EYES_STREAM_TOKEN`; el nodo nunca decide la sirena.
- **Fluidez**: Frecuencia de comandos PTZ a 25Hz (cooldown 0.04s) y deadzone ajustada (0.08) para tracking continuo de objetivos a alta velocidad.
