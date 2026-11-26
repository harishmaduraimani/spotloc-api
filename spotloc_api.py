from __future__ import annotations
from pathlib import Path
import hashlib
import smtplib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from urllib.parse import quote
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

import extract_reel
import location_processor
import spotloc_agent


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SPOTLOC_DB_PATH", str(ROOT / "spotloc.db")))
FREE_REEL_LIMIT = 5
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7
TOKEN_SECRET = os.getenv("SPOTLOC_SECRET_KEY") or secrets.token_urlsafe(48)
bearer = HTTPBearer(auto_error=False)
app = FastAPI(title="SpotLoc API", version="1.0.0")
logger = logging.getLogger("spotloc.api")
allowed_origins = [
    x.strip()
    for x in os.getenv(
        "SPOTLOC_CORS_ORIGINS",
        "http://localhost:8081,http://localhost:19006,https://localhost,capacitor://localhost"
    ).split(",")
    if x.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"]
)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def initialize_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free' CHECK(plan IN ('free','premium')),
            subscription_status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reels (
            reel_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            reel_url TEXT NOT NULL,
            instagram_username TEXT,
            business_name TEXT,
            category TEXT,
            address TEXT,
            location_json TEXT NOT NULL,
            verification_status TEXT NOT NULL DEFAULT 'needs_confirmation',
            user_confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, reel_url)
        );
        CREATE INDEX IF NOT EXISTS reels_user_created ON reels(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS preferences (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            nearby_notifications_enabled INTEGER NOT NULL DEFAULT 0,
            proximity_radius_m INTEGER NOT NULL DEFAULT 1000
        );
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reel_jobs (
            job_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            reel_url TEXT NOT NULL,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            result_json TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, reel_url)
        );
        CREATE INDEX IF NOT EXISTS reel_jobs_user_created ON reel_jobs(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


initialize_db()


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


class AnalyzeRequest(BaseModel):
    reel_url: str = Field(min_length=12, max_length=2048)

    @field_validator("reel_url")
    @classmethod
    def validate_instagram_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {
                "instagram.com",
                "www.instagram.com",
                "m.instagram.com",
                "instagr.am",
                "www.instagr.am"
            }
            or not re.search(r"/(reel|reels|p)/[^/]+", parsed.path)
        ):
            raise ValueError("Enter a valid public Instagram Reel URL.")
        return value.strip().split("?")[0].rstrip("/") + "/"


class SaveRequest(BaseModel):
    reel_url: str = Field(min_length=12, max_length=2048)
    business_name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    instagram_username: str | None = Field(default=None, max_length=100)
    address: str = Field(min_length=1, max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    verification_status: str = Field(
        default="needs_confirmation",
        pattern="^(needs_confirmation|user_confirmed)$"
    )
    user_confirmed: bool = False

    @field_validator("reel_url")
    @classmethod
    def validate_reel_url(cls, value: str) -> str:
        return AnalyzeRequest.validate_instagram_url(value)


class PreferenceUpdate(BaseModel):
    nearby_notifications_enabled: bool
    proximity_radius_m: int = Field(default=1000, ge=100, le=10000)


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=10, max_length=128)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}:{digest.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        return hmac.compare_digest(
            hash_password(password, bytes.fromhex(salt_hex)),
            f"{salt_hex}:{digest_hex}"
        )
    except (ValueError, TypeError):
        return False


def issue_token(user_id: str) -> str:
    expires = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{user_id}.{expires}"
    signature = hmac.new(
        TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def current_user(
    auth: HTTPAuthorizationCredentials | None = Depends(bearer)
) -> dict[str, Any]:
    if not auth or auth.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    try:
        user_id, expiry, signature = auth.credentials.rsplit(".", 2)
        payload = f"{user_id}.{expiry}"
        expected = hmac.new(
            TOKEN_SECRET.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        if int(expiry) < time.time() or not hmac.compare_digest(signature, expected):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="Your session has expired. Sign in again."
        )

    with connect() as db:
        user = db.execute(
            """
            SELECT user_id,email,plan,subscription_status,created_at
            FROM users WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Your session is no longer valid."
        )

    return dict(user)


_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}
_geocode_lock = threading.Lock()
_last_geocode_at = 0.0


def rate_limit(key: str, max_hits: int, period_seconds: int) -> None:
    now = time.monotonic()

    with _rate_lock:
        hits = [
            hit for hit in _rate_hits.get(key, [])
            if now - hit < period_seconds
        ]

        if len(hits) >= max_hits:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again shortly."
            )

        hits.append(now)
        _rate_hits[key] = hits


def geocode_cached(address: str) -> dict[str, Any]:
    global _last_geocode_at

    key = " ".join(address.lower().split())

    with connect() as db:
        cached = db.execute(
            "SELECT result_json FROM geocode_cache WHERE address_key=?",
            (key,)
        ).fetchone()

    if cached:
        return json.loads(cached[0])

    # Nominatim's public service expects no more than one request per second.
    with _geocode_lock:
        delay = 1.0 - (time.monotonic() - _last_geocode_at)

        if delay > 0:
            time.sleep(delay)

        result = location_processor.geocode_address(address, verbose=False)
        _last_geocode_at = time.monotonic()

    if result.get("geocoding_status") != "error":
        with connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO geocode_cache
                (address_key,result_json,created_at)
                VALUES(?,?,?)
                """,
                (key, json.dumps(result), now_iso())
            )

    return result


def safe_pipeline_error(error: Exception, stage: str) -> HTTPException:
    message = str(error).lower()

    if stage == "extract":
        if (
            "apify_token" in message
            or "not configured" in message
            or "unauthorized" in message
            or "forbidden" in message
        ):
            return HTTPException(
                status_code=503,
                detail="Reel extraction is not available on this server right now. Please try again later."
            )

        if "timeout" in message or "timed out" in message:
            return HTTPException(
                status_code=504,
                detail="Instagram took too long to respond. Please try this Reel again."
            )

        return HTTPException(
            status_code=502,
            detail="Instagram could not provide this Reel to SpotLoc. Check that the Reel is public and try again."
        )

    if "connection" in message or "refused" in message or "ollama" in message:
        return HTTPException(
            status_code=503,
            detail="SpotLoc's location analyzer is unavailable. Please try again later."
        )

    if "timeout" in message or "timed out" in message:
        return HTTPException(
            status_code=504,
            detail="Location analysis took too long. Please try again."
        )

    if "model" in message and ("not found" in message or "pull" in message):
        return HTTPException(
            status_code=503,
            detail="SpotLoc's location analyzer needs setup. Please try again later."
        )

    return HTTPException(
        status_code=502,
        detail="SpotLoc could not analyze the place information in this Reel. Please try another public Reel."
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=201)
def register(body: Credentials, request: Request) -> dict[str, Any]:
    rate_limit(
        f"register:{request.client.host if request.client else 'unknown'}",
        5,
        3600
    )

    user_id = secrets.token_urlsafe(18)
    created_at = now_iso()

    try:
        with connect() as db:
            db.execute(
                """
                INSERT INTO users(user_id,email,password_hash,created_at)
                VALUES(?,?,?,?)
                """,
                (
                    user_id,
                    body.email.lower(),
                    hash_password(body.password),
                    created_at
                )
            )
            db.execute(
                "INSERT INTO preferences(user_id) VALUES(?)",
                (user_id,)
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists."
        )

    return {
        "access_token": issue_token(user_id),
        "token_type": "bearer",
        "user": {
            "user_id": user_id,
            "email": body.email.lower(),
            "plan": "free",
            "subscription_status": "active"
        }
    }


@app.post("/api/auth/login")
def login(body: Credentials, request: Request) -> dict[str, Any]:
    rate_limit(
        f"login:{request.client.host if request.client else 'unknown'}",
        10,
        900
    )

    with connect() as db:
        row = db.execute(
            """
            SELECT user_id,email,password_hash,plan,subscription_status
            FROM users WHERE email=? COLLATE NOCASE
            """,
            (body.email,)
        ).fetchone()

    if not row or not check_password(body.password, row["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="Email or password is incorrect."
        )

    user = {
        key: row[key]
        for key in ("user_id", "email", "plan", "subscription_status")
    }

    return {
        "access_token": issue_token(user["user_id"]),
        "token_type": "bearer",
        "user": user
    }


@app.post("/api/auth/forgot-password")
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request
) -> dict[str, str]:
    client = request.client.host if request.client else "unknown"
    rate_limit(f"password-reset:{client}", 5, 3600)

    smtp_host = os.getenv("SPOTLOC_SMTP_HOST")
    sender = os.getenv("SPOTLOC_SMTP_FROM")
    web_url = os.getenv("SPOTLOC_WEB_URL")

    if not smtp_host or not sender or not web_url:
        raise HTTPException(
            status_code=503,
            detail="Password recovery email is not configured on this SpotLoc server."
        )

    with connect() as db:
        user = db.execute(
            "SELECT user_id,email FROM users WHERE email=? COLLATE NOCASE",
            (body.email,)
        ).fetchone()

        if not user:
            return {
                "message": "If an account exists for that email, a reset link has been sent."
            }

        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()

        db.execute(
            "DELETE FROM password_reset_tokens WHERE user_id=?",
            (user["user_id"],)
        )
        db.execute(
            """
            INSERT INTO password_reset_tokens
            (token_hash,user_id,expires_at,created_at)
            VALUES(?,?,?,?)
            """,
            (token_hash, user["user_id"], expires_at, now_iso())
        )

    reset_url = web_url.rstrip("/") + "/?reset_token=" + quote(token)

    message = EmailMessage()
    message["Subject"] = "Reset your SpotLoc password"
    message["From"] = sender
    message["To"] = user["email"]
    message.set_content(
        "Use this one-time link within one hour to reset your SpotLoc password:\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, ignore this email."
    )

    try:
        port = int(os.getenv("SPOTLOC_SMTP_PORT", "587"))
        username = os.getenv("SPOTLOC_SMTP_USER")
        password = os.getenv("SPOTLOC_SMTP_PASSWORD")

        if os.getenv("SPOTLOC_SMTP_USE_SSL", "false").lower() == "true":
            with smtplib.SMTP_SSL(smtp_host, port, timeout=20) as server:
                if username:
                    server.login(username, password or "")
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                server.starttls()
                if username:
                    server.login(username, password or "")
                server.send_message(message)

    except Exception as error:
        logger.error(
            "Password reset email failed (%s)",
            type(error).__name__
        )
        with connect() as db:
            db.execute(
                "DELETE FROM password_reset_tokens WHERE token_hash=?",
                (token_hash,)
            )
        raise HTTPException(
            status_code=503,
            detail="SpotLoc could not send the reset email. Try again later or contact support."
        ) from error

    return {
        "message": "If an account exists for that email, a reset link has been sent."
    }


@app.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordRequest) -> dict[str, str]:
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()

    with connect() as db:
        row = db.execute(
            """
            SELECT user_id FROM password_reset_tokens
            WHERE token_hash=? AND expires_at>?
            """,
            (token_hash, now_iso())
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=400,
                detail="This password reset link is invalid or has expired. Request a new one."
            )

        db.execute(
            "UPDATE users SET password_hash=? WHERE user_id=?",
            (hash_password(body.new_password), row["user_id"])
        )
        db.execute(
            "DELETE FROM password_reset_tokens WHERE user_id=?",
            (row["user_id"],)
        )

    return {
        "message": "Your password was reset. Sign in with your new password."
    }


@app.get("/api/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM reels WHERE user_id=?",
            (user["user_id"],)
        ).fetchone()[0]

    limit = None if user["plan"] == "premium" else FREE_REEL_LIMIT

    return {
        **user,
        "saved_reel_count": count,
        "reel_limit": limit,
        "remaining_saves": None if limit is None else max(0, limit - count)
    }


def run_analysis(
    reel_url: str,
    progress: Any = None
) -> dict[str, Any]:
    stage = "fetching"

    try:
        if progress:
            progress("fetching")

        reel = extract_reel.scrape_reel(
            reel_url,
            output_file=None,
            verbose=False
        )

        stage = "analyzing"

        if progress:
            progress("analyzing")

        analysis = spotloc_agent.run_agent(reel)
        analysis["reel_url"] = reel.get("reel_url")

        business = analysis.get("primary_business") or {}
        location = analysis.get("location_analysis") or {}

        normalized = location_processor.normalize_address(analysis)

        address = (
            normalized["address"]
            or location.get("original_address_text")
            or location.get("reconstructed_address")
            or ""
        )

        stage = "resolving_location"

        if progress:
            progress("resolving_location")

        coords = (
            geocode_cached(address)
            if address
            else {
                "latitude": None,
                "longitude": None,
                "geocoding_status": "not_attempted",
                "display_name": None
            }
        )

        postcode_conflict = bool(
            location.get("postal_code")
            and coords.get("display_name")
            and str(location["postal_code"]) not in coords["display_name"]
        )

        return {
            "reel_url": reel.get("reel_url"),
            "reel_id": reel.get("reel_id"),
            "creator": reel.get("creator"),
            "thumbnail": reel.get("thumbnail"),
            "business": {
                "name": business.get("name"),
                "category": business.get("category"),
                "instagram_username": business.get("instagram_username")
            },
            "address": {
                **location,
                "full_address": address
            },
            "coordinates": {
                "latitude": coords.get("latitude"),
                "longitude": coords.get("longitude")
            },
            "geocoding": {
                "status": coords.get("geocoding_status"),
                "display_name": coords.get("display_name"),
                "provider": "OpenStreetMap Nominatim"
            },
            "verification": {
                "business_verified": False,
                "address_verified": False,
                "coordinates_verified": False,
                "user_confirmed": False,
                "status": (
                    "needs_confirmation"
                    if postcode_conflict or not address
                    else "user_review_required"
                ),
                "postal_code_conflict": postcode_conflict
            },
            "confidence": analysis.get("confidence", {}),
            "uncertainties": analysis.get("uncertainties", [])
        }

    except HTTPException:
        raise

    except Exception as error:
        # IMPORTANT: this block must remain indented inside the except clause.
        logger.exception(
            "Reel analysis failed during stage=%s. Error type=%s. Error=%s",
            stage,
            type(error).__name__,
            str(error)
        )

        raise safe_pipeline_error(
            error,
            "extract" if stage == "fetching" else "analyze"
        ) from error


def _job_worker() -> None:
    while True:
        job_id = _job_queue.get()

        try:
            with connect() as db:
                row = db.execute(
                    """
                    SELECT job_id,user_id,reel_url,status
                    FROM reel_jobs WHERE job_id=?
                    """,
                    (job_id,)
                ).fetchone()

            if not row or row["status"] in {
                "completed",
                "partially_completed",
                "failed"
            }:
                continue

            def update_stage(stage: str) -> None:
                with connect() as db:
                    db.execute(
                        """
                        UPDATE reel_jobs
                        SET status=?,stage=?,updated_at=?
                        WHERE job_id=?
                        """,
                        (stage, stage, now_iso(), job_id)
                    )

            try:
                result = run_analysis(row["reel_url"], update_stage)
                coords = result.get("coordinates") or {}

                final_status = (
                    "completed"
                    if coords.get("latitude") is not None
                    and coords.get("longitude") is not None
                    else "partially_completed"
                )

                with connect() as db:
                    db.execute(
                        """
                        UPDATE reel_jobs
                        SET status=?,stage='completed',result_json=?,
                            error_message=NULL,updated_at=?
                        WHERE job_id=?
                        """,
                        (
                            final_status,
                            json.dumps(result),
                            now_iso(),
                            job_id
                        )
                    )

            except HTTPException as error:
                detail = (
                    error.detail
                    if isinstance(error.detail, str)
                    else "Reel analysis could not be completed. Please try again."
                )

                with connect() as db:
                    db.execute(
                        """
                        UPDATE reel_jobs
                        SET status='failed',stage='failed',
                            error_message=?,updated_at=?
                        WHERE job_id=?
                        """,
                        (detail, now_iso(), job_id)
                    )

            except Exception as error:
                logger.exception(
                    "Background Reel job %s failed: %s",
                    job_id,
                    type(error).__name__
                )

                with connect() as db:
                    db.execute(
                        """
                        UPDATE reel_jobs
                        SET status='failed',stage='failed',
                            error_message=?,updated_at=?
                        WHERE job_id=?
                        """,
                        (
                            "SpotLoc could not analyze this Reel. Please try again.",
                            now_iso(),
                            job_id
                        )
                    )

        finally:
            _job_queue.task_done()


_job_queue: queue.Queue[str] = queue.Queue()
_job_thread: threading.Thread | None = None


@app.on_event("startup")
def resume_reel_jobs() -> None:
    global _job_thread

    with connect() as db:
        db.execute(
            """
            UPDATE reel_jobs
            SET status='queued',stage='queued',updated_at=?
            WHERE status IN ('fetching','analyzing','resolving_location')
            """,
            (now_iso(),)
        )

        jobs = db.execute(
            """
            SELECT job_id FROM reel_jobs
            WHERE status='queued'
            ORDER BY created_at
            """
        ).fetchall()

    for row in jobs:
        _job_queue.put(row["job_id"])

    if not _job_thread or not _job_thread.is_alive():
        _job_thread = threading.Thread(
            target=_job_worker,
            name="spotloc-reel-jobs",
            daemon=True
        )
        _job_thread.start()


@app.post("/api/reel-jobs", status_code=202)

@app.get("/api/reel-jobs")
def list_reel_jobs(
    user: dict[str, Any] = Depends(current_user)
) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT job_id,reel_url,status,stage,error_message,
                   created_at,updated_at
            FROM reel_jobs
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (user["user_id"],)
        ).fetchall()

    return [dict(row) for row in rows]

@app.post("/api/reel-jobs", status_code=202)
def create_reel_job(
    body: AnalyzeRequest,
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:

    rate_limit(f"analyze:{user['user_id']}", 5, 3600)

    now = now_iso()
    should_queue = False

    with connect() as db:

        row = db.execute(
            """
            SELECT job_id,status,stage
            FROM reel_jobs
            WHERE user_id=? AND reel_url=?
            """,
            (user["user_id"], body.reel_url)
        ).fetchone()

        if row:

            job_id = row["job_id"]

            if row["status"] == "failed":

                db.execute(
                    """
                    UPDATE reel_jobs
                    SET status='queued',
                        stage='queued',
                        result_json=NULL,
                        error_message=NULL,
                        updated_at=?
                    WHERE job_id=?
                    """,
                    (now, job_id)
                )

                status_value = "queued"
                stage_value = "queued"
                should_queue = True

            else:

                status_value = row["status"]
                stage_value = row["stage"]

        else:

            job_id = secrets.token_urlsafe(18)

            db.execute(
                """
                INSERT INTO reel_jobs
                (
                    job_id,
                    user_id,
                    reel_url,
                    status,
                    stage,
                    created_at,
                    updated_at
                )
                VALUES(
                    ?, ?, ?, 'queued', 'queued', ?, ?
                )
                """,
                (
                    job_id,
                    user["user_id"],
                    body.reel_url,
                    now,
                    now
                )
            )

            status_value = "queued"
            stage_value = "queued"
            should_queue = True

    # IMPORTANT:
    # Queue the job only AFTER the database transaction commits.
    if should_queue:
        logger.info("Queuing Reel job: %s", job_id)
        _job_queue.put(job_id)

    return {
        "job_id": job_id,
        "status": status_value,
        "stage": stage_value
    }
@app.get("/api/reel-jobs/{job_id}")
def get_reel_job(
    job_id: str,
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            SELECT job_id,reel_url,status,stage,result_json,error_message,
                   created_at,updated_at
            FROM reel_jobs
            WHERE job_id=? AND user_id=?
            """,
            (job_id, user["user_id"])
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Reel job not found.")

    result = dict(row)
    result["result"] = (
        json.loads(result.pop("result_json"))
        if result.get("result_json")
        else None
    )
    return result


@app.post("/api/reels/analyze")
def analyze(
    body: AnalyzeRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    rate_limit(f"analyze:{user['user_id']}", 5, 3600)
    return run_analysis(body.reel_url)


@app.get("/api/reels")
def list_reels(
    user: dict[str, Any] = Depends(current_user)
) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT reel_id,reel_url,instagram_username,business_name,category,
                   address,location_json,verification_status,user_confirmed,created_at
            FROM reels
            WHERE user_id=?
            ORDER BY created_at DESC
            """,
            (user["user_id"],)
        ).fetchall()

    return [
        {
            **dict(row),
            "location": json.loads(row["location_json"]),
            "user_confirmed": bool(row["user_confirmed"])
        }
        for row in rows
    ]


@app.post("/api/reels/save", status_code=201)
def save_reel(
    body: SaveRequest,
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        duplicate = db.execute(
            """
            SELECT reel_id FROM reels
            WHERE user_id=? AND reel_url=?
            """,
            (user["user_id"], body.reel_url)
        ).fetchone()

        if duplicate:
            raise HTTPException(
                status_code=409,
                detail="You have already saved this Reel."
            )

        row = db.execute(
            "SELECT plan FROM users WHERE user_id=?",
            (user["user_id"],)
        ).fetchone()

        count = db.execute(
            "SELECT COUNT(*) FROM reels WHERE user_id=?",
            (user["user_id"],)
        ).fetchone()[0]

        if row["plan"] == "free" and count >= FREE_REEL_LIMIT:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "free_limit_reached",
                    "message": (
                        "You've reached your free SpotLoc limit! "
                        "You have saved 5 out of 5 Reels. Upgrade your "
                        "subscription to discover and save more places."
                    )
                }
            )

        reel_id = secrets.token_urlsafe(18)
        created_at = now_iso()
        values = body.model_dump()
        values["user_confirmed"] = body.user_confirmed

        db.execute(
            """
            INSERT INTO reels(
                reel_id,user_id,reel_url,instagram_username,business_name,
                category,address,location_json,verification_status,
                user_confirmed,created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                reel_id,
                user["user_id"],
                body.reel_url,
                body.instagram_username,
                body.business_name,
                body.category,
                body.address,
                json.dumps(values),
                "user_confirmed" if body.user_confirmed else body.verification_status,
                int(body.user_confirmed),
                created_at
            )
        )

    return {
        "reel_id": reel_id,
        "created_at": created_at,
        "saved_reel_count": count + 1,
        "reel_limit": None if row["plan"] == "premium" else FREE_REEL_LIMIT,
        "remaining_saves": (
            None
            if row["plan"] == "premium"
            else FREE_REEL_LIMIT - count - 1
        )
    }


@app.delete("/api/reels/{reel_id}", status_code=204)
def delete_reel(
    reel_id: str,
    user: dict[str, Any] = Depends(current_user)
) -> None:
    with connect() as db:
        result = db.execute(
            "DELETE FROM reels WHERE reel_id=? AND user_id=?",
            (reel_id, user["user_id"])
        )

        if result.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="Saved Reel not found."
            )


@app.get("/api/preferences")
def get_preferences(
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            SELECT nearby_notifications_enabled,proximity_radius_m
            FROM preferences WHERE user_id=?
            """,
            (user["user_id"],)
        ).fetchone()

    return {
        "nearby_notifications_enabled": bool(row[0]),
        "proximity_radius_m": row[1]
    }


@app.patch("/api/preferences")
def update_preferences(
    body: PreferenceUpdate,
    user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    with connect() as db:
        db.execute(
            """
            UPDATE preferences
            SET nearby_notifications_enabled=?,proximity_radius_m=?
            WHERE user_id=?
            """,
            (
                int(body.nearby_notifications_enabled),
                body.proximity_radius_m,
                user["user_id"]
            )
        )

    return body.model_dump()


@app.delete("/api/me", status_code=204)
def delete_account(
    body: DeleteAccountRequest,
    user: dict[str, Any] = Depends(current_user)
) -> None:
    with connect() as db:
        row = db.execute(
            "SELECT password_hash FROM users WHERE user_id=?",
            (user["user_id"],)
        ).fetchone()

        if not row or not check_password(body.password, row["password_hash"]):
            raise HTTPException(
                status_code=401,
                detail="Password is incorrect."
            )

        db.execute(
            "DELETE FROM users WHERE user_id=?",
            (user["user_id"],)
        )
        
