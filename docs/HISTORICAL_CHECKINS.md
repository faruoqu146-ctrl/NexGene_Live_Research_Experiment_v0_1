# Historical check-ins

NexGene can accept **past observation timestamps** so synthetic or migrated timelines analyze correctly.

## API

`POST /api/v1/checkins/morning` and `POST /api/v1/checkins/evening`

```json
{
  "values": {
    "sleep_duration": 7.2,
    "energy": 6,
    "morning_context": "none"
  },
  "recorded_at": "2026-09-15T08:00:00+00:00"
}
```

- `values` — only kinds in `ALLOWED_OBSERVATIONS`
- `recorded_at` — optional ISO-8601 datetime (UTC preferred). If omitted, server uses "now".

Evening example:

```json
{
  "values": {
    "mood": 7,
    "stress": 4,
    "focus": 6,
    "activity_duration": 30,
    "activity_level": 5,
    "diet_quality": 6,
    "caffeine": 1,
    "nicotine": "none",
    "alcohol": "none"
  },
  "recorded_at": "2026-09-15T21:00:00+00:00"
}
```

Requires auth + `X-CSRF-Token` header (same as other mutating routes).

## Downstream analysis

After import:

1. `POST /api/v1/intelligence/engine/run` — builds lagged hypotheses on the full timeline
2. `GET /api/v1/patterns` — daily series by `recorded_at`
3. `GET /api/v1/signals` / `insights` / `brief`
4. `GET /api/v1/reports/weekly` — uses the **latest observation date** as the end of the week (not wall-clock "today"), so historical months still produce a report

## Notes

- SQLite free-tier data under `/tmp` is wiped on instance restart; use Postgres for durable history.
- Do not invent observation kinds; unsupported fields are rejected.
