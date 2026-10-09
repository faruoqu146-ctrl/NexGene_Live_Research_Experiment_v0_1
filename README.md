# NexGene Live Research Experiment v0.1

Bounded scholarly retrieval over structured personal longitudinal hypotheses.

- **Endpoint:** `POST /api/v1/intelligence/research/run`
- **Providers:** PubMed (NCBI E-utilities) + Crossref works metadata
- **Experiment version:** `lre-0.1`
- **Boundary:** metadata-only; no LLM planner; no clinical interpretation; no hypothesis mutation

## Render deploy

See [docs/RENDER_LIVE_TEST.md](docs/RENDER_LIVE_TEST.md).

1. Open [Render Dashboard](https://dashboard.render.com) → **New → Blueprint** → select this repo (uses `render.yaml`).
2. Set secrets:
   - `NCBI_EMAIL` (required, real contact email)
   - `CROSSREF_MAILTO` (required)
   - Optional `NCBI_API_KEY`
3. Deploy and wait for `/api/v1/health`.

## Local

```bash
docker compose up --build
# or
PYTHONPATH=. uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
pytest backend/tests
```

## Live research flow

1. Register / login (session + CSRF cookies)
2. `POST /api/v1/dev/seed-longitudinal` (DEV_MODE only) — 14 days of synthetic observations
3. `POST /api/v1/intelligence/engine/run` — form hypotheses
4. `POST /api/v1/intelligence/research/run` — live PubMed/Crossref retrieval
5. `GET /api/v1/evidence/snapshots` — user-scoped hashed snapshots

## Important

Ensure `backend/app/main.py` and `mobile/` are present in the repo before deploy. If this branch was created partially, upload the full project tree from the release zip so Docker build succeeds.
