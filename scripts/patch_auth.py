"""Patch main.py for Render reliability: datetime compare + faster pbkdf2 in DEV_MODE."""
from pathlib import Path
import re

p = Path("/app/backend/app/main.py")
src = p.read_text(encoding="utf-8")
orig = src

# 1) Insert _to_aware helper after def now()
helper = '''
def _to_aware(dt):
    """SQLite returns naive datetimes; make them UTC-aware for safe compare."""
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

'''
if "_to_aware" not in src:
    src = src.replace(
        "def now():\n    return datetime.now(timezone.utc)\n",
        "def now():\n    return datetime.now(timezone.utc)\n" + helper,
        1,
    )

# 2) Fix get_session expires_at comparison (TypeError naive vs aware on SQLite)
src = src.replace(
    "if not st or st.expires_at < now():",
    "if not st or _to_aware(st.expires_at) < now():",
)

# 3) Lower pbkdf2 rounds in DEV_MODE (600k rounds can timeout on free tier)
src = src.replace(
    "pbkdf2_sha256__default_rounds=600000",
    "pbkdf2_sha256__default_rounds=(120000 if DEV_MODE else 600000)",
    1,
)

# 4) Normalize Render postgres:// URLs for SQLAlchemy
needle = 'DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nexgene.db")'
if needle in src and 'replace("postgres://"' not in src:
    src = src.replace(
        needle,
        needle + '\nif DATABASE_URL.startswith("postgres://"):\n    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)',
        1,
    )

if src == orig:
    print("WARN: no changes applied")
else:
    p.write_text(src, encoding="utf-8")
    print("patched", p)
    print("aware_helper", "_to_aware" in src)
    print("expires_fix", "_to_aware(st.expires_at)" in src)
    print("rounds", "120000 if DEV_MODE" in src)
