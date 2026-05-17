FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Cloud Run sets PORT env var; fallback to 8080 for local
ENV PORT=8080
EXPOSE 8080

# Healthcheck-friendly startup
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info
