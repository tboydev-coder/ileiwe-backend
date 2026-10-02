# Standalone API

This directory contains the complete FastAPI backend, migrations, database access, storage adapters, worker, tests, and Docker infrastructure. It can be copied/deployed without `frontend/` or any root-level tooling.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
# Edit DATABASE_URL for your existing PostgreSQL database,
# or use sqlite:///./ile-iwe.db for local development.
python run.py
```

The API listens on `http://127.0.0.1:8000` by default. `/docs` provides interactive API documentation; `/health` reports liveness and `/ready` checks database readiness. Startup applies Alembic migrations. No frontend process or build is required. Run `python -m app.worker` separately to process queued documents and notifications.

`.env.example` is the single configuration reference. Process variables override `.env`, including `PORT` and `FORWARDED_ALLOW_IPS`. Relative database and storage paths resolve from this directory. `FRONTEND_URL` supplies links for emails/QR codes, while `CORS_ORIGINS` permits browser origins; neither requires a frontend server at backend startup.

Alternatively, after creating `.env`, run `docker compose up -d --build` here for PostgreSQL, API, and worker. Compose uses its internal database service and exposes the API on `PORT`. Run the frontend independently with its own public `VITE_API_URL`.

Verification is optional: `python -m pytest -q` uses temporary SQLite data. Export `ILE_TEST_DATABASE_URL` only for a disposable PostgreSQL database because fixtures clear its application data. Tests and `.verification/` artifacts are excluded from the backend container.
