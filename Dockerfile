FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend /app/backend
COPY mobile /app/mobile
COPY scripts /app/scripts
# Auth/session reliability for Render (SQLite datetime + DEV_MODE hash cost)
RUN python /app/scripts/patch_auth.py
WORKDIR /app
ENV PORT=8000
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}
