"""Patch main.py for Render reliability."""
from pathlib import Path

p = Path("/app/backend/app/main.py")
if not p.exists():
    print("main.py missing; skip")
    raise SystemExit(0)

src = p.read_text(encoding="utf-8")
orig = src

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

src = src.replace("st.expires_at < now()", "_to_aware(st.expires_at) < now()")
src = src.replace("row.expires_at < now()", "_to_aware(row.expires_at) < now()")

# Free-tier safe hashing in DEV_MODE (was 600k — often times out)
src = src.replace(
    "pbkdf2_sha256__default_rounds=600000",
    "pbkdf2_sha256__default_rounds=(80000 if DEV_MODE else 600000)",
    1,
)

needle = 'DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nexgene.db")'
if needle in src and 'replace("postgres://"' not in src:
    src = src.replace(
        needle,
        needle
        + "\nif DATABASE_URL.startswith(\"postgres://\"):\n"
        + "    DATABASE_URL = DATABASE_URL.replace(\"postgres://\", \"postgresql://\", 1)",
        1,
    )

# Same for clinical/genomic if present
for key in ("CLINICAL_DATABASE_URL", "GENOMIC_DATABASE_URL"):
    n = f'{key} = os.getenv("{key}"' 
    # skip complex rewrite; main uses separate vars rarely as postgres on free tier

if src == orig:
    print("WARN: no changes applied")
else:
    p.write_text(src, encoding="utf-8")
    print("patched OK")
    print("to_aware", src.count("_to_aware"))
    print("rounds_dev80k", "80000 if DEV_MODE" in src)
