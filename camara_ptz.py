#!/usr/bin/env python3
"""
Script modular para control PTZ e integración de procesamiento de video en tiempo real.
Diseñado para cámaras Hikvision, con captura multihilo para evitar lag, control por Joystick Virtual,
y detección de personas en tiempo real con YOLO26n acelerado con TensorRT/CUDA (FP16).
"""

import os
import time
import threading
import cv2
import requests
from requests.auth import HTTPDigestAuth

# Intentar importar dependencias de ML
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
# CLASE: CAPTURA DE VIDEO MULTIHILO (ANTI-LAG)
# =====================================================================
class ThreadedVideoCapture:
    """
    Clase para leer el flujo de video RTSP en un hilo dedicado.
    Evita la acumulación de frames en el buffer interno de OpenCV,
    garantizando que el bucle principal siempre reciba el frame más reciente.
    """
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
        """Inicializa o reinicializa el objeto VideoCapture de OpenCV."""
        if self.cap is not None:
            self.cap.release()
        print(f"[VIDEO] Conectando a {self.rtsp_url}...")
        self.cap = cv2.VideoCapture(self.rtsp_url)
        # Opcional: Forzar transporte sobre TCP para mayor estabilidad
        self.cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        
    def start(self):
        """Inicia el hilo de lectura continua."""
        if self.running:
            print("[VIDEO] El hilo ya está corriendo.")
            return self
        self.running = True
        self.thread = threading.Thread(target=self._update, daemon=True, name="RTSPReaderThread")
        self.thread.start()
        return self

    def _update(self):
        """Bucle del hilo: Lee y vacía el buffer continuamente."""
        consecutive_failures = 0
        max_failures = 30  # Intentos antes de intentar reconectar por completo

        while self.running:
            if self.cap is None or not self.cap.isOpened():
                print("[VIDEO] Conexión perdida. Reintentando inicializar...")
                self._initialize_capture()
                time.sleep(2)
                continue

            ret, frame = self.cap.read()
            if not ret:
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    print("[WARNING] Demasiados fallos de lectura de frame. Reiniciando stream...")
                    self._initialize_capture()
                    consecutive_failures = 0
                time.sleep(0.01)
                continue
            
            consecutive_failures = 0
            
            # Guardamos el frame más reciente de forma segura utilizando un Lock
            with self.lock:
                self.ret = ret
                self.frame = frame

            # Pequeña pausa para no saturar la CPU
            time.sleep(0.001)

    def read(self):
        """Devuelve el último frame capturado de manera segura."""
        with self.lock:
            return self.ret, self.frame.copy() if self.frame is not None else None

    def stop(self):
        """Detiene el hilo de lectura y libera los recursos del video."""
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.cap is not None:
            self.cap.release()
        print("[VIDEO] Hilo de captura detenido y recursos liberados.")


# =====================================================================
# CLASE: CONTROL PTZ MEDIANTE ISAPI HIKVISION
# =====================================================================
class HikvisionPTZ:
    """
    Clase para interactuar con el control de movimiento continuo (PTZ) de Hikvision.
    Realiza llamadas HTTP PUT con autenticación Digest.
    Incluye sistema de Throttling para no saturar la red ni la cámara.
    """
    def __init__(self, ip, username, password, channel=1, timeout=3.0):
        self.ip = ip
        self.url = f"http://{ip}/ISAPI/PTZCtrl/channels/{channel}/continuous"
        self.auth = HTTPDigestAuth(username, password)
        self.timeout = timeout
        
        # Throttling
        self.last_pan = 0
        self.last_tilt = 0
        self.last_send_time = 0.0

    def mover(self, pan, tilt, force=False):
        """
        Envía velocidades de movimiento continuo a la cámara.
        
        Args:
            pan (int): Velocidad de paneo horizontal (-100 a 100).
            tilt (int): Velocidad de inclinación vertical (-100 a 100).
            force (bool): Si es True, ignora el throttling y envía inmediatamente.
        """
        # Asegurar parada inmediata saltando el throttling
        if pan == 0 and tilt == 0:
            force = True

        now = time.time()
        # Throttling: Evitar comandos si cambiaron hace menos de 100ms y los valores son idénticos
        if not force:
            if (now - self.last_send_time < 0.1) and (pan == self.last_pan and tilt == self.last_tilt):
                return True

        self.last_pan = pan
        self.last_tilt = tilt
        self.last_send_time = now

        # Payload XML requerido por el estándar ISAPI de Hikvision
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
    <pan>{int(pan)}</pan>
    <tilt>{int(tilt)}</tilt>
</PTZData>"""
        
        headers = {
            "Content-Type": "application/xml"
        }

        # Ejecutar petición HTTP PUT en segundo plano (para no bloquear el renderizado del video)
        def _enviar():
            try:
                response = requests.put(
                    self.url,
                    data=payload,
                    headers=headers,
                    auth=self.auth,
                    timeout=self.timeout
                )
                if response.status_code not in [200, 201]:
                    print(f"[PTZ ERROR] HTTP {response.status_code}: {response.text.strip()}")
            except requests.exceptions.RequestException as e:
                print(f"[PTZ EXCEPCIÓN] Error al mover PTZ: {e}")

        # Ejecutamos el envío en un hilo ligero para que no afecte la tasa de FPS de visualización
        threading.Thread(target=_enviar, daemon=True).start()
        return True

    def detener(self):
        """Detiene cualquier movimiento en curso enviando velocidades en cero."""
        return self.mover(0, 0, force=True)


# =====================================================================
# FUNCIÓN: CARGA Y COMPILACIÓN DE YOLO26N A TENSORRT
# =====================================================================
def cargar_modelo_yolo():
    """
    Carga el modelo YOLO26n.
    Si existe un motor TensorRT (.engine) compilado con precisión FP16, lo carga.
    De lo contrario, descarga yolo26n.pt y compila a .engine.
    Si la compilación de TensorRT no es posible, cae en ejecución CUDA nativa (FP16).
    """
    if not YOLO_AVAILABLE:
        print("[YOLO WARNING] Ultralytics no está instalado. Ejecución sin detección de objetos.")
        return None

    engine_path = "yolo26n.engine"
    pt_path = "yolo26n.pt"

    # 1. Comprobar si ya existe el motor compilado de TensorRT
    if os.path.exists(engine_path):
        print(f"[YOLO] Motor TensorRT encontrado en '{engine_path}'. Cargando...")
        try:
            model = YOLO(engine_path, task="detect")
            print("[YOLO] Motor TensorRT cargado con éxito para inferencia ultra-rápida.")
            return model
        except Exception as e:
            print(f"[YOLO WARNING] No se pudo cargar '{engine_path}' directamente: {e}")
            print("[YOLO] Se intentará recompilar o usar el modelo PyTorch...")

    # 2. Cargar modelo original PyTorch (.pt)
    print(f"[YOLO] Cargando modelo PyTorch '{pt_path}'...")
    try:
        model = YOLO(pt_path)
    except Exception as e:
        print(f"[YOLO] Descargando modelo base '{pt_path}' desde Ultralytics...")
        model = YOLO("yolo26n.pt")

    # 3. Intentar exportar a TensorRT si CUDA está disponible
    if PYTORCH_AVAILABLE and torch.cuda.is_available():
        device_name = torch.cuda.get_device_name(0)
        print(f"[YOLO] Detectada GPU CUDA: {device_name}.")
        print("[YOLO] Compilando a TensorRT (.engine) con precisión FP16 (half=True)...")
        print("[YOLO] NOTA: La primera compilación puede tardar varios minutos.")
        
        try:
            # Exportación nativa de Ultralytics
            # half=True habilita FP16 para aprovechar los Tensor Cores de la RTX 4070
            # device=0 indica que se use la GPU para la compilación
            model.export(format="engine", half=True, device=0)
            
            # Recargar el modelo ya en formato TensorRT (.engine)
            if os.path.exists(engine_path):
                model = YOLO(engine_path, task="detect")
                print("[YOLO] Motor TensorRT (.engine) compilado y cargado correctamente.")
                return model
            else:
                print("[YOLO WARNING] Compilación reportada con éxito pero no se encontró 'yolo26n.engine'.")
        except Exception as e:
            print(f"[YOLO ERROR] Error durante la exportación a TensorRT: {e}")
            print("[YOLO] Cayendo en modo CUDA Nativo (FP16 / Half Precision)...")
            
        # Si falló la compilación a TensorRT, forzamos PyTorch CUDA en FP16
        try:
            model.to("cuda")
            print("[YOLO] Inicializado en modo CUDA nativo (FP16).")
        except Exception as e:
            print(f"[YOLO ERROR] No se pudo transferir el modelo a CUDA: {e}. Usando CPU.")
    else:
        print("[YOLO WARNING] CUDA no disponible. Usando CPU para inferencia.")

    return model


# =====================================================================
# BUCLE PRINCIPAL DE EJECUCIÓN
# =====================================================================
def main():
    # --- CONFIGURACIÓN DE LA CÁMARA ---
    CAMERA_IP = "192.168.1.200"
    USERNAME = "admin"
    PASSWORD = "Asenso117925"  # Contraseña actualizada por el usuario
    
    # Flujo RTSP primario (Canal 101: canal 1 stream principal)
    RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@{CAMERA_IP}:554/Streaming/Channels/101"

    print("\n=== INICIANDO SISTEMA PTZ + YOLO26n REAL-TIME ===")
    
    # 1. Cargar el modelo YOLO
    model = cargar_modelo_yolo()
    
    # 2. Inicialización de control PTZ
    ptz = HikvisionPTZ(ip=CAMERA_IP, username=USERNAME, password=PASSWORD)
    
    # 3. Inicialización de captura de video multihilo (Anti-Lag)
    capture = ThreadedVideoCapture(rtsp_url=RTSP_URL)
    capture.start()

    # Estado del Joystick Virtual
    is_dragging = False
    start_x, start_y = -1, -1
    current_x, current_y = -1, -1

    # Definir el callback de mouse
    def mouse_callback(event, x, y, flags, param):
        nonlocal is_dragging, start_x, start_y, current_x, current_y
        
        if event == cv2.EVENT_LBUTTONDOWN:
            is_dragging = True
            start_x, start_y = x, y
            current_x, current_y = x, y
            
        elif event == cv2.EVENT_MOUSEMOVE:
            if is_dragging:
                current_x, current_y = x, y
                
                # Calcular el desplazamiento (delta) desde el punto de inicio
                dx = x - start_x
                dy = y - start_y
                
                # Sensibilidad: 100 píxeles de arrastre equivalen a la velocidad máxima (100)
                # pan_speed y tilt_speed se limitan a [-100, 100]
                pan_speed = int((dx / 100.0) * 100)
                tilt_speed = int((-dy / 100.0) * 100)  # Invertir eje Y para OpenCV
                
                pan_speed = max(min(pan_speed, 100), -100)
                tilt_speed = max(min(tilt_speed, 100), -100)
                
                # Mover cámara
                ptz.mover(pan_speed, tilt_speed)
                
        elif event == cv2.EVENT_LBUTTONUP:
            if is_dragging:
                is_dragging = False
                start_x, start_y = -1, -1
                current_x, current_y = -1, -1
                print("[PTZ] Joystick soltado. Frenando cámara...")
                ptz.detener()

    # Crear ventana de visualización y registrar el callback del mouse
    window_name = "Hikvision PTZ & YOLO26n (Real-Time)"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    print("\n=======================================================")
    print("INSTRUCCIONES DE USO:")
    print("1. HAZ CLIC IZQUIERDO Y ARRASTRA en cualquier parte del video para")
    print("   mover la cámara en esa dirección (Joystick Virtual).")
    print("2. SUELTA EL CLIC para detener el movimiento automáticamente.")
    print("3. Presiona la tecla [Q] para cerrar y salir limpiamente.")
    print("=======================================================\n")

    # Inicializar contador de FPS
    last_time = time.time()
    frame_count = 0
    fps_display = "0.0 FPS"

    # Variables de seguimiento automático PTZ
    auto_tracking = True
    last_ptz_send_time = 0.0
    ptz_command_cooldown = 0.15  # Rate limit rígido de 150ms
    was_moving = False

    try:
        while True:
            # Leer el frame más reciente
            ret, frame = capture.read()

            if not ret or frame is None:
                time.sleep(0.005)
                continue

            h, w = frame.shape[:2]
            cx, cy = w // 2, h // 2

            # Inferencia de YOLO
            display_frame = frame.copy()
            
            if model is not None:
                # === ESPACIO PARA INFERENCIA DE YOLO ===
                # Detección optimizada con CUDA/TensorRT en precisión FP16 (half)
                # classes=[0] filtra únicamente personas (ID 0 en el dataset COCO)
                # verbose=False para no saturar el stdout
                device_inference = "cuda:0" if (PYTORCH_AVAILABLE and torch.cuda.is_available()) else "cpu"
                
                # Para modelos de TensorRT (.engine), ultralytics no necesita el parámetro half
                # Para modelos de PyTorch en CUDA, forzamos half=True para aprovechar los Tensor Cores
                use_half = False
                if PYTORCH_AVAILABLE and torch.cuda.is_available():
                    use_half = not str(model.ckpt).endswith('.engine')

                try:
                    results = model(
                        frame,
                        device=device_inference,
                        half=use_half,
                        classes=[0],
                        verbose=False
                    )
                    # Pintar los bounding boxes de las personas detectadas
                    display_frame = results[0].plot()

                    best_box = None
                    min_dist = float('inf')
                    best_coords = None  # (px, py, conf_val)

                    if results[0].boxes is not None and len(results[0].boxes) > 0:
                        for box in results[0].boxes:
                            conf_val = float(box.conf[0])
                            # Filtrar únicamente personas con confianza de 70% o superior (0.70)
                            if conf_val >= 0.70:
                                xyxy = box.xyxy[0].tolist()  # [x1, y1, x2, y2]
                                x1, y1, x2, y2 = xyxy
                                px = int((x1 + x2) / 2)
                                py = int((y1 + y2) / 2)
                                
                                # Calcular distancia euclidiana al centro del frame
                                dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                                if dist < min_dist:
                                    min_dist = dist
                                    best_box = xyxy
                                    best_coords = (px, py, conf_val)

                    if best_coords is not None:
                        px, py, conf_val = best_coords
                        
                        # Dibujar mira central y línea al objetivo
                        cv2.line(display_frame, (cx, cy), (px, py), (0, 255, 255), 2)
                        cv2.circle(display_frame, (px, py), 6, (0, 0, 255), -1)
                        
                        # Dibujar rectángulo distintivo naranja para el objetivo seleccionado
                        x1, y1, x2, y2 = map(int, best_box)
                        cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 165, 255), 3)
                        cv2.putText(
                            display_frame,
                            f"TARGET [{conf_val*100:.0f}%]",
                            (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.6,
                            (0, 165, 255),
                            2
                        )

                        if auto_tracking and not is_dragging:
                            # Calcular desfase normalizado (-1.0 a 1.0)
                            offset_x = px - cx
                            offset_y = cy - py  # Positivo si está arriba
                            
                            norm_x = offset_x / cx
                            norm_y = offset_y / cy
                            
                            # Zona muerta del 15%
                            deadzone = 0.15
                            
                            pan_speed = 0
                            tilt_speed = 0
                            
                            if abs(norm_x) > deadzone:
                                sign_x = 1 if norm_x > 0 else -1
                                # Control Proporcional mapeado a velocidades útiles de Hikvision [15, 60]
                                pan_speed = int(sign_x * (15 + (abs(norm_x) - deadzone) / (1.0 - deadzone) * 45))
                                
                            if abs(norm_y) > deadzone:
                                sign_y = 1 if norm_y > 0 else -1
                                tilt_speed = int(sign_y * (15 + (abs(norm_y) - deadzone) / (1.0 - deadzone) * 45))
                                
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
                        # Si no hay objetivo y nos estábamos moviendo en modo automático, detener la cámara
                        if auto_tracking and not is_dragging and was_moving:
                            ptz.detener()
                            was_moving = False

                except Exception as e:
                    print(f"[YOLO ERROR] Error durante la inferencia: {e}")
                    # En caso de error, continuar mostrando el frame crudo
                    display_frame = frame.copy()

            # Dibujar el Joystick Virtual Overlay si se está arrastrando el mouse
            if is_dragging and start_x != -1:
                # Centro del joystick (clic inicial)
                cv2.circle(display_frame, (start_x, start_y), 8, (0, 255, 0), -1)
                # Límite máximo del joystick (círculo exterior de 100px)
                cv2.circle(display_frame, (start_x, start_y), 100, (0, 255, 0), 2)
                # Línea de dirección de arrastre
                cv2.line(display_frame, (start_x, start_y), (current_x, current_y), (0, 255, 255), 2)
                # Posición actual del dedo/mouse
                cv2.circle(display_frame, (current_x, current_y), 6, (0, 0, 255), -1)
                
                # Mostrar texto de las velocidades calculadas
                dx = current_x - start_x
                dy = current_y - start_y
                pan_speed = max(min(int((dx / 100.0) * 100), 100), -100)
                tilt_speed = max(min(int((-dy / 100.0) * 100), 100), -100)
                cv2.putText(
                    display_frame,
                    f"Pan: {pan_speed} | Tilt: {tilt_speed}",
                    (start_x - 70, start_y - 110),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    2
                )

            # Calcular y mostrar FPS
            frame_count += 1
            now = time.time()
            if now - last_time >= 1.0:
                fps = frame_count / (now - last_time)
                fps_display = f"{fps:.1f} FPS"
                frame_count = 0
                last_time = now
            
            # Dibujar FPS en pantalla
            cv2.putText(
                display_frame,
                fps_display,
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (0, 255, 255),
                2
            )

            # Dibujar mira de referencia en el centro
            cv2.drawMarker(display_frame, (cx, cy), (255, 0, 0), markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)

            # Mostrar estado de auto-tracking en pantalla
            if auto_tracking:
                status_text = "TRACKING: ACTIVO (Tecla [T] para pausar)"
                color = (0, 255, 0)
            else:
                status_text = "TRACKING: MANUAL (Tecla [T] para activar)"
                color = (0, 0, 255)
            cv2.putText(
                display_frame,
                status_text,
                (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2
            )

            # Mostrar flujo de video
            cv2.imshow(window_name, display_frame)

            # Capturar tecla para salir o interactuar
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                print("[INFO] Tecla 'q' presionada. Cerrando sistema...")
                break
            elif key == ord('t') or key == ord('T'):
                auto_tracking = not auto_tracking
                print(f"[PTZ] Auto-tracking cambiado a: {auto_tracking}")
                if not auto_tracking:
                    ptz.detener()
                    was_moving = False

    except KeyboardInterrupt:
        print("[INFO] Interrupción por teclado detectada.")
    finally:
        # Cierre y limpieza garantizados
        print("\n=== LIMPIANDO Y CERRANDO RECURSOS ===")
        # Detener movimiento de la cámara por seguridad
        ptz.detener()
        # Detener hilo de lectura de video
        capture.stop()
        # Cerrar todas las ventanas de OpenCV
        cv2.destroyAllWindows()
        print("[INFO] Cierre limpio completado con éxito.")

if __name__ == "__main__":
    main()
