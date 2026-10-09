# Render live test — Live Research Experiment v0.1

## 1. Deploy

1. Connect this repo in [Render](https://dashboard.render.com) → **New → Blueprint** (uses `render.yaml`), or **New → Web Service** with Docker.
2. Set env vars (Blueprint marks some as sync:false — enter them in the dashboard):
   - `NCBI_EMAIL` = real contact email (required by NCBI)
   - `CROSSREF_MAILTO` = real contact email
   - Optional: `NCBI_API_KEY`
3. Deploy. Wait until health is green: `GET /api/v1/health`

Free plan uses ephemeral SQLite under `/tmp` (data lost on restart). Cold starts can take 30–60s.

## 2. Full live test protocol

Replace `BASE` with your Render URL.

```bash
BASE=https://nexgene-live-research.onrender.com
JAR=/tmp/nexgene_render.jar
rm -f "$JAR"

# Health
curl -sS "$BASE/api/v1/health"; echo

# Register (DEV_MODE returns verification token)
REG=$(curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d '{"email":"live-research@example.com","password":"TestPass123!@#"}')
echo "$REG"
TOKEN=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('dev_verification_token',''))" <<<"$REG")

# Verify email (if token present)
if [ -n "$TOKEN" ]; then
  curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/auth/verify-email?token=$TOKEN"; echo
fi

# Login (refreshes session + CSRF cookies)
curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"live-research@example.com","password":"TestPass123!@#"}'; echo

# CSRF from cookie
CSRF=$(python3 -c "
import http.cookiejar
cj=http.cookiejar.MozillaCookieJar('$JAR'); cj.load(ignore_discard=True, ignore_expires=True)
for c in cj:
    if c.name=='nexgene_csrf': print(c.value); break
")
echo "CSRF=$CSRF"

# Seed 14 days of longitudinal observations (DEV_MODE only)
curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/dev/seed-longitudinal?days=14" \
  -H "X-CSRF-Token: $CSRF"; echo

# Run Intelligence Engine → structured hypotheses
curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/intelligence/engine/run" \
  -H "X-CSRF-Token: $CSRF"; echo

# Inspect hypotheses
curl -sS -c "$JAR" -b "$JAR" "$BASE/api/v1/intelligence/hypotheses?limit=10"; echo

# LIVE RESEARCH EXPERIMENT
curl -sS -c "$JAR" -b "$JAR" -X POST "$BASE/api/v1/intelligence/research/run" \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"max_queries":3}'; echo

# Evidence snapshots (user-scoped, hashed)
curl -sS -c "$JAR" -b "$JAR" "$BASE/api/v1/evidence/snapshots"; echo
```

## 3. Exit checks

- `experiment_version` == `lre-0.1`
- `model_interpretation` == `not_performed`
- Each result has `source`, `title`, `url`, `retrieved_at`, `content_hash`
- PubMed and/or Crossref return under live network conditions
- Second account cannot read first account snapshots
- Rate limit: 3 research runs / IP / window
