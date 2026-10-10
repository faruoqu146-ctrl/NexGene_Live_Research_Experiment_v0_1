FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY mobile /app/mobile
COPY scripts /app/scripts

# Apply runtime patches (datetime + hash cost). Never fail the image build on patch.
RUN python /app/scripts/patch_auth.py || echo "patch_auth skipped"

# Clearer client-side auth errors (show HTTP status / validation detail)
RUN python - <<'PY'
from pathlib import Path
p = Path("/app/mobile/app.js")
if not p.exists():
    raise SystemExit(0)
s = p.read_text(encoding="utf-8")
old = "if(!r.ok){setMsg('authmsg',d.detail||'Could not authenticate.');return}"
new = (
    "if(!r.ok){"
    "const msg=typeof d.detail==='string'?d.detail:"
    "(Array.isArray(d.detail)?d.detail.map(x=>x.msg||JSON.stringify(x)).join('; '):"
    "('HTTP '+r.status+' — could not authenticate'));"
    "setMsg('authmsg',msg);return}"
)
if old in s:
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("app.js auth error message improved")
else:
    print("app.js pattern not found; left unchanged")
PY

WORKDIR /app
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Shell form so Render's $PORT is expanded
CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}
