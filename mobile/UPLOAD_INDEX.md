# Fix the UI

`mobile/index.html` on GitHub is **broken** (HTML tags were stripped).

Replace it with the real file from the zip:

1. Download `NexGene_Live_Research_Experiment_v0_1.zip`
2. Extract `mobile/index.html` (must start with `<!doctype html>` and be ~7800 bytes)
3. On GitHub: open `mobile/index.html` → Edit (pencil) → paste the full file → Commit
4. Render → Manual Deploy
5. Hard-refresh the site

`styles.css` (~11247 bytes) and `app.js` (~15970 bytes) should already be correct.
