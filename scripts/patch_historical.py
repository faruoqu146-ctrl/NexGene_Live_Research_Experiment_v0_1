"""Enable historical check-ins and downstream analysis on imported timelines.

- CheckinIn accepts optional recorded_at (ISO datetime)
- save_checkin persists that timestamp (UTC-aware)
- weekly report + 30-day quality windows anchor to latest observation
  so September data still produces reports when "now" is later
- SQLite naive/aware datetime compares via _to_aware
"""
from pathlib import Path

p = Path("/app/backend/app/main.py")
if not p.exists():
    print("main.py missing; skip")
    raise SystemExit(0)

src = p.read_text(encoding="utf-8")
orig = src

# --- _to_aware helper ---
if "_to_aware" not in src:
    helper = '''
def _to_aware(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

'''
    src = src.replace(
        "def now():\n    return datetime.now(timezone.utc)\n",
        "def now():\n    return datetime.now(timezone.utc)\n" + helper,
        1,
    )

# --- CheckinIn.recorded_at ---
old_cls = """class CheckinIn(BaseModel):
    values: dict[str, float | str]

    @model_validator(mode="after")
    def validate_values(self):"""
new_cls = """class CheckinIn(BaseModel):
    values: dict[str, float | str]
    recorded_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_values(self):"""
if old_cls in src and "class CheckinIn(BaseModel):\n    values: dict[str, float | str]\n    recorded_at:" not in src:
    src = src.replace(old_cls, new_cls, 1)

# --- save_checkin uses recorded_at ---
old_save = """def save_checkin(period, data, u, s):
    recorded = now()"""
new_save = """def save_checkin(period, data, u, s):
    recorded = data.recorded_at if getattr(data, "recorded_at", None) else now()
    if recorded is not None and getattr(recorded, "tzinfo", None) is None:
        recorded = recorded.replace(tzinfo=timezone.utc)"""
if old_save in src:
    src = src.replace(old_save, new_save, 1)

# --- session token expiry compares ---
src = src.replace("st.expires_at < now()", "_to_aware(st.expires_at) < now()")
src = src.replace("row.expires_at < now()", "_to_aware(row.expires_at) < now()")

# --- weekly_report: anchor to latest observation ---
old_weekly = """@app.get("/api/v1/reports/weekly")
def weekly_report(u: User = Depends(current_user), s: Session = Depends(db)):
    end = now()
    start = end - timedelta(days=7)
    baseline_start = end - timedelta(days=28)
    rows = s.scalars(
        select(Observation).where(
            Observation.user_id == u.id,
            Observation.recorded_at >= baseline_start,
        ).order_by(Observation.recorded_at.asc())
    ).all()
    week = [r for r in rows if _to_aware(r.recorded_at) >= start]"""

# Prefer already-patched week line variants
if "def weekly_report" in src and "_data_anchor_end" not in src:
    old_weekly_simple = '''@app.get("/api/v1/reports/weekly")
def weekly_report(u: User = Depends(current_user), s: Session = Depends(db)):
    end = now()
    start = end - timedelta(days=7)
    baseline_start = end - timedelta(days=28)'''
    new_weekly_simple = '''@app.get("/api/v1/reports/weekly")
def weekly_report(u: User = Depends(current_user), s: Session = Depends(db)):
    # Anchor to latest observation so historical imports still produce reports
    latest = s.scalar(select(func.max(Observation.recorded_at)).where(Observation.user_id == u.id))
    end = _to_aware(latest) if latest is not None else now()
    start = end - timedelta(days=7)
    baseline_start = end - timedelta(days=28)'''
    if old_weekly_simple in src:
        src = src.replace(old_weekly_simple, new_weekly_simple, 1)

# week filter (handle both patched and unpatched forms)
if "week = [r for r in rows if r.recorded_at >= start]" in src:
    src = src.replace(
        "week = [r for r in rows if r.recorded_at >= start]",
        "week = [r for r in rows if _to_aware(r.recorded_at) >= start]",
    )

# --- data-quality / insights windows: anchor to data end when present ---
old_dq = '''@app.get("/api/v1/intelligence/data-quality")
def intelligence_data_quality(u: User = Depends(current_user), s: Session = Depends(db)):
    end = now()
    since = end - timedelta(days=30)'''
new_dq = '''@app.get("/api/v1/intelligence/data-quality")
def intelligence_data_quality(u: User = Depends(current_user), s: Session = Depends(db)):
    latest = s.scalar(select(func.max(Observation.recorded_at)).where(Observation.user_id == u.id))
    end = _to_aware(latest) if latest is not None else now()
    since = end - timedelta(days=30)'''
if old_dq in src:
    src = src.replace(old_dq, new_dq, 1)

# Broader recorded_at compares (idempotent-ish)
for a, b in [
    ("r.recorded_at >= since", "_to_aware(r.recorded_at) >= since"),
    ("r.recorded_at >= start", "_to_aware(r.recorded_at) >= start"),
    ("r.recorded_at >= recent_start", "_to_aware(r.recorded_at) >= recent_start"),
    ("prior_start <= r.recorded_at < recent_start", "prior_start <= _to_aware(r.recorded_at) < recent_start"),
    ("baseline_start <= r.recorded_at < start", "baseline_start <= _to_aware(r.recorded_at) < start"),
]:
    if b not in src:
        src = src.replace(a, b)

if src == orig:
    print("WARN: no historical patches applied")
else:
    p.write_text(src, encoding="utf-8")
    print("patch_historical OK")
    print("checkin_recorded_at", "recorded_at: Optional[datetime]" in src and "def save_checkin" in src)
    print("weekly_anchor", "_data_anchor" in src or "latest = s.scalar(select(func.max" in src)
