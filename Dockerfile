FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY mobile /app/mobile
COPY scripts /app/scripts

# Drop leftover UI base64 fragments (caused Invalid base64-encoded string errors)
RUN rm -f /app/mobile/index.b64.* /app/mobile/styles_a.css /app/mobile/styles_b.css \
          /app/mobile/app_a.js /app/mobile/app_b.js /app/mobile/UPLOAD_INDEX.md || true

# Auth/session reliability patches
RUN python /app/scripts/patch_auth.py || echo "patch_auth skipped"

# Clearer client-side auth errors
RUN python -c "from pathlib import Path;p=Path('/app/mobile/app.js');\n\
s=p.read_text() if p.exists() else '';old=\"if(!r.ok){setMsg('authmsg',d.detail||'Could not authenticate.');return}\";\n\
new=\"if(!r.ok){const msg=typeof d.detail==='string'?d.detail:(Array.isArray(d.detail)?d.detail.map(x=>x.msg||JSON.stringify(x)).join('; '):('HTTP '+r.status+' — could not authenticate'));setMsg('authmsg',msg);return}\";\n\
p.write_text(s.replace(old,new,1)) if old in s else None; print('app.js', 'patched' if old in s else 'skip')"

WORKDIR /app
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}
