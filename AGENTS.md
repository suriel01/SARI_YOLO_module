# SARI (Sistema Autónomo de Respuesta a Intrusiones) — Módulo Ojos

Microservicio autónomo de visión por computadora, detección acelerada YOLO26n (TensorRT/CUDA FP16) y seguimiento PTZ ultra-fluido ejecutándose en NVIDIA Jetson Orin.

## Stack
- **Lenguaje**: Python 3
- **Visión / IA**: OpenCV, Ultralytics YOLO26n, PyTorch CUDA FP16 / TensorRT Engine
- **Comunicación**: WebSockets (`ws`), REST API (`requests`), Docker + NVIDIA Container Toolkit
- **Módulo Cerebro**: Integrado con `SARI_brain_agent_module` (https://github.com/suriel01/SARI_brain_agent_module.git)

## Comandos
- `docker compose up --build -d` — arranca el Módulo Ojos en la Jetson
- `docker compose logs -f` — monitorea métricas de FPS en tiempo real y logs del contenedor
- `docker compose down` — detiene el microservicio de cámara

## Estructura del proyecto
- `camara_ptz.py` — Captura RTSP multihilo, detección YOLO acelerada y seguimiento PTZ Hikvision (25Hz).
- `telegram_alert.py` — Envío directo de emergencias por Telegram (fallback).
- `docker-compose.yml` — Despliegue con acceso a GPU NVIDIA y configuración de red.
- `model_cache/` — Persistencia del motor compilado `yolo26n.engine`.

## Convenciones
- **Estilo**: `snake_case` para variables y funciones.
- **Rendimiento**: El hilo de captura OpenCV y el hilo de control PTZ no deben bloquear el hilo de inferencia YOLO.
- **Comunicación**: Telemetría continua vía WebSockets (`ws://<CEREBRO_HOST>:8765`) y alertas estructuradas de evidencia vía HTTP REST (`POST http://<CEREBRO_HOST>:8000/api/alerts/event`).
- **Fluidez**: Frecuencia de comandos PTZ a 25Hz (cooldown 0.04s) y deadzone ajustada (0.08) para tracking continuo de objetivos a alta velocidad.
