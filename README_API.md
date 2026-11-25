# SpotLoc API

This API wraps the existing Apify → Ollama → Nominatim pipeline. It calls the existing Python functions directly and does not read or overwrite the standalone scripts' JSON files during requests.

## Run locally

From `D:\Spotloc`, with Ollama running and the configured `qwen3:4b` model available:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-api.txt
$env:SPOTLOC_SECRET_KEY = "replace-with-a-long-random-secret"
.\venv\Scripts\python.exe -m uvicorn spotloc_api:app --host 0.0.0.0 --port 8000
```

`APIFY_TOKEN` continues to be read server-side from the existing `.env`. Keep that file private. `SPOTLOC_SECRET_KEY` must be set to a long random value in any persistent deployment; without it, the API creates a temporary secret at startup and sessions stop working after a restart. The SQLite database defaults to `spotloc.db`; set `SPOTLOC_DB_PATH` to choose another location. Never put these values in the mobile app.

On a phone using the same Wi-Fi, configure the app to call `http://<computer-lan-ip>:8000`. Capacitor Android uses the `https://localhost` WebView origin, which is allowed by default. For production, set `SPOTLOC_CORS_ORIGINS` to the exact web origins you deploy.

## API

- `POST /api/auth/register` and `POST /api/auth/login` create accounts and return a bearer token.
- `POST /api/auth/forgot-password` sends a one-hour single-use reset link; `POST /api/auth/reset-password` consumes it. Configure `SPOTLOC_SMTP_HOST`, `SPOTLOC_SMTP_FROM`, `SPOTLOC_WEB_URL`, and the optional SMTP port/user/password/SSL variables. Recovery returns a clear 503 until SMTP is configured.
- `GET /api/me` returns plan, saved Reel count, and remaining slots.
- `POST /api/reels/analyze` runs the existing extraction, LLM, and geocoding pipeline synchronously for the web client.
- `POST /api/reel-jobs` creates or reuses a persistent background Reel job and returns its ID immediately.
- `GET /api/reel-jobs` lists the signed-in user’s recent jobs; `GET /api/reel-jobs/{job_id}` returns state and the completed result.
- Background jobs use the same existing Apify, Ollama, and geocoding functions and resume queued/in-progress jobs after an API restart.
- `POST /api/reels/save` stores one primary business/location for a Reel. The request must come from an authenticated user. A `BEGIN IMMEDIATE` SQLite transaction checks the per-user entitlement and count before insert, so concurrent sixth saves cannot pass the free cap.
- `GET /api/reels` and `DELETE /api/reels/{reel_id}` only expose or remove the authenticated user's records. Deleting a record releases one slot.
- `GET/PATCH /api/preferences` stores nearby alert settings and a radius; clients should only request foreground location when the user enables nearby alerts.
- `DELETE /api/me` deletes the account and its associated data after password confirmation.
- `GET /health` is a non-sensitive health check.

Free accounts are capped at five saved Reel records. The limit counts one primary business/location per Reel, even if the intelligence result mentions other places. A `premium` entitlement is represented separately on the user record, but there is no public entitlement-changing route or payment integration in this MVP.

Geocoder results are coordinates only. They are never marked verified automatically. Clients should display the returned review status and let the user confirm or correct details before saving. The API intentionally stores only the primary extracted business and location, not comments, likes, follower counts, or live user coordinates.

## MVP deployment notes

This first API layer uses SQLite and in-memory per-process rate limiting. It is suitable for local development or a single API instance, not a horizontally scaled public service. Before public launch, add email verification and password recovery, a durable shared rate limiter, HTTPS, backups, operational monitoring, and a reviewed production database deployment. Keep Ollama and Nominatim calls on the server; do not expose their credentials or the Apify token to the app.
