FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend /app/backend
COPY mobile /app/mobile
# Rebuild full index.html from base64 parts when present (avoids truncated HTML uploads)
RUN if ls /app/mobile/index.b64.* >/dev/null 2>&1; then \
      cat /app/mobile/index.b64.* | base64 -d > /app/mobile/index.html; \
    fi
COPY scripts /app/scripts
WORKDIR /app
ENV PORT=8000
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}
