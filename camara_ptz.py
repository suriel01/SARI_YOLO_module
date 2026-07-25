#!/usr/bin/env python3
"""
Módulo Ojo - Microservicio de Producción Dockerizado (SARI Eye Node).
Captura de video RTSP de alta velocidad, detección YOLO acelerada (TensorRT/CUDA FP16),
seguimiento PTZ Hikvision ultra-fluido (25Hz) y Servidor de Video en Vivo MJPEG (Puerto 8080).

Comunica telemetría y alertas al Módulo Cerebro (SARI Brain Agent) mediante:
  1. WebSockets: ws://<CEREBRO_HOST>:8765
  2. REST API: http://<CEREBRO_HOST>:8000/api/alerts/event
  3. Servidor de Video Web MJPEG: http://0.0.0.0:8080/video_feed
"""

import os
import time
import threading
import json
import asyncio
import queue
import cv2
import requests
import websockets
from requests.auth import HTTPDigestAuth

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

CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.70"))
STREAM_PORT = int(os.environ.get("STREAM_PORT", "8080"))
RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@{CAMERA_IP}:554/Streaming/Channels/101"

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
# SERVIDOR HTTP DE TRANSMISIÓN DE VIDEO MJPEG EN VIVO (PUERTO 8080)
# =====================================================================
def _generar_frames_mjpeg():
    """Generador de frames JPEG codificados para el stream MJPEG del navegador."""
    while True:
        with frame_lock:
            jpeg_bytes = latest_encoded_frame

        if jpeg_bytes is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
        time.sleep(0.033)  # ~30 FPS


def iniciar_servidor_stream_video():
    """Inicia el servidor HTTP de transmisión de video en segundo plano (Flask o Native HTTP)."""
    if FLASK_AVAILABLE:
        app = Flask(__name__)

        @app.route('/video_feed')
        @app.route('/stream')
        def video_feed():
            return Response(_generar_frames_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

        @app.route('/snapshot')
        def snapshot():
            with frame_lock:
                if latest_encoded_frame is not None:
                    return Response(latest_encoded_frame, mimetype='image/jpeg')
            return Response("Frame no disponible", status=503)

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
                <p><span class="badge">EN VIVO</span> Cámara Hikvision PTZ con Superposición YOLO26n</p>
                <img src="/video_feed" alt="Cámara PTZ en vivo" />
            </body>
            </html>
            """
            return Response(html_content, mimetype='text/html')

        print(f"[STREAM VIDEO] Servidor Flask en vivo escuchando en http://0.0.0.0:{STREAM_PORT}/video_feed")
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
                if self.path in ['/video_feed', '/stream']:
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
                    if data:
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/jpeg')
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self.send_error(503)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    html = f"<html><body style='background:#0f172a;color:#fff;text-align:center;'><h2>👁️ SARI Módulo Ojos</h2><img src='/video_feed' style='max-width:900px;'/></body></html>"
                    self.wfile.write(html.encode('utf-8'))

        def _run_native():
            server = ThreadedHTTPServer(('0.0.0.0', STREAM_PORT), MJPEGHandler)
            print(f"[STREAM VIDEO] Servidor HTTP Nativo escuchando en http://0.0.0.0:{STREAM_PORT}/video_feed")
            server.serve_forever()

        threading.Thread(target=_run_native, daemon=True, name="MJPEGStreamThread").start()


# =====================================================================
# AUXILIAR: NOTIFICACIÓN REST AL SARI BRAIN AGENT
# =====================================================================
def notificar_evento_rest(camera_id, reason, duration, confidence=0.85):
    """Envía un evento de intrusión estructurado al backend del Módulo Cerebro."""
    payload = {
        "module_name": f"Jetson-{camera_id}",
        "event": f"Intrusión ({reason}) - {round(duration, 1)}s",
        "confidence": confidence,
        "auto_siren": True
    }
    
    def _post():
        try:
            resp = requests.post(CEREBRO_HTTP_EVENT_URL, json=payload, timeout=3.0)
            if resp.status_code == 200:
                print(f"[REST ALERT] Evidencia de intrusión enviada exitosamente a SARI Brain Agent ({CEREBRO_HTTP_EVENT_URL}).")
            else:
                print(f"[REST ALERT WARNING] SARI Brain respondió con HTTP {resp.status_code}")
        except Exception as e:
            print(f"[REST ALERT ERROR] No se pudo enviar evento HTTP al Cerebro ({CEREBRO_HTTP_EVENT_URL}): {e}")

    threading.Thread(target=_post, daemon=True).start()


# =====================================================================
# CLASE: CAPTURA DE VIDEO MULTIHILO ALTA VELOCIDAD (ANTI-LAG)
# =====================================================================
class ThreadedVideoCapture:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.cap = None
        self.ret = False
        self.frame = None
        self.running = False
        self.lock = threading.Lock()
        self.thread = None
        self._initialize_capture()

    def _initialize_capture(self):
        if self.cap is not None:
            self.cap.release()
        print(f"[VIDEO] Conectando a {self.rtsp_url}...")
        self.cap = cv2.VideoCapture(self.rtsp_url)
        self.cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
    def start(self):
        if self.running:
            return self
        self.running = True
        self.thread = threading.Thread(target=self._update, daemon=True, name="RTSPReaderThread")
        self.thread.start()
        return self

    def _update(self):
        consecutive_failures = 0
        max_failures = 30
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                print("[VIDEO] Conexión perdida. Reintentando...")
                self._initialize_capture()
                time.sleep(2)
                continue

            ret, frame = self.cap.read()
            if not ret:
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    print("[WARNING] Reiniciando stream RTSP...")
                    self._initialize_capture()
                    consecutive_failures = 0
                time.sleep(0.005)
                continue
            
            consecutive_failures = 0
            with self.lock:
                self.ret = ret
                self.frame = frame
            time.sleep(0.001)

    def read(self):
        with self.lock:
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

    def mover(self, pan, tilt, force=False):
        if pan == 0 and tilt == 0:
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
        return self.mover(0, 0, force=True)


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
                nuevo_estado = datos.get("estado", True)
                estado_global["auto_tracking"] = nuevo_estado
                print(f"[COMANDO] Auto-tracking cambiado a: {nuevo_estado}")
                
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
# BUCLE PRINCIPAL DE PROCESAMIENTO (OPENCV + YOLO + TRACKING SUAVE + MJPEG)
# =====================================================================
def main():
    global latest_encoded_frame

    print("\n" + "=" * 60)
    print("  SARI — MÓDULO OJOS (Eye Node v2.0)")
    print("  Visión por Computadora Acelerada + Control PTZ + Stream MJPEG (8080)")
    print("=" * 60 + "\n")
    
    # 1. Iniciar Hilo de WebSockets
    ws_thread = threading.Thread(target=iniciar_hilo_websocket, daemon=True, name="WebSocketThread")
    ws_thread.start()

    # 2. Iniciar Servidor de Video en Vivo MJPEG (Puerto 8080)
    iniciar_servidor_stream_video()

    # 3. Cargar Modelo YOLO
    model = cargar_modelo_yolo()
    
    # 4. Inicializar PTZ y Captura de Video
    ptz = HikvisionPTZ(ip=CAMERA_IP, username=USERNAME, password=PASSWORD)
    capture = ThreadedVideoCapture(rtsp_url=RTSP_URL)
    capture.start()

    last_ptz_send_time = 0.0
    ptz_command_cooldown = 0.04
    was_moving = False
    
    tiempo_inicio_deteccion = None
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
            
            # Frame anotado para el stream web
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
                                
                                # Dibujar Bounding Box en el frame de transmisión web
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
                                    print(f"[DETECCIÓN ≥70%] Persona detectada — Confianza: {round(conf_val * 100, 1)}% | BBox: [{x1}, {y1}, {x2}, {y2}]")
                                
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
                    if best_coords is not None and estado_global["auto_tracking"]:
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
                                
                    # CONTROL DE ALERTAS DE INTRUSIÓN PROLONGADA (> 5 SEGUNDOS)
                    if best_coords is not None:
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
                                    notificar_evento_rest("PTZ_1", "persona_mas_de_5s", dur_round, confidence=best_coords[2])
                                    ultimo_envio_alerta = time.time()
                                except queue.Full:
                                    pass
                    else:
                        tiempo_inicio_deteccion = None
                        if was_moving:
                            ptz.detener()
                            was_moving = False

                except Exception as e:
                    print(f"[YOLO ERROR] {e}")

            # Codificar frame anotado para la transmisión HTTP MJPEG
            try:
                ok, jpeg_buf = cv2.imencode('.jpg', annotated_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok:
                    with frame_lock:
                        latest_encoded_frame = jpeg_buf.tobytes()
            except Exception:
                pass

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
