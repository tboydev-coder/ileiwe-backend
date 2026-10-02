import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from .core.config import get_settings
from .core.database import migrate, engine
from . import (
    auth,
    resources,
    students,
    attendance,
    results,
    finance,
    notifications,
    documents,
    reports,
    accounts,
    portals,
)

settings = get_settings()
logger = logging.getLogger("ile-iwe")
logging.basicConfig(level=settings.log_level, format="%(message)s")


@asynccontextmanager
async def lifespan(app):
    if settings.auto_migrate:
        migrate()
    yield


app = FastAPI(title="ile-iwe API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[s.strip() for s in settings.cors_origins.split(",") if s.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["X-Request-ID", "Content-Disposition"],
)
for router in (
    auth.router,
    resources.router,
    students.router,
    attendance.router,
    results.router,
    finance.router,
    notifications.router,
    documents.router,
    reports.router,
    accounts.router,
    portals.router,
):
    app.include_router(router, prefix="/api/v1")

limits = defaultdict(lambda: [0, 0.0])
rate_lock = Lock()


def error(status, message, code=None):
    return JSONResponse(
        {
            "error": {
                "code": code
                or {
                    401: "UNAUTHENTICATED",
                    403: "FORBIDDEN",
                    404: "NOT_FOUND",
                    409: "CONFLICT",
                    422: "VALIDATION_ERROR",
                    429: "RATE_LIMITED",
                }.get(status, "REQUEST_ERROR"),
                "message": message,
            }
        },
        status_code=status,
    )


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    if isinstance(exc.detail, dict):
        return error(exc.status_code, exc.detail["message"], exc.detail["code"])
    return error(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return error(422, "; ".join(".".join(map(str, e["loc"][1:])) + ": " + e["msg"] for e in exc.errors()))


@app.exception_handler(IntegrityError)
async def constraint_error(request, exc):
    return error(409, "This record conflicts with an existing record or database constraint.")


@app.exception_handler(Exception)
async def internal_error(request, exc):
    logger.error(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "ERROR",
                "service": "api",
                "request_id": getattr(request.state, "request_id", None),
                "event": "unhandled_error",
                "error_type": type(exc).__name__,
            }
        )
    )
    return error(500, "An unexpected error occurred. Please try again.", "INTERNAL_ERROR")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    started = time.monotonic()
    sensitive = request.url.path.startswith("/api/v1/auth/") and request.method == "POST"
    scan = request.url.path == "/api/v1/attendance/scan"
    if sensitive or scan:
        # Trust forwarded addresses only when Uvicorn is configured for a trusted proxy.
        identity = request.client.host if request.client else "unknown"
        key = f"ile-iwe:rate:{identity}:{'auth' if sensitive else 'scan'}"
        ceiling = 20 if sensitive else 120
        with rate_lock:
            current = time.monotonic()
            if len(limits) > 10000:
                for stale in [k for k, v in limits.items() if v[1] < current]:
                    del limits[stale]
            if limits[key][1] < current:
                limits[key] = [0, current + 60]
            limits[key][0] += 1
            hits = limits[key][0]
        if hits > ceiling:
            return error(429, "Too many requests. Try again in a minute.")
    response = await call_next(request)
    response.headers.update(
        {
            "X-Request-ID": request_id,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        }
    )
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    logger.info(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "INFO",
                "service": "api",
                "request_id": request_id,
                "user_id": getattr(request.state, "user_id", None),
                "school_id": getattr(request.state, "school_id", None),
                "event": "http_request",
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
    )
    return response


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "ile-iwe-api"}


@app.get("/ready", tags=["Health"])
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return error(503, "A required service is unavailable.", "NOT_READY")
    return {
        "status": "ready",
        "database": "connected",
    }
