# CLAUDE.md — SARI Eyes module

Vision node of SARI on an NVIDIA Jetson Orin: RTSP capture, YOLO26n person detection (TensorRT FP16),
Hikvision PTZ tracking, authenticated MJPEG stream, alerts to the SARI Brain
(`~/Desktop/SARI_brain_agent`, FastAPI on port 7000 + Mosquitto). Read `AGENTS.md` and `spec/` first.

## Stack
- Python 3 (`sari_eyes/` package), OpenCV, Ultralytics YOLO, paho-mqtt, requests, psutil.
- Docker image pinned by tag and digest (`Dockerfile`), runs as uid 10001; runtime deps in `requirements.txt`.
- Host telemetry agent via systemd (`deploy/sari-jetson.service`, env in `/etc/sari/jetson.env`).

## Commands
- Tests: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q`
- Jetson: `docker compose up --build -d`, `docker compose logs -f`, `docker compose down`

## Security rules (non-negotiable)
- Never read, print or commit real secrets. `.env` and `/etc/sari/jetson.env` are local only;
  templates (`.env.example`, `deploy/jetson.env.example`) hold `<placeholders>`. Tests guard this.
- Required variables have no defaults: missing config must fail closed (`ConfigError`).
- Brain contract: REST alerts to `CEREBRO_HTTP_EVENT_URL` with `X-SARI-Node-Key`; MQTT only on
  `sari/nodes/<JETSON_NODE_ID>/...` with `MQTT_USER == JETSON_NODE_ID`; never `sari/alerts`.
- The node reports evidence (one snapshot per alert); the brain decides the siren. No `auto_siren`.
- Every stream route requires `EYES_STREAM_TOKEN` (constant-time compare); no wildcard CORS.
- Commands from MQTT go through `sari_eyes/commands.py`; keep the 0.25–0.9 confidence band.
- Never log URLs that embed credentials (use `config.redact_url`) or the Telegram API URL.

## Conventions
- Capture, PTZ sending, MQTT and alert delivery run on their own threads and must never block inference.
- Shared state goes through `NodeState` / `FrameBuffer` (locked); no new module-level mutable globals.
- `logging`, not `print`, inside `sari_eyes/`. Code, comments and new docs in English; `spec/` is Spanish.
- Conventional Commits; test-first with `pytest` (mock camera, YOLO and network).
