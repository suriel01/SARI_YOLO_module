"""SARI Eyes node: wires capture, detection, PTZ tracking, alerts, commands and the stream.

Threads: RTSP reader, MQTT command listener, alert dispatcher, MJPEG server and PTZ senders.
None of them blocks the inference loop; shared state lives in NodeState and FrameBuffer.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import cv2

from . import commands
from .alert_queue import AlertDispatcher, DiskAlertQueue
from .alerts import build_alert_payload, build_tamper_payload, encode_snapshot
from .capture import ThreadedVideoCapture, waiting_frame
from .config import ConfigError, Settings, load_settings
from .detector import PersonDetector, load_model, pick_target
from .intrusion import IntrusionTimer
from .ptz import HikvisionPTZ, ManualPtz, tracking_speed
from .state import NodeState
from .stream import FrameBuffer, start_stream_server
from .tamper import TamperDetector
from .telegram import send_message
from .transport import mqtt as mqtt_transport
from .transport import rest as rest_transport

logger = logging.getLogger("sari_eyes")

CAMERA_ID = "PTZ_1"
PTZ_COOLDOWN_S = 0.04
PREVIEW_WIDTH = 1280


def handle_command(topic: str, raw: bytes, topics: mqtt_transport.Topics, state: NodeState,
                   ptz: HikvisionPTZ, manual: ManualPtz) -> bool:
    """Validate and apply one MQTT command. Returns True when it was applied."""
    try:
        payload = commands.decode(raw)
        if topic == topics.ptz:
            cmd = commands.parse_ptz(payload)
            logger.info("Manual PTZ command: %s", cmd)
            manual.execute(cmd)
            return True
        if topic == topics.tracking:
            changes = {"tracking": commands.parse_tracking(payload)}
        elif topic == topics.config:
            changes = commands.parse_config(payload)
        else:
            return False
    except commands.CommandError as exc:
        logger.warning("Command rejected on %s: %s", topic, exc)
        return False
    except Exception:
        logger.exception("Command on %s failed", topic)
        return False

    if "yolo_confidence" in changes:
        state.set_confidence(changes["yolo_confidence"])
        logger.info("Confidence threshold set to %.2f", changes["yolo_confidence"])
    if "tracking" in changes:
        state.set_tracking(changes["tracking"])
        logger.info("Auto-tracking %s", "enabled" if changes["tracking"] else "paused")
        if not changes["tracking"]:
            ptz.stop()
    return True


def start_stream_safely(settings: Settings, buffer: FrameBuffer):
    """The node keeps detecting and alerting even when the stream port cannot be bound."""
    try:
        return start_stream_server(buffer, token=settings.stream_token, bind=settings.stream_bind,
                                   port=settings.stream_port, placeholder=lambda: waiting_frame("STARTING STREAM..."))
    except OSError as exc:
        logger.error("Stream server could not start on %s:%s (%s); continuing without video",
                     settings.stream_bind, settings.stream_port, exc)
        return None


def build_dispatcher(settings: Settings) -> AlertDispatcher:
    return AlertDispatcher(
        DiskAlertQueue(os.environ.get("ALERT_QUEUE_DIR", "alert_queue")),
        deliver=[
            lambda p: rest_transport.post_alert(settings, p),
            lambda p: mqtt_transport.publish_alert(settings, p),
        ],
        telegram=lambda text: send_message(text, token=settings.telegram_token, chat_id=settings.telegram_chat_id),
        node_id=settings.node_id,
    )


class EyesNode:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = NodeState(tracking=True, confidence=settings.confidence_threshold)
        self.frames = FrameBuffer()
        self.dispatcher = build_dispatcher(settings)
        self.tamper = TamperDetector()
        self.intrusion = IntrusionTimer()
        self.ptz = HikvisionPTZ(settings.camera_ip, settings.camera_user, settings.camera_password, state=self.state)
        self.manual = ManualPtz(self.ptz)
        self.topics = mqtt_transport.Topics(settings.node_id)
        self._moving = False
        self._last_ptz = 0.0

    # --- alerts ---
    def _alert_intrusion(self, duration: float, confidence: float, frame) -> None:
        logger.warning("Intrusion: person present for %.1fs; queueing alert", duration)
        self.dispatcher.submit(build_alert_payload(
            node_id=self.settings.node_id, camera_id=CAMERA_ID, reason="person_over_5s",
            duration=duration, confidence=confidence, snapshot_b64=encode_snapshot(frame)))

    def _alert_tamper(self, reason: str, frame=None) -> None:
        logger.warning("Camera tamper detected: %s", reason)
        self.dispatcher.submit(build_tamper_payload(
            node_id=self.settings.node_id, camera_id=CAMERA_ID, reason=reason,
            snapshot_b64=encode_snapshot(frame) if frame is not None else None))

    # --- tracking ---
    def _stop_if_moving(self) -> None:
        if self._moving:
            self.ptz.stop()
            self._moving = False

    def _track(self, target, cx: int, cy: int, now: float) -> None:
        if target is None or not self.state.tracking_enabled:
            self._stop_if_moving()
            return
        pan, tilt = tracking_speed(*target.center, cx, cy)
        if pan == 0 and tilt == 0:
            self._stop_if_moving()
            return
        if not self._moving or now - self._last_ptz >= PTZ_COOLDOWN_S:
            self.ptz.move(pan, tilt)
            self._last_ptz, self._moving = now, True

    # --- per frame ---
    def process(self, frame, detector: Optional[PersonDetector], now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        reason = self.tamper.analyze(frame, now)
        if reason:
            self._alert_tamper(reason, frame)

        h, w = frame.shape[:2]
        cx, cy = w // 2, h // 2
        annotated = frame.copy()
        cv2.drawMarker(annotated, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 20, 2)

        detections = []
        if detector is not None:
            try:
                detections = detector.detect(frame, self.state.confidence_threshold)
            except Exception:
                logger.exception("Inference failed")
        alerting = self.intrusion.is_alerting(now)
        for d in detections:
            color = (0, 0, 255) if alerting else (0, 255, 0)
            x1, y1, x2, y2 = d.bbox
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, f"Person {int(d.confidence * 100)}%", (x1, max(20, y1 - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        if alerting and detections:
            cv2.putText(annotated, "!!! INTRUSION !!!", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 3)

        target = pick_target(detections, cx, cy)
        self._track(target, cx, cy, now)
        duration = self.intrusion.update(target is not None, now)
        if duration is not None:
            self._alert_intrusion(duration, target.confidence, annotated)

        preview = cv2.resize(annotated, (PREVIEW_WIDTH, int(h * PREVIEW_WIDTH / w))) if w > PREVIEW_WIDTH else annotated
        ok, jpeg = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if ok:
            self.frames.set(jpeg.tobytes())
        return len(detections)

    def run(self) -> None:
        self.frames.set(waiting_frame("CONNECTING TO CAMERA..."))
        mqtt_transport.CommandListener(
            self.settings,
            lambda topic, raw: handle_command(topic, raw, self.topics, self.state, self.ptz, self.manual),
        ).start()
        start_stream_safely(self.settings, self.frames)
        self.dispatcher.start()

        model = load_model(os.environ.get("MODEL_CACHE_DIR", "model_cache"))
        detector = PersonDetector(model) if model is not None else None
        capture = ThreadedVideoCapture(self.settings.rtsp_url).start()

        frames, started, last_count = 0, time.time(), 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    reason = self.tamper.check_stream(time.time(), capture.last_frame_at())
                    if reason:
                        self._alert_tamper(reason)
                    time.sleep(0.005)
                    continue
                last_count = self.process(frame, detector)
                frames += 1
                elapsed = time.time() - started
                if elapsed >= 5.0:
                    logger.info("Processing %.1f FPS | detections %d", frames / elapsed, last_count)
                    frames, started = 0, time.time()
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.ptz.stop()
            capture.stop()
            self.dispatcher.stop()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    logger.info("SARI Eyes node %s starting", settings.node_id)
    EyesNode(settings).run()
