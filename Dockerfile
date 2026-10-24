# Stage 1: build the phone page (React + shadcn/ui) into yotobridge/static
FROM node:22-alpine AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN mkdir -p ../yotobridge && npm run build

# Stage 2: the bridge
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY yotobridge ./yotobridge
COPY --from=web /src/yotobridge/static ./yotobridge/static
ENV BRIDGE_CONFIG=/data/config.yaml PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
# /s/health answers without a PIN; assumes the default listen_port 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/s/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "yotobridge"]
