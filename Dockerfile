FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY mobile /app/mobile
COPY scripts /app/scripts

# Drop leftover UI base64 fragments
RUN rm -f /app/mobile/index.b64.* \
          /app/mobile/styles_a.css /app/mobile/styles_b.css \
          /app/mobile/app_a.js /app/mobile/app_b.js \
          /app/mobile/UPLOAD_INDEX.md || true

# Auth + historical check-ins + data-anchored reports (never fail the build)
RUN python /app/scripts/patch_auth.py || echo "patch_auth skipped"
RUN python /app/scripts/patch_historical.py || echo "patch_historical skipped"
RUN python /app/scripts/patch_frontend.py || echo "patch_frontend skipped"

WORKDIR /app
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}
