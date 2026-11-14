#!/usr/bin/env python3
"""
Módulo Ojo - Microservicio de Producción Dockerizado (SARI Eye Node).
Captura de video RTSP de alta velocidad (RTSP/TCP), detección YOLO acelerada (TensorRT/CUDA FP16),
seguimiento PTZ Hikvision ultra-fluido (25Hz) y Servidor de Video en Vivo MJPEG (Puerto 8080).

Comunica telemetría y alertas al Módulo Cerebro (SARI Brain Agent) mediante:
  1. MQTT: sari/nodes/<JETSON_NODE_ID>/alerts (fallback) and validated commands on .../ptz|tracking|config
  2. REST API: CEREBRO_HTTP_EVENT_URL (POST /api/alerts/event on the brain, port 7000, X-SARI-Node-Key)
  3. Servidor de Video Web MJPEG autenticado: http://<STREAM_BIND>:<STREAM_PORT>/mjpeg?token=<EYES_STREAM_TOKEN>
"""

import os
import time
import threading
import json
import base64
import cv2
import numpy as np
import requests
from requests.auth import HTTPDigestAuth

# Forzar transporte TCP en FFMPEG/OpenCV para evitar artefactos, desincronización y pantallas negras en Hikvision
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

try:
    import paho.mqtt.client as mqtt
    import paho.mqtt.publish as mqtt_publish
    HAS_MQTT = True
    HAS_MQTT_PUBLISH = True
except ImportError:
    HAS_MQTT = False
    HAS_MQTT_PUBLISH = False

# Importar módulo de alertas de Telegram (fallback / directo)
from sari_eyes.telegram import send_message as enviar_alerta_telegram

try:
    import torch
    PYTORCH_AVAILABLE = True
except ImportError:
    PYTORCH_AVAILABLE = False

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False


# =====================================================================
# CONFIGURACIÓN POR VARIABLES DE ENTORNO
# =====================================================================
from sari_eyes import commands
from sari_eyes.alert_queue import AlertDispatcher, DiskAlertQueue
from sari_eyes.alerts import build_alert_payload, build_tamper_payload, encode_snapshot
from sari_eyes.tamper import TamperDetector
from sari_eyes.config import load_settings, redact_url
from sari_eyes.stream import FrameBuffer, start_stream_server
from sari_eyes.transport import mqtt as mqtt_transport
from sari_eyes.transport import rest as rest_transport

SETTINGS = load_settings()  # fails closed with ConfigError when a required variable is missing
CAMERA_IP = SETTINGS.camera_ip
USERNAME = SETTINGS.camera_user
PASSWORD = SETTINGS.camera_password

CEREBRO_HTTP_EVENT_URL = SETTINGS.cerebro_http_event_url

NODE_ID = SETTINGS.node_id
MQTT_BROKER_HOST = SETTINGS.mqtt_host
MQTT_BROKER_PORT = SETTINGS.mqtt_port
MQTT_USER = SETTINGS.mqtt_user
MQTT_PASS = SETTINGS.mqtt_password
NODE_IP = SETTINGS.node_ip or ""

CONFIDENCE_THRESHOLD = SETTINGS.confidence_threshold
STREAM_PORT = SETTINGS.stream_port
RTSP_URL = SETTINGS.rtsp_url

# Tópicos de control MQTT
TOPICS = mqtt_transport.Topics(NODE_ID)
TOPIC_PTZ = TOPICS.ptz
TOPIC_TRACKING = TOPICS.tracking
TOPIC_CONFIG = TOPICS.config

# Variable de estado de seguimiento (Control Central SARI Cerebro)
human_tracking_active = True
ptz_controlador = None
_manual_timer = None
_manual_lock = threading.Lock()

# Estado Compartido
estado_global = {
    "auto_tracking": True,
    "pan_actual": 0,
    "tilt_actual": 0
}


# Cola persistente de alertas (reintentos con backoff + fallback Telegram)
DISPATCHER = AlertDispatcher(
    DiskAlertQueue(os.environ.get("ALERT_QUEUE_DIR", "alert_queue")),
    deliver=[
        lambda p: rest_transport.post_alert(SETTINGS, p),
        lambda p: mqtt_transport.publish_alert(SETTINGS, p),
    ],
    telegram=lambda text: enviar_alerta_telegram(text, token=SETTINGS.telegram_token, chat_id=SETTINGS.telegram_chat_id),
    node_id=NODE_ID,
)
TAMPER = TamperDetector()

# Buffer para transmisión de video MJPEG (thread-safe)
FRAME_BUFFER = FrameBuffer()


# =====================================================================
# AUXILIAR: FRAME DE ESPERA AUTOMÁTICO (ANTI-PANTALLA NEGRA)
# =====================================================================
def crear_frame_espera(mensaje="CONECTANDO A CAMARA HIKVISION..."):
    """Genera una imagen JPEG sintética cuando la cámara aún no ha entregado frames."""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    
    # Fondo con degradado sutil
    cv2.rectangle(img, (0, 0), (1280, 720), (15, 23, 42), -1)
    
    # Retícula e icono
    cv2.circle(img, (640, 360), 60, (56, 189, 248), 2)
    cv2.line(img, (640, 280), (640, 440), (56, 189, 248), 2)
    cv2.line(img, (560, 360), (720, 360), (56, 189, 248), 2)
    
    # Texto descriptivo
    cv2.putText(img, "SARI MODULO OJOS (JETSON ORIN)", (380, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (56, 189, 248), 2)
    cv2.putText(img, mensaje, (320, 460), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.putText(img, time.strftime("IP Camera RTSP/TCP | %Y-%m-%d %H:%M:%S"), (420, 500), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (148, 163, 184), 1)
    
    ret, buffer = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buffer.tobytes() if ret else b''


# =====================================================================
# AUXILIAR: NOTIFICACIÓN REST AL SARI BRAIN AGENT
# =====================================================================
def notificar_evento_rest(camera_id, reason, duration, confidence=0.85, frame=None):
    """Queues one intrusion alert (single snapshot) for persistent delivery to the brain."""
    payload = build_alert_payload(
        node_id=NODE_ID,
        camera_id=camera_id,
        reason=reason,
        duration=duration,
        confidence=confidence,
        snapshot_b64=encode_snapshot(frame) if frame is not None else None,
    )
    DISPATCHER.submit(payload)


def notificar_tamper(camera_id, reason, frame=None):
    """Queues a camera tamper alert (RTSP loss, black, frozen or occluded frame)."""
    print(f"[TAMPER] {reason} en {camera_id}")
    DISPATCHER.submit(build_tamper_payload(
        node_id=NODE_ID,
        camera_id=camera_id,
        reason=reason,
        snapshot_b64=encode_snapshot(frame) if frame is not None else None,
    ))


# =====================================================================
# CLASE: CAPTURA DE VIDEO MULTIHILO ALTA VELOCIDAD (ANTI-FREEZE & WATCHDOG)
# =====================================================================
class ThreadedVideoCapture:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.cap = None
        self.ret = False
        self.frame = None
        self.running = False
        self.last_frame_time = 0.0
        self.lock = threading.Lock()
        self.thread = None
        self._initialize_capture()

    def _initialize_capture(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        print(f"[VIDEO] Abriendo stream Hikvision RTSP: {redact_url(self.rtsp_url)}...")
        self.cap = cv2.VideoCapture(self.rtsp_url)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def start(self):
        if self.running:
            return self
        self.running = True
        self.thread = threading.Thread(target=self._update, daemon=True, name="RTSPReaderThread")
        self.thread.start()
        return self

    def _update(self):
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                print("[VIDEO] Stream no disponible. Reconectando en 1s...")
                time.sleep(1.0)
                self._initialize_capture()
                continue

            try:
                # Usar grab() + retrieve() para descartar frames acumulados en el buffer
                if not self.cap.grab():
                    time.sleep(0.01)
                    if time.time() - self.last_frame_time > 3.0:
                        print("[WARNING] Watchdog: Stream congelado por >3s. Reiniciando conexión...")
                        self._initialize_capture()
                    continue

                ret, frame = self.cap.retrieve()
                if ret and frame is not None:
                    with self.lock:
                        self.ret = True
                        self.frame = frame
                        self.last_frame_time = time.time()
                else:
                    time.sleep(0.005)
            except Exception as e:
                print(f"[VIDEO EXCEPCIÓN] {e}")
                time.sleep(0.5)

    def last_frame_at(self):
        with self.lock:
            return self.last_frame_time

    def read(self):
        with self.lock:
            # Si el último frame tiene más de 1 segundo de antigüedad, reportar como no disponible
            if time.time() - self.last_frame_time > 1.0:
                return False, None
            return self.ret, self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.cap is not None:
            self.cap.release()
        print("[VIDEO] Hilo de captura detenido.")


# =====================================================================
# CLASE: CONTROL PTZ HIKVISION ULTRA-FLUIDO
# =====================================================================
class HikvisionPTZ:
    def __init__(self, ip, username, password, channel=1, timeout=2.0):
        self.url = f"http://{ip}/ISAPI/PTZCtrl/channels/{channel}/continuous"
        self.auth = HTTPDigestAuth(username, password)
        self.timeout = timeout
        self.last_pan = 0
        self.last_tilt = 0
        self.last_send_time = 0.0

    def mover(self, pan, tilt, zoom=0, force=False):
        if pan == 0 and tilt == 0 and zoom == 0:
            force = True

        now = time.time()
        if not force:
            if (now - self.last_send_time < 0.04) and (pan == self.last_pan and tilt == self.last_tilt):
                return True

        self.last_pan = pan
        self.last_tilt = tilt
        self.last_send_time = now
        
        estado_global["pan_actual"] = pan
        estado_global["tilt_actual"] = tilt

        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <pan>{int(pan)}</pan>
    <tilt>{int(tilt)}</tilt>
    <zoom>{int(zoom)}</zoom>
</PTZData>"""
        
        headers = {"Content-Type": "application/xml"}

        def _enviar():
            try:
                response = requests.put(self.url, data=payload, headers=headers, auth=self.auth, timeout=self.timeout)
                if response.status_code not in [200, 201]:
                    print(f"[PTZ ERROR] HTTP {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"[PTZ EXCEPCIÓN] {e}")

        threading.Thread(target=_enviar, daemon=True).start()
        return True

    def detener(self):
        return self.mover(0, 0, zoom=0, force=True)


PTZ_PULSE_SPEED = 50


def ejecutar_comando_ptz_manual(cmd):
    """Ejecuta un PtzCommand ya validado (sari_eyes.commands.parse_ptz)."""
    global _manual_timer
    if ptz_controlador is None:
        print("[PTZ MANUAL] Controlador PTZ aún no inicializado.")
        return

    with _manual_lock:
        if _manual_timer and _manual_timer.is_alive():
            _manual_timer.cancel()

    if cmd.action in ("stop", "center"):
        ptz_controlador.detener()
        return

    pan, tilt, zoom = cmd.pan, cmd.tilt, cmd.zoom
    if cmd.action is not None:
        pan, tilt, zoom = {
            "up": (0, PTZ_PULSE_SPEED, 0),
            "down": (0, -PTZ_PULSE_SPEED, 0),
            "left": (-PTZ_PULSE_SPEED, 0, 0),
            "right": (PTZ_PULSE_SPEED, 0, 0),
            "zoom_in": (0, 0, PTZ_PULSE_SPEED),
            "zoom_out": (0, 0, -PTZ_PULSE_SPEED),
        }[cmd.action]

    ptz_controlador.mover(pan, tilt, zoom, force=True)

    # Pulso manual: auto-detener tras 0.35s de inactividad
    if pan or tilt or zoom:
        with _manual_lock:
            _manual_timer = threading.Timer(0.35, ptz_controlador.detener)
            _manual_timer.daemon = True
            _manual_timer.start()


# =====================================================================
# CARGA Y COMPILACIÓN DE YOLO26N A TENSORRT (FP16)
# =====================================================================
def cargar_modelo_yolo():
    if not YOLO_AVAILABLE:
        print("[YOLO WARNING] Ultralytics no instalado.")
        return None

    os.makedirs("model_cache", exist_ok=True)
    engine_path = "model_cache/yolo26n.engine"
    pt_path = "model_cache/yolo26n.pt"

    if os.path.exists(engine_path):
        print(f"[YOLO] Cargando motor TensorRT acelerado: {engine_path}")
        try:
            model = YOLO(engine_path, task="detect")
            print("[YOLO] Motor TensorRT cargado con éxito en GPU.")
            return model
        except Exception as e:
            print(f"[YOLO WARNING] Error al cargar .engine: {e}")

    print(f"[YOLO] Cargando modelo PyTorch '{pt_path}'...")
    try:
        model = YOLO(pt_path)
    except Exception:
        print("[YOLO] Descargando modelo base 'yolo26n.pt'...")
        model = YOLO("yolo26n.pt")
        os.rename("yolo26n.pt", pt_path)
        model = YOLO(pt_path)

    if PYTORCH_AVAILABLE and torch.cuda.is_available():
        print("[YOLO] CUDA detectado. Exportando a TensorRT FP16 para máxima fluidez...")
        try:
            model.export(format="engine", half=True, device=0, workspace=4)
            exported_engine = pt_path.replace(".pt", ".engine")
            if os.path.exists(exported_engine):
                print("[YOLO] Motor TensorRT compilado. Recargando...")
                return YOLO(exported_engine, task="detect")
        except Exception as e:
            print(f"[YOLO ERROR] Exportación TensorRT falló: {e}")
        
        print("[YOLO] Forzando modo CUDA Nativo FP16.")
        model.to("cuda")
    else:
        print("[YOLO WARNING] Ejecutando en CPU.")

    return model


# =====================================================================
# HILO DE ESCUCHA MQTT: CONTROL MANUAL PTZ, TRACKING Y CONFIGURACIÓN
# =====================================================================
def iniciar_hilo_mqtt_control():
    """Escucha comandos manuales PTZ, interruptor de seguimiento de humanos y config dinámica."""
    if not HAS_MQTT:
        print("[MQTT CONTROL] Módulo paho-mqtt no disponible. Escucha desactivada.")
        return

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0 or str(rc) == "Success":
            client.subscribe(TOPIC_PTZ, qos=0)
            client.subscribe(TOPIC_TRACKING, qos=1)
            client.subscribe(TOPIC_CONFIG, qos=1)
            print(f"[JETSON] Conectado a SARI Cerebro en {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
            print(f"[JETSON] Suscrito a comandos PTZ y Tracking en {TOPIC_PTZ}")
            print(f"[JETSON] Suscrito a control de tracking en {TOPIC_TRACKING}")
            print(f"[JETSON] Suscrito a config dinámica en {TOPIC_CONFIG}")
        else:
            print(f"[MQTT CONTROL WARNING] Error de conexión MQTT, rc={rc}")

    def on_message(client, userdata, msg):
        global human_tracking_active, CONFIDENCE_THRESHOLD
        try:
            payload = commands.decode(msg.payload)
            if msg.topic == TOPIC_TRACKING:
                cambios = {"tracking": commands.parse_tracking(payload)}
            elif msg.topic == TOPIC_PTZ:
                cmd = commands.parse_ptz(payload)
                print(f"[PTZ] Comando manual: {cmd}")
                ejecutar_comando_ptz_manual(cmd)
                return
            elif msg.topic == TOPIC_CONFIG:
                cambios = commands.parse_config(payload)
            else:
                return
        except commands.CommandError as e:
            print(f"[MQTT CONTROL] Comando rechazado en {msg.topic}: {e}")
            return

        if "yolo_confidence" in cambios:
            CONFIDENCE_THRESHOLD = cambios["yolo_confidence"]
            print(f"[MQTT CONFIG OK] Umbral de confianza actualizado a {round(CONFIDENCE_THRESHOLD * 100, 1)}%")
        if "tracking" in cambios:
            human_tracking_active = cambios["tracking"]
            estado_global["auto_tracking"] = human_tracking_active
            print(f"[TRACKING] Seguimiento de humanos {'ACTIVADO' if human_tracking_active else 'PAUSADO'}")
            if not human_tracking_active and ptz_controlador is not None:
                ptz_controlador.detener()

    def _mqtt_loop():
        while True:
            try:
                client = mqtt_transport.make_client(SETTINGS, "ctrl")
                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=30)
                client.loop_forever()
            except Exception as e:
                print(f"[MQTT CONTROL RECONNECT] Reconectando escucha MQTT en 5s... ({e})")
                time.sleep(5.0)

    threading.Thread(target=_mqtt_loop, daemon=True, name="MQTTControlThread").start()


# =====================================================================
# BUCLE PRINCIPAL DE PROCESAMIENTO (OPENCV + YOLO + TRACKING SUAVE + MJPEG)
# =====================================================================
def main():
    print("\n" + "=" * 60)
    print("  SARI — MÓDULO OJOS (Eye Node v2.0)")
    print("  Visión por Computadora Acelerada + Control PTZ + Stream MJPEG (8080)")
    print("=" * 60 + "\n")
    
    # Inicializar frame de espera inmediatamente para evitar pantallas negras al conectar
    FRAME_BUFFER.set(crear_frame_espera("CONECTANDO A CÁMARA HIKVISION..."))

    # 1. Inicializar PTZ y vincular controlador global para comandos manuales
    global ptz_controlador
    ptz = HikvisionPTZ(ip=CAMERA_IP, username=USERNAME, password=PASSWORD)
    ptz_controlador = ptz

    # 2. Iniciar escucha de comandos MQTT validados (PTZ / Tracking / Config)
    iniciar_hilo_mqtt_control()

    # 3. Iniciar Servidor de Video en Vivo MJPEG (Puerto 8080)
    start_stream_server(
        FRAME_BUFFER,
        token=SETTINGS.stream_token,
        bind=SETTINGS.stream_bind,
        port=SETTINGS.stream_port,
        placeholder=lambda: crear_frame_espera("INICIALIZANDO STREAM EN VIVO..."),
    )

    # 3b. Entrega persistente de alertas
    DISPATCHER.start()

    # 4. Cargar Modelo YOLO y Captura de Video (RTSP/TCP)
    model = cargar_modelo_yolo()
    capture = ThreadedVideoCapture(rtsp_url=RTSP_URL)
    capture.start()

    last_ptz_send_time = 0.0
    ptz_command_cooldown = 0.04
    was_moving = False
    
    tiempo_inicio_deteccion = None
    ultimo_visto = None
    ultimo_envio_alerta = 0.0
    cooldown_alerta = 10.0

    # Variables de métricas de rendimiento (FPS)
    fps_start_time = time.time()
    fps_frame_count = 0
    inference_time_accum = 0.0

    try:
        while True:
            ret, frame = capture.read()
            if not ret or frame is None:
                motivo = TAMPER.check_stream(time.time(), capture.last_frame_at())
                if motivo:
                    notificar_tamper("PTZ_1", motivo)
                time.sleep(0.005)
                continue

            motivo = TAMPER.analyze(frame, time.time())
            if motivo:
                notificar_tamper("PTZ_1", motivo, frame=frame)

            h, w = frame.shape[:2]
            cx, cy = w // 2, h // 2
            
            # Frame anotado para la transmisión web MJPEG
            annotated_frame = frame.copy()
            cv2.drawMarker(annotated_frame, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 20, 2)
            
            detecciones_payload = []
            
            if model is not None:
                device_inference = "cuda:0" if (PYTORCH_AVAILABLE and torch.cuda.is_available()) else "cpu"
                
                t_infer_start = time.time()
                try:
                    results = model(frame, device=device_inference, classes=[0], verbose=False)
                    t_infer_end = time.time()
                    inference_time_accum += (t_infer_end - t_infer_start)

                    best_coords = None
                    min_dist = float('inf')

                    if results[0].boxes is not None and len(results[0].boxes) > 0:
                        now_log_time = time.time()
                        for box in results[0].boxes:
                            conf_val = float(box.conf[0])
                            
                            # Solo procesar y mostrar detecciones >= 70% de confianza
                            if conf_val >= CONFIDENCE_THRESHOLD:
                                xyxy = box.xyxy[0].tolist()
                                x1, y1, x2, y2 = map(int, xyxy)
                                px, py = (x1 + x2) // 2, (y1 + y2) // 2
                                
                                is_alert = (tiempo_inicio_deteccion is not None and (time.time() - tiempo_inicio_deteccion) >= 5.0)
                                box_color = (0, 0, 255) if is_alert else (0, 255, 0)
                                label_text = f"Persona {int(conf_val*100)}%"
                                
                                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, 2)
                                cv2.putText(annotated_frame, label_text, (x1, max(20, y1 - 10)),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, box_color, 2)
                                
                                if is_alert:
                                    cv2.putText(annotated_frame, "!!! INTRUSION DETECTADA !!!", (20, 40),
                                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 3)

                                # Loguear en consola (máximo 1 vez por segundo)
                                if not hasattr(main, "_last_det_log") or (now_log_time - main._last_det_log >= 1.0):
                                    main._last_det_log = now_log_time
                                    print(f"[DETECCIÓN ≥{int(CONFIDENCE_THRESHOLD*100)}%] Persona detectada — Confianza: {round(conf_val * 100, 1)}% | BBox: [{x1}, {y1}, {x2}, {y2}]")
                                
                                detecciones_payload.append({
                                    "clase": "persona",
                                    "confianza": round(conf_val, 2),
                                    "bbox": [x1, y1, x2, y2]
                                })
                                
                                dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                                if dist < min_dist:
                                    min_dist = dist
                                    best_coords = (px, py, conf_val)

                    # ALGORITMO DE SEGUIMIENTO PTZ CONTINUO Y SUAVE
                    if best_coords is not None:
                        if human_tracking_active and estado_global["auto_tracking"]:
                            # Ejecutar seguimiento automático centrado en el bbox de la persona
                            px, py, conf_target = best_coords
                            offset_x, offset_y = px - cx, cy - py
                            norm_x, norm_y = offset_x / cx, offset_y / cy
                            
                            deadzone = 0.08
                            pan_speed, tilt_speed = 0, 0
                            
                            if abs(norm_x) > deadzone:
                                sign_x = 1 if norm_x > 0 else -1
                                norm_dist_x = (abs(norm_x) - deadzone) / (1.0 - deadzone)
                                pan_speed = int(sign_x * (20 + (norm_dist_x ** 1.1) * 80))
                                
                            if abs(norm_y) > deadzone:
                                sign_y = 1 if norm_y > 0 else -1
                                norm_dist_y = (abs(norm_y) - deadzone) / (1.0 - deadzone)
                                tilt_speed = int(sign_y * (20 + (norm_dist_y ** 1.1) * 80))
                                
                            pan_speed = max(min(pan_speed, 100), -100)
                            tilt_speed = max(min(tilt_speed, 100), -100)
                            
                            now_time = time.time()
                            if pan_speed == 0 and tilt_speed == 0:
                                if was_moving:
                                    ptz.detener()
                                    was_moving = False
                            else:
                                if (now_time - last_ptz_send_time >= ptz_command_cooldown) or not was_moving:
                                    ptz.mover(pan_speed, tilt_speed)
                                    last_ptz_send_time = now_time
                                    was_moving = True
                        else:
                            # La cámara mantiene la posición fija o manual seleccionada por el operador
                            if was_moving:
                                ptz.detener()
                                was_moving = False
                                
                    # CONTROL DE ALERTAS DE INTRUSIÓN PROLONGADA (> 5 SEGUNDOS)
                    if best_coords is not None:
                        ultimo_visto = time.time()
                        if tiempo_inicio_deteccion is None:
                            tiempo_inicio_deteccion = time.time()
                        else:
                            tiempo_transcurrido = time.time() - tiempo_inicio_deteccion
                            if tiempo_transcurrido >= 5.0 and (time.time() - ultimo_envio_alerta > cooldown_alerta):
                                dur_round = round(tiempo_transcurrido, 2)
                                print(f"[ALERTA PTZ] Persona detectada durante {dur_round}s. Encolando alerta para el Cerebro...")
                                notificar_evento_rest("PTZ_1", "persona_mas_de_5s", dur_round, confidence=best_coords[2], frame=annotated_frame)
                                ultimo_envio_alerta = time.time()
                                tiempo_inicio_deteccion = None
                                ultimo_visto = None
                    else:
                        # Ventana de paciencia de 2 segundos antes de resetear el timer
                        if ultimo_visto is not None and (time.time() - ultimo_visto > 2.0):
                            tiempo_inicio_deteccion = None
                            ultimo_visto = None
                            
                        if was_moving:
                            ptz.detener()
                            was_moving = False

                except Exception as e:
                    print(f"[YOLO ERROR] {e}")

            # Codificar frame optimizado a 720p para transmisión fluida sin retardo
            try:
                preview = cv2.resize(annotated_frame, (1280, 720)) if (w > 1280) else annotated_frame
                ok, jpeg_buf = cv2.imencode('.jpg', preview, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                if ok:
                    FRAME_BUFFER.set(jpeg_buf.tobytes())
            except Exception as e:
                print(f"[ENCODE ERROR] {e}")

            # MÉTRICAS DE FLUIDEZ (REPORTAR FPS Y LATENCIA CADA 5 SEGUNDOS)
            fps_frame_count += 1
            elapsed_fps = time.time() - fps_start_time
            if elapsed_fps >= 5.0:
                current_fps = round(fps_frame_count / elapsed_fps, 1)
                avg_infer_ms = round((inference_time_accum / max(1, fps_frame_count)) * 1000, 1)
                print(f"[MÉTRICAS FLUIDEZ] Procesamiento: {current_fps} FPS | Latencia Inferencia YOLO: {avg_infer_ms}ms | Detecciones: {len(detecciones_payload)}")
                fps_start_time = time.time()
                fps_frame_count = 0
                inference_time_accum = 0.0

    except KeyboardInterrupt:
        print("[INFO] Interrupción manual recibida.")
    finally:
        ptz.detener()
        capture.stop()
        print("[INFO] Módulo Ojos cerrado correctamente.")

if __name__ == "__main__":
    main()
