# Utilizamos la imagen oficial de Ultralytics optimizada para NVIDIA Jetson con JetPack 6
FROM ultralytics/ultralytics:latest-jetson-jetpack6

# Variables de entorno
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Los_Angeles

# Instalar dependencias del sistema requeridas para OpenCV y utilidades de red
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Configurar directorio de trabajo
WORKDIR /app

# Instalar dependencias de Python
RUN pip3 install --no-cache-dir \
    ultralytics \
    opencv-python-headless \
    websockets \
    requests \
    flask

# Copiar el código fuente
COPY camara_ptz.py .
COPY telegram_alert.py .

# Puerto expuesto para servidor de video MJPEG
EXPOSE 8080

# Comando por defecto
CMD ["python3", "camara_ptz.py"]
