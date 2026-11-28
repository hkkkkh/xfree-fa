FROM python:3.12-slim
WORKDIR /app
# v3.19: ffmpeg for the transcode-once HLS light renditions
# (240/360/480p + 360p MP4). Debian bookworm ffmpeg 5.1: the
# filtergraph used here needs nothing newer (verified against the
# same command on ffmpeg 8.1.2 locally). No other packages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY proxy/ ./proxy/
EXPOSE 10000
CMD ["python", "proxy/app.py"]
