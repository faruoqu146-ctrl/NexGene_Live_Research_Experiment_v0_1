"""Optional frontend tweaks at image build time. Never fails the build."""
from pathlib import Path

p = Path("/app/mobile/app.js")
if not p.exists():
    print("app.js missing; skip")
    raise SystemExit(0)

s = p.read_text(encoding="utf-8")
old = "if(!r.ok){setMsg('authmsg',d.detail||'Could not authenticate.');return}"
new = (
    "if(!r.ok){"
    "const msg=typeof d.detail==='string'?d.detail:"
    "(Array.isArray(d.detail)?d.detail.map(x=>x.msg||JSON.stringify(x)).join('; '):"
    "('HTTP '+r.status+' - could not authenticate'));"
    "setMsg('authmsg',msg);return}"
)
if old in s:
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("app.js auth error message improved")
else:
    print("app.js pattern not found; left unchanged")
