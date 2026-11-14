# Feature: eyes-hardening

Objective: harden the SARI Eyes module (Jetson vision node) and align it with the hardened brain contract (suriel01/SARI_brain_agent_module#1).
Branch: `chore/eyes-hardening` (off `main`, never pushed by the agent). Delivery: one commit per task on the branch; push and PR are the owner's decisions.
Test runner: `pytest -q` from `.venv` (pytest, numpy, opencv-python-headless, requests, paho-mqtt). Camera, YOLO and network are mocked.

## Specs

- **S1 Secrets.** "remove every hardcoded secret from code/compose; read from env with no defaults (fail closed with a clear error); `git rm --cached .env`, add `.env` to `.gitignore`, add `.env.example` with placeholder values only; fix env name mismatches (`MQTT_PASS` vs `MQTT_PASSWORD`, `MQTT_PORT` vs `MQTT_BROKER_PORT`)." Canonical names: `MQTT_PASSWORD`, `MQTT_BROKER_PORT`. The RTSP URL (it embeds the camera password) is never logged.
- **S2 Brain contract.** "REST alerts to `POST /api/alerts/event` must send header `X-SARI-Node-Key: $SARI_NODE_KEY`; remove the hardcoded fallback IPs (192.168.55.100, 192.168.1.71) and port 8000 (brain API is port 7000; configure one `CEREBRO_HTTP_EVENT_URL`). MQTT alerts must publish to `sari/nodes/<NODE_ID>/alerts` (QoS 1) instead of `sari/alerts`; the MQTT username must equal the node id (broker ACL pattern `sari/nodes/%u/...`), e.g. `Jetson-PTZ_1`. Telemetry/status stay on `sari/nodes/<NODE_ID>/telemetry|status` with `node_id` equal to the topic node. Send ONE snapshot (not four copies), and remove `auto_siren: true` (the brain decides; spec 001). Optional MQTT TLS: support `MQTT_TLS_CA` path → port 8883." Startup fails closed when `MQTT_USER` differs from `JETSON_NODE_ID`.
- **S3 Video.** "MJPEG/snapshot endpoints require auth (token header or query `?token=` from env `EYES_STREAM_TOKEN`, constant-time compare), drop `Access-Control-Allow-Origin: *`, keep bind configurable. Remove the duplicate Flask/native server implementation (keep one)." Kept: the native `http.server` implementation (no Flask dependency). Header: `X-SARI-Stream-Token`. Bind: `STREAM_BIND` (default `0.0.0.0`), `STREAM_PORT` (default `8080`).
- **S4 Resilience.** "persistent on-disk alert queue with retry/backoff when the brain is unreachable (no silent drop); automatic Telegram fallback alert when the brain stays unreachable; tamper detection (RTSP loss, black/frozen/occluded frame) publishing a tamper alert." An alert counts as delivered when REST (2xx) or MQTT (QoS 1 publish acknowledged) succeeds. Tamper alerts use `event_type: "tamper"`.
- **S5 Commands.** "PTZ/tracking/config over MQTT must validate ranges and reject `yolo_confidence` outside a safe band (e.g. 0.25–0.9); WebSocket commands from the brain must be authenticated or removed." Decision: the brain has no WebSocket server (verified: no `8765` listener in `SARI_brain_agent/backend`), so the WebSocket client and its commands are removed.
- **S6 Structure.** "split `camara_ptz.py` into modules (capture, detector, ptz, stream, transport/mqtt, transport/rest, alerts queue) with unit tests that mock the camera, YOLO and network; replace prints with logging; add locks around shared globals; pinned `requirements.txt`; Dockerfile pinned base tag and non-root user; bring a single unified `jetson_telemetry.py` (the brain repo's version at `~/Desktop/SARI_brain_agent/jetson_telemetry.py` has the HTTP fallback with `X-SARI-Node-Key` and a systemd unit `sari-jetson.service` using `EnvironmentFile=/etc/sari/jetson.env`) — merge into this repo. Add `CLAUDE.md` for this repo (stack, commands, security rules) and update `AGENTS.md`/README/specs that are stale."

## Tasks

The split (S6) is done progressively: each task moves the code it touches into the `sari_eyes` package with tests; T6 finishes the split and the packaging/docs.

| ID | Specs | Route | Status | Commit |
|----|-------|-------|--------|--------|
| T1 | S1 | inline | pending | |
| T2 | S2 | inline | pending | |
| T3 | S3 | inline | pending | |
| T4 | S4 | inline | pending | |
| T5 | S5 | inline | pending | |
| T6 | S6 | inline | pending | |

Route evidence: one session, sequential dependent units on shared files (`camara_ptz.py`, package wiring), so no Writer trigger applies.

## Log

- **L1 (2026-11-13, brief from the parent session, verbatim):**

  Harden the SARI "Eyes" module in this repo (`~/Desktop/SARI_YOLO_module`, runs on an NVIDIA Jetson with YOLO, PTZ camera, MJPEG) and align it with the already-hardened brain contract. Work on a new branch off `main`; never push, never force-push, never rewrite git history (history purge and secret rotation are the owner's manual actions). Never read or print real secret values; never put secrets in tracked files. Follow ODD: create `odd/tasks/eyes-hardening.md` (Specs S#, Tasks, Log with this brief verbatim as L1) before the first source write, test-first (RED then GREEN) with pytest, Conventional Commits without AI attribution, one commit per task.

  Current state (from an audit; verify before acting): all Python, `camara_ptz.py` (998 lines: capture, YOLO, PTZ via Hikvision ISAPI, MJPEG/Flask + native HTTP server on 0.0.0.0:8080 with `Access-Control-Allow-Origin: *`, WebSocket to brain `ws://host:8765`, MQTT, REST alerts), `jetson_telemetry.py`, `telegram_alert.py`, Dockerfile (unpinned `ultralytics/ultralytics:latest-jetson-jetpack6`, unpinned pip, no requirements.txt, runs as root), docker-compose.yml, committed `.env` with live Telegram token, Hikvision admin password and MQTT password (also hardcoded in `camara_ptz.py:64,77`, `telegram_alert.py:16-17`, `jetson_telemetry.py:17-20`, compose). No tests at all.

  Tasks:
  1. Secrets: remove every hardcoded secret from code/compose; read from env with no defaults (fail closed with a clear error); `git rm --cached .env`, add `.env` to `.gitignore`, add `.env.example` with placeholder values only; fix env name mismatches (`MQTT_PASS` vs `MQTT_PASSWORD`, `MQTT_PORT` vs `MQTT_BROKER_PORT`).
  2. Brain contract: REST alerts to `POST /api/alerts/event` must send header `X-SARI-Node-Key: $SARI_NODE_KEY`; remove the hardcoded fallback IPs (192.168.55.100, 192.168.1.71) and port 8000 (brain API is port 7000; configure one `CEREBRO_HTTP_EVENT_URL`). MQTT alerts must publish to `sari/nodes/<NODE_ID>/alerts` (QoS 1) instead of `sari/alerts`; the MQTT username must equal the node id (broker ACL pattern `sari/nodes/%u/...`), e.g. `Jetson-PTZ_1`. Telemetry/status stay on `sari/nodes/<NODE_ID>/telemetry|status` with `node_id` equal to the topic node. Send ONE snapshot (not four copies), and remove `auto_siren: true` (the brain decides; spec 001). Optional MQTT TLS: support `MQTT_TLS_CA` path → port 8883.
  3. Video: MJPEG/snapshot endpoints require auth (token header or query `?token=` from env `EYES_STREAM_TOKEN`, constant-time compare), drop `Access-Control-Allow-Origin: *`, keep bind configurable. Remove the duplicate Flask/native server implementation (keep one).
  4. Resilience: persistent on-disk alert queue with retry/backoff when the brain is unreachable (no silent drop); automatic Telegram fallback alert when the brain stays unreachable; tamper detection (RTSP loss, black/frozen/occluded frame) publishing a tamper alert.
  5. Commands: PTZ/tracking/config over MQTT must validate ranges and reject `yolo_confidence` outside a safe band (e.g. 0.25–0.9); WebSocket commands from the brain must be authenticated or removed.
  6. Structure: split `camara_ptz.py` into modules (capture, detector, ptz, stream, transport/mqtt, transport/rest, alerts queue) with unit tests that mock the camera, YOLO and network; replace prints with logging; add locks around shared globals; pinned `requirements.txt`; Dockerfile pinned base tag and non-root user; bring a single unified `jetson_telemetry.py` (the brain repo's version at `~/Desktop/SARI_brain_agent/jetson_telemetry.py` has the HTTP fallback with `X-SARI-Node-Key` and a systemd unit `sari-jetson.service` using `EnvironmentFile=/etc/sari/jetson.env`) — merge into this repo. Add `CLAUDE.md` for this repo (stack, commands, security rules) and update `AGENTS.md`/README/specs that are stale.
  Done = all tasks committed on the branch with tests passing (`pytest -q`), a short report of owner actions (rotate Telegram token, Hikvision admin password, MQTT password; create MQTT user `Jetson-PTZ_1` in the brain broker; set `SARI_NODE_KEY` from the brain's `SARI_NODE_KEYS`; purge history or make repo private), and what could not be verified without the Jetson hardware.

- **L2 (2026-11-13, exploration):** brain verified on branch `chore/claude-odd-reorg`: `require_node` maps `X-SARI-Node-Key` to a node id (401 otherwise); `/api/telemetry/node` rejects a payload `node_id` that differs from the key's node (403); broker ACL grants `pattern write sari/nodes/%u/{telemetry,status,alerts}` and `pattern read sari/nodes/%u/{ptz,tracking,config}`; no WebSocket server exists in the brain; the Telegram bot fetches `<stream_url with /mjpeg→/snapshot>` from the node, so the brain's `stream_url` must carry `?token=` once S3 lands (owner action). The current code also logs the RTSP URL including the camera password (fixed under S1).
