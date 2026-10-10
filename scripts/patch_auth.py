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

def _safe_verify(password: str, password_hash: str) -> bool:
    """Never let passlib/base64 errors become 500s during login."""
    if not password_hash or not isinstance(password_hash, str):
        return False
    try:
        return bool(pwd.verify(password, password_hash))
    except Exception:
        return False

'''

if "_to_aware" not in src:
    src = src.replace(
        "def now():\n    return datetime.now(timezone.utc)\n",
        "def now():\n    return datetime.now(timezone.utc)\n" + helper,
        1,
    )
elif "_safe_verify" not in src:
    src = src.replace(
        "def _to_aware(dt):",
        helper.strip().split("def _to_aware(dt):", 1)[0] + "def _to_aware(dt):",
        1,
    )
    # If that failed to insert _safe_verify, inject after _to_aware block end is hard;
    # fall through to simple insert near get_session.
    if "_safe_verify" not in src:
        src = src.replace(
            "def get_session(request: Request, s: Session):",
            "def _safe_verify(password: str, password_hash: str) -> bool:\n"
            "    if not password_hash or not isinstance(password_hash, str):\n"
            "        return False\n"
            "    try:\n"
            "        return bool(pwd.verify(password, password_hash))\n"
            "    except Exception:\n"
            "        return False\n\n\n"
            "def get_session(request: Request, s: Session):",
            1,
        )

src = src.replace("st.expires_at < now()", "_to_aware(st.expires_at) < now()")
src = src.replace("row.expires_at < now()", "_to_aware(row.expires_at) < now()")

src = src.replace(
    "pbkdf2_sha256__default_rounds=600000",
    "pbkdf2_sha256__default_rounds=(80000 if DEV_MODE else 600000)",
    1,
)

# Harden login/register password checks
src = src.replace(
    "pwd.verify(x.password, DUMMY_PASSWORD_HASH)",
    "_safe_verify(x.password, DUMMY_PASSWORD_HASH)",
)
src = src.replace(
    "if not pwd.verify(x.password, u.password_hash):",
    "if not _safe_verify(x.password, u.password_hash):",
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

if src == orig:
    print("WARN: no changes applied")
else:
    p.write_text(src, encoding="utf-8")
    print("patched OK")
    print("safe_verify", "_safe_verify" in src)
    print("to_aware", src.count("_to_aware"))
