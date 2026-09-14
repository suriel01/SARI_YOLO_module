#!/usr/bin/env python3
"""
Módulo Ojo - Microservicio de Producción Dockerizado (SARI Eye Node).
Captura de video RTSP de alta velocidad (RTSP/TCP), detección YOLO acelerada (TensorRT/CUDA FP16),
seguimiento PTZ Hikvision ultra-fluido (25Hz) y Servidor de Video en Vivo MJPEG (Puerto 8080).

Comunica telemetría y alertas al Módulo Cerebro (SARI Brain Agent) mediante:
  1. WebSockets: ws://<CEREBRO_HOST>:8765
  2. REST API: http://<CEREBRO_HOST>:8000/api/alerts/event
  3. Servidor de Video Web MJPEG: http://0.0.0.0:8080/video_feed (o /mjpeg)
"""

import os
import time
import threading
import json
import asyncio
import queue
import base64
import cv2
import numpy as np
import requests
import websockets
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
from telegram_alert import enviar_alerta_telegram

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

try:
    from flask import Flask, Response
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

# =====================================================================
# CONFIGURACIÓN POR VARIABLES DE ENTORNO
# =====================================================================
CAMERA_IP = os.environ.get("CAMERA_IP", "192.168.1.200")
USERNAME = os.environ.get("CAMERA_USER", "admin")
PASSWORD = os.environ.get("CAMERA_PASS", "Asenso117925")

CEREBRO_HOST = os.environ.get("CEREBRO_HOST", "192.168.1.100")
CEREBRO_PORT_WS = os.environ.get("CEREBRO_PORT_WS", "8765")
CEREBRO_PORT_HTTP = os.environ.get("CEREBRO_PORT_HTTP", "8000")

CEREBRO_URL = os.environ.get("CEREBRO_URL", f"ws://{CEREBRO_HOST}:{CEREBRO_PORT_WS}")
CEREBRO_HTTP_EVENT_URL = os.environ.get("CEREBRO_HTTP_EVENT_URL", f"http://{CEREBRO_HOST}:{CEREBRO_PORT_HTTP}/api/alerts/event")

NODE_ID = os.environ.get("JETSON_NODE_ID", "Jetson-PTZ_1")
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "192.168.1.71")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", 1883))
MQTT_USER = os.environ.get("MQTT_USER", "sari_operator")
MQTT_PASS = os.environ.get("MQTT_PASSWORD", "sari_secure_password_2026")
NODE_IP = os.environ.get("JETSON_NODE_IP", "192.168.1.77")

CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.70"))
STREAM_PORT = int(os.environ.get("STREAM_PORT", "8080"))
RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@{CAMERA_IP}:554/Streaming/Channels/101"

# Tópicos de control MQTT
TOPIC_PTZ = f"sari/nodes/{NODE_ID}/ptz"
TOPIC_TRACKING = f"sari/nodes/{NODE_ID}/tracking"
TOPIC_CONFIG = f"sari/nodes/{NODE_ID}/config"

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

telemetria_queue = queue.Queue(maxsize=10)

# Buffer para transmisión de video MJPEG
frame_lock = threading.Lock()
latest_encoded_frame = None


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
# SERVIDOR HTTP DE TRANSMISIÓN DE VIDEO MJPEG EN VIVO (PUERTO 8080)
# =====================================================================
def _generar_frames_mjpeg():
    """Generador continuo de frames JPEG codificados para el stream web de la interfaz SOC."""
    while True:
        with frame_lock:
            jpeg_bytes = latest_encoded_frame

        if jpeg_bytes is None:
            jpeg_bytes = crear_frame_espera("INICIALIZANDO STREAM EN VIVO...")

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
        time.sleep(0.04)  # ~25 FPS estables


def iniciar_servidor_stream_video():
    """Inicia el servidor HTTP de transmisión de video en segundo plano (Flask o Native HTTP)."""
    if FLASK_AVAILABLE:
        app = Flask(__name__)

        @app.after_request
        def add_cors_headers(response):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            return response

        @app.route('/video_feed')
        @app.route('/stream')
        @app.route('/mjpeg')
        def video_feed():
            return Response(_generar_frames_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

        @app.route('/snapshot')
        def snapshot():
            with frame_lock:
                data = latest_encoded_frame
            if data is None:
                data = crear_frame_espera("SNAPSHOT INICIALIZANDO...")
            return Response(data, mimetype='image/jpeg')

        @app.route('/')
        def index():
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>SARI Módulo Ojos — Video en Vivo</title>
                <style>
                    body {{ background: #0f172a; color: #f8fafc; font-family: monospace; text-align: center; margin: 0; padding: 20px; }}
                    h2 {{ color: #38bdf8; }}
                    img {{ border: 2px solid #38bdf8; border-radius: 8px; max-width: 960px; width: 100%; height: auto; box-shadow: 0 0 20px rgba(56, 189, 248, 0.3); }}
                    .badge {{ background: #22c55e; color: #000; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}
                </style>
            </head>
            <body>
                <h2>👁️ SARI Módulo Ojos — Transmisión Web MJPEG</h2>
                <p><span class="badge">EN VIVO</span> Cámara Hikvision PTZ con Superposición YOLO26n (RTSP/TCP)</p>
                <img src="/mjpeg" alt="Cámara PTZ en vivo" />
            </body>
            </html>
            """
            return Response(html_content, mimetype='text/html')

        print(f"[STREAM VIDEO] Servidor Flask en vivo escuchando en http://0.0.0.0:{STREAM_PORT}/mjpeg")
        threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=STREAM_PORT, debug=False, use_reloader=False),
            daemon=True,
            name="MJPEGStreamThread"
        ).start()
    else:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from socketserver import ThreadingMixIn

        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        class MJPEGHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path in ['/video_feed', '/stream', '/mjpeg']:
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.end_headers()
                    try:
                        for chunk in _generar_frames_mjpeg():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except Exception:
                        pass
                elif self.path == '/snapshot':
                    with frame_lock:
                        data = latest_encoded_frame
                    if not data:
                        data = crear_frame_espera("SNAPSHOT INICIALIZANDO...")
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    html = f"<html><body style='background:#0f172a;color:#fff;text-align:center;'><h2>👁️ SARI Módulo Ojos</h2><img src='/mjpeg' style='max-width:900px;'/></body></html>"
                    self.wfile.write(html.encode('utf-8'))

        def _run_native():
            server = ThreadedHTTPServer(('0.0.0.0', STREAM_PORT), MJPEGHandler)
            print(f"[STREAM VIDEO] Servidor HTTP Nativo escuchando en http://0.0.0.0:{STREAM_PORT}/mjpeg")
            server.serve_forever()

        threading.Thread(target=_run_native, daemon=True, name="MJPEGStreamThread").start()


# =====================================================================
# AUXILIAR: NOTIFICACIÓN REST AL SARI BRAIN AGENT
# =====================================================================
def notificar_evento_rest(camera_id, reason, duration, confidence=0.85, frame=None):
    """Envía un evento de intrusión estructurado con captura de evidencia en base64 al backend del Módulo Cerebro."""
    image_base64 = None
    if frame is not None:
        try:
            h, w = frame.shape[:2]
            snap_img = cv2.resize(frame, (1280, 720)) if w > 1280 else frame
            ok, buf = cv2.imencode('.jpg', snap_img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                image_base64 = base64.b64encode(buf.tobytes()).decode('utf-8')
        except Exception as e:
            print(f"[SNAPSHOT ERROR] No se pudo codificar imagen para alerta: {e}")

    snapshot_data_url = f"data:image/jpeg;base64,{image_base64}" if image_base64 else None

    payload = {
        "module_name": f"Jetson-{camera_id}",
        "camara_id": camera_id,
        "event_type": "intrusion",
        "severity": "high",
        "event": f"Intrusión ({reason}) - {round(duration, 1)}s",
        "message": f"Intrusión detectada ({reason}) durante {round(duration, 1)}s con {int(confidence*100)}% de confianza",
        "confidence": confidence,
        "duration": duration,
        "auto_siren": True,
        "image_base64": image_base64,
        "snapshot": snapshot_data_url,
        "image_url": snapshot_data_url,
        "metadata": {
            "confidence": confidence,
            "duration": duration,
            "image_base64": image_base64,
            "snapshot": snapshot_data_url
        }
    }
    
    def _post():
        candidate_urls = [
            CEREBRO_HTTP_EVENT_URL,
            "http://192.168.55.100:8000/api/alerts/event",
            "http://192.168.1.71:8000/api/alerts/event"
        ]
        seen = set()
        unique_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]

        enviado = False
        for url in unique_urls:
            try:
                resp = requests.post(url, json=payload, timeout=2.0)
                if resp.status_code in [200, 201, 202]:
                    print(f"[REST ALERT OK] Evidencia con captura enviada exitosamente a SARI Brain ({url}). Status: {resp.status_code}")
                    enviado = True
                    break
                else:
                    print(f"[REST ALERT WARNING] SARI Brain ({url}) respondió con HTTP {resp.status_code}")
            except Exception:
                continue

        if not enviado:
            print(f"[REST ALERT ERROR] No se pudo conectar con el backend de SARI Brain en ninguna de las URLs probadas: {unique_urls}")

        # Publicación al broker MQTT del Cerebro (Topic: sari/alerts, QoS=1)
        if HAS_MQTT_PUBLISH:
            try:
                alert_payload_mqtt = {
                    "node_id": NODE_ID,
                    "event_type": "intrusion",
                    "severity": "high",
                    "message": "Intrusión verificada en perímetro",
                    "confidence": round(confidence, 2),
                    "snapshot": image_base64 or ""
                }
                mqtt_publish.single(
                    topic="sari/alerts",
                    payload=json.dumps(alert_payload_mqtt),
                    qos=1,
                    hostname=MQTT_BROKER_HOST,
                    port=MQTT_BROKER_PORT,
                    auth={"username": MQTT_USER, "password": MQTT_PASS},
                    keepalive=10
                )
                print(f"[MQTT ALERT OK] Alerta de intrusión con snapshot base64 publicada en {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} topic sari/alerts")
            except Exception as e_mqtt:
                print(f"[MQTT ALERT WARNING] No se pudo publicar alerta en MQTT: {e_mqtt}")

    threading.Thread(target=_post, daemon=True).start()


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
        print(f"[VIDEO] Abriendo stream Hikvision RTSP: {self.rtsp_url}...")
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


def ejecutar_comando_ptz_manual(action, pan_delta=0, tilt_delta=0, zoom_delta=0):
    """Ejecuta comandos de movimiento manual PTZ recibidos de SARI Cerebro."""
    global _manual_timer
    if ptz_controlador is None:
        print("[PTZ MANUAL] Controlador PTZ aún no inicializado.")
        return

    action_lower = str(action).lower() if action else ""

    if action_lower == "stop":
        with _manual_lock:
            if _manual_timer and _manual_timer.is_alive():
                _manual_timer.cancel()
        ptz_controlador.detener()
        return

    pan_speed = 0
    tilt_speed = 0
    zoom_speed = 0
    is_pulse = False

    # 1. Comandos directos D-Pad
    if action_lower == "up":
        tilt_speed = 50
        is_pulse = True
    elif action_lower == "down":
        tilt_speed = -50
        is_pulse = True
    elif action_lower == "left":
        pan_speed = -50
        is_pulse = True
    elif action_lower == "right":
        pan_speed = 50
        is_pulse = True
    elif action_lower == "center":
        ptz_controlador.detener()
        return
    elif action_lower == "zoom_in":
        zoom_speed = 50
        is_pulse = True
    elif action_lower == "zoom_out":
        zoom_speed = -50
        is_pulse = True
    else:
        # 2. Comandos Click & Drag / Joystick por deltas
        if pan_delta != 0:
            pan_speed = max(min(int(pan_delta), 100), -100)
            is_pulse = True
        if tilt_delta != 0:
            tilt_speed = max(min(int(tilt_delta), 100), -100)
            is_pulse = True
        if zoom_delta != 0:
            zoom_speed = max(min(int(zoom_delta), 100), -100)
            is_pulse = True

    with _manual_lock:
        if _manual_timer and _manual_timer.is_alive():
            _manual_timer.cancel()

    ptz_controlador.mover(pan_speed, tilt_speed, zoom_speed, force=True)

    # Si es un pulso manual, auto-detener tras 0.35s de inactividad
    if is_pulse:
        def _auto_stop():
            time.sleep(0.35)
            if ptz_controlador is not None:
                ptz_controlador.detener()
        with _manual_lock:
            _manual_timer = threading.Thread(target=_auto_stop, daemon=True)
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
# COMUNICACIÓN WEBSOCKET ASÍNCRONA CON EL CEREBRO
# =====================================================================
async def websocket_loop():
    """Bucle asíncrono para enviar telemetría y recibir comandos (con reconexión exponencial)."""
    backoff = 1.0
    max_backoff = 60.0
    
    while True:
        try:
            print(f"[WEBSOCKET] Conectando al Módulo Cerebro en {CEREBRO_URL}...")
            async with websockets.connect(CEREBRO_URL) as ws:
                print("[WEBSOCKET] ✅ Conectado exitosamente al Módulo Cerebro.")
                backoff = 1.0
                
                heartbeat_task = asyncio.create_task(enviar_heartbeat(ws))
                telemetry_task = asyncio.create_task(procesar_telemetria(ws))
                receive_task = asyncio.create_task(recibir_comandos(ws))
                
                done, pending = await asyncio.wait(
                    [heartbeat_task, telemetry_task, receive_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                for task in pending:
                    task.cancel()
                    
        except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, Exception) as e:
            print(f"[WEBSOCKET] Reintentando conexión con Cerebro en {backoff}s... ({e})")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

async def enviar_heartbeat(ws):
    """Envía un ping de vida cada 2 segundos."""
    while True:
        payload = {
            "tipo": "heartbeat",
            "camara_id": "PTZ_1",
            "timestamp": time.time()
        }
        await ws.send(json.dumps(payload))
        await asyncio.sleep(2)

async def procesar_telemetria(ws):
    """Extrae datos de la cola y los envía al Cerebro."""
    while True:
        try:
            payload = telemetria_queue.get_nowait()
            await ws.send(json.dumps(payload))
            telemetria_queue.task_done()
        except queue.Empty:
            await asyncio.sleep(0.01)

async def recibir_comandos(ws):
    """Escucha comandos entrantes desde el Cerebro."""
    async for mensaje in ws:
        try:
            datos = json.loads(mensaje)
            comando = datos.get("comando")
            
            if comando == "set_tracking":
                global human_tracking_active
                nuevo_estado = datos.get("estado", True)
                human_tracking_active = bool(nuevo_estado)
                estado_global["auto_tracking"] = human_tracking_active
                print(f"[COMANDO] Auto-tracking cambiado a: {nuevo_estado}")
                if not human_tracking_active and ptz_controlador is not None:
                    ptz_controlador.detener()
                
            elif comando == "telegram_alert":
                texto = datos.get("mensaje", "Alerta desde el Módulo Ojo")
                print("[COMANDO] Alerta de Telegram solicitada.")
                threading.Thread(target=enviar_alerta_telegram, args=(texto,), daemon=True).start()
                
            else:
                print(f"[WEBSOCKET] Comando no reconocido: {comando}")
                
        except json.JSONDecodeError:
            print("[WEBSOCKET] Mensaje inválido recibido del Cerebro.")

def iniciar_hilo_websocket():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(websocket_loop())


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
            payload = json.loads(msg.payload.decode("utf-8"))

            # 1. Comando de Seguimiento de Humanos
            if msg.topic == TOPIC_TRACKING:
                human_tracking_active = bool(payload.get("enabled", True))
                estado_global["auto_tracking"] = human_tracking_active
                print(f"[TRACKING] Seguimiento de humanos {'ACTIVADO' if human_tracking_active else 'PAUSADO'}")
                if not human_tracking_active and ptz_controlador is not None:
                    ptz_controlador.detener()

            # 2. Comandos de Movimiento Manual PTZ (Click & Drag / D-Pad / Zoom)
            elif msg.topic == TOPIC_PTZ:
                action = payload.get("action")
                pan_delta = payload.get("pan_delta", 0)
                tilt_delta = payload.get("tilt_delta", 0)
                zoom_delta = payload.get("zoom_delta", 0)
                print(f"[PTZ] Moviendo cámara: {action} (Pan: {pan_delta}, Tilt: {tilt_delta}, Zoom: {zoom_delta})")
                ejecutar_comando_ptz_manual(action, pan_delta, tilt_delta, zoom_delta)

            # 3. Configuración dinámica (sari/nodes/{NODE_ID}/config)
            elif msg.topic == TOPIC_CONFIG:
                print(f"[MQTT CONFIG] Mensaje de configuración recibido en {msg.topic}: {payload}")
                if "yolo_confidence" in payload:
                    nuevo_umbral = float(payload["yolo_confidence"])
                    if 0.1 <= nuevo_umbral <= 0.99:
                        CONFIDENCE_THRESHOLD = nuevo_umbral
                        print(f"[MQTT CONFIG OK] Umbral de confianza actualizado a {round(CONFIDENCE_THRESHOLD * 100, 1)}%")

                if "active" in payload or "ptz_tracking" in payload:
                    val = payload.get("ptz_tracking", payload.get("active", True))
                    human_tracking_active = bool(val)
                    estado_global["auto_tracking"] = human_tracking_active
                    print(f"[MQTT CONFIG OK] Auto-tracking actualizado a: {human_tracking_active}")
                    if not human_tracking_active and ptz_controlador is not None:
                        ptz_controlador.detener()

        except Exception as e:
            print(f"[ERROR] Procesando mensaje MQTT: {e}")

    def _mqtt_loop():
        while True:
            try:
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"jetson_vision_ctrl_{NODE_ID}")
                except Exception:
                    client = mqtt.Client(client_id=f"jetson_vision_ctrl_{NODE_ID}")
                client.username_pw_set(MQTT_USER, MQTT_PASS)
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
    global latest_encoded_frame

    print("\n" + "=" * 60)
    print("  SARI — MÓDULO OJOS (Eye Node v2.0)")
    print("  Visión por Computadora Acelerada + Control PTZ + Stream MJPEG (8080)")
    print("=" * 60 + "\n")
    
    # Inicializar frame de espera inmediatamente para evitar pantallas negras al conectar
    with frame_lock:
        latest_encoded_frame = crear_frame_espera("CONECTANDO A CÁMARA HIKVISION...")

    # 1. Inicializar PTZ y vincular controlador global para comandos manuales
    global ptz_controlador
    ptz = HikvisionPTZ(ip=CAMERA_IP, username=USERNAME, password=PASSWORD)
    ptz_controlador = ptz

    # 2. Iniciar Hilos de Comunicación (WebSockets y Control MQTT PTZ / Tracking)
    ws_thread = threading.Thread(target=iniciar_hilo_websocket, daemon=True, name="WebSocketThread")
    ws_thread.start()
    iniciar_hilo_mqtt_control()

    # 3. Iniciar Servidor de Video en Vivo MJPEG (Puerto 8080)
    iniciar_servidor_stream_video()

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
                time.sleep(0.005)
                continue

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
                                payload_alerta = {
                                    "tipo": "alerta",
                                    "camara_id": "PTZ_1",
                                    "razon": "persona_mas_de_5s",
                                    "tiempo_detectado": dur_round,
                                    "timestamp": time.time()
                                }
                                try:
                                    telemetria_queue.put_nowait(payload_alerta)
                                    print(f"[ALERTA PTZ] Persona detectada durante {dur_round}s. Enviando telemetría y evento REST al Cerebro...")
                                    notificar_evento_rest("PTZ_1", "persona_mas_de_5s", dur_round, confidence=best_coords[2], frame=annotated_frame)
                                    ultimo_envio_alerta = time.time()
                                    tiempo_inicio_deteccion = None
                                    ultimo_visto = None
                                except queue.Full:
                                    pass
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
                    with frame_lock:
                        latest_encoded_frame = jpeg_buf.tobytes()
            except Exception as e:
                print(f"[ENCODE ERROR] {e}")

            # Enviar telemetría periódica a la cola de WebSockets
            payload = {
                "tipo": "telemetria",
                "camara_id": "PTZ_1",
                "timestamp": time.time(),
                "detecciones": detecciones_payload,
                "estado_ptz": {
                    "pan": estado_global["pan_actual"],
                    "tilt": estado_global["tilt_actual"]
                }
            }
            try:
                telemetria_queue.put_nowait(payload)
            except queue.Full:
                pass

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
