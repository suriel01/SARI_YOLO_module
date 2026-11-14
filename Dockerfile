# Ultralytics image for NVIDIA Jetson (JetPack 6), pinned by tag AND digest.
# Bump deliberately: a new ultralytics/TensorRT version invalidates model_cache/yolo26n.engine.
FROM ultralytics/ultralytics:8.4.204-jetson-jetpack6@sha256:e42e5e4105ac36287d422bba214a96ee2d3901406817ed22dd52299922faa8e8

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Los_Angeles

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1-mesa-glx \
        libglib2.0-0 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Unprivileged runtime user. Code stays root-owned (read-only for the service); /app is
# group-writable with the sticky bit so YOLO can download weights without replacing code.
# The `video` group grants access to the Jetson GPU device nodes.
RUN groupadd --system --gid 10001 sari \
    && useradd --system --uid 10001 --gid sari --create-home --home-dir /home/sari --shell /usr/sbin/nologin sari \
    && (getent group video >/dev/null && usermod -aG video sari || true) \
    && mkdir -p /app/model_cache /app/alert_queue \
    && chown sari:sari /app/model_cache /app/alert_queue \
    && chown root:sari /app && chmod 1775 /app

COPY camara_ptz.py telegram_alert.py ./
COPY sari_eyes ./sari_eyes

USER sari

# Authenticated MJPEG/snapshot server (EYES_STREAM_TOKEN)
EXPOSE 8080

CMD ["python3", "camara_ptz.py"]
