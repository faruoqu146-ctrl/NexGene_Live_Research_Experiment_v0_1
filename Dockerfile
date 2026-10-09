FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend /app/backend
# Reassemble main.py if shipped as parts (GitHub push size workaround)
RUN if [ ! -f /app/backend/app/main.py ] && ls /app/backend/app/main_part_*.py >/dev/null 2>&1; then \
      cat /app/backend/app/main_part_*.py > /app/backend/app/main.py; \
    fi
COPY mobile /app/mobile
COPY scripts /app/scripts
WORKDIR /app
ENV PORT=8000
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}
