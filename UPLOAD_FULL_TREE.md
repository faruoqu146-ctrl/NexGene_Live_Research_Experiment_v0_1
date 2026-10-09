# Complete the repo before Render deploy

This GitHub repository was scaffolded with Render config (`render.yaml`, `Dockerfile`), the live research planner, tests, and docs.

**You still need the full application tree on `main`:**

- `backend/app/main.py` (~140 KB)
- `mobile/index.html`, `mobile/app.js`, `mobile/styles.css`
- remaining `backend/tests/*`, `docs/*`, `scripts/*`

## Fastest path

From the machine that has `NexGene_Live_Research_Experiment_v0_1.zip`:

```bash
unzip NexGene_Live_Research_Experiment_v0_1.zip -d nexgene
cd nexgene
# keep the Render files if already on GitHub, or copy them in:
# Dockerfile (PORT-aware), render.yaml, docs/RENDER_LIVE_TEST.md
# and the DEV seed route in main.py if you want the automated protocol

git init
git remote add origin https://github.com/faruoqu146-ctrl/NexGene_Live_Research_Experiment_v0_1.git
git fetch origin
git checkout -B main
# add all files
git add -A
git commit -m "Complete Live Research Experiment v0.1 for Render"
git push -u origin main --force
```

Then open Render → Blueprint → this repo → set `NCBI_EMAIL` + `CROSSREF_MAILTO` → deploy.

Full curl protocol: `docs/RENDER_LIVE_TEST.md`.
