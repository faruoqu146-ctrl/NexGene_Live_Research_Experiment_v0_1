# Auth troubleshooting on Render

## 1. Confirm the API is up
Open in the browser:

```
https://YOUR-SERVICE.onrender.com/api/v1/health
```

You must see:
```json
{"status":"ok","version":"1.6.0"}
```

If you see **Not Found** or a Render error page, auth cannot work. Check **Render → Logs**.

## 2. Required env vars

| Key | Value |
|-----|--------|
| DEV_MODE | true |
| COOKIE_SECURE | true |
| DATABASE_URL | sqlite:////tmp/nexgene.db |
| GENOMIC_DATABASE_URL | sqlite:////tmp/nexgene_genomic.db |
| CLINICAL_DATABASE_URL | sqlite:////tmp/nexgene_clinical.db |
| SECRET_KEY | (auto-generated is fine) |

If a **Postgres** database is linked, either unlink it or keep `psycopg2-binary` installed (already in requirements).

## 3. Password rules
Must be 12+ characters with upper, lower, number, and symbol.

Example: `TestPass123!@#`

## 4. Browser Network tab
After clicking Sign in / Create account, open DevTools → Network → the `login` or `register` request:

- **404** → service not deployed / wrong URL
- **401** → wrong password or email
- **422** → validation (password rules)
- **429** → rate limited (wait a minute)
- **500** → check Render logs (DB / datetime)
- **502/504** → app crashed or timed out (hashing / cold start)

## 5. After a good deploy
Hard-refresh the site (or private window), then register a **new** email.
