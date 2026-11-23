from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request as UrlRequest, urlopen
from uuid import UUID

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from src.chart_collector import fetch_youtube_chart


ROOT = Path(__file__).resolve().parent
CHART_TYPES = {"TopSongs", "TopMusicVideos", "TopArtists"}
ENTITY_TYPES = {"all", "video", "channel", "artist"}
MARKETS = {
    "global": "Global", "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "NZ": "New Zealand", "DE": "Germany", "FR": "France",
    "IT": "Italy", "ES": "Spain", "BR": "Brazil", "MX": "Mexico", "JP": "Japan",
    "KR": "South Korea", "IN": "India", "ID": "Indonesia", "PH": "Philippines",
    "AR": "Argentina", "NL": "Netherlands", "SE": "Sweden", "NO": "Norway",
}
WORKER_INTERVAL_SECONDS = 5
MAX_JOB_DAYS = 365
pool: asyncpg.Pool | None = None
worker_task: asyncio.Task | None = None


def require_pool() -> asyncpg.Pool:
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not ready")
    return pool


def auth_base_url() -> str:
    value = os.environ.get("NEON_AUTH_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise HTTPException(status_code=503, detail="Neon Auth is not configured")
    return value


def call_neon_auth(path: str, method: str, cookie: str = "", body: bytes | None = None):
    base_url = auth_base_url()
    safe_path = "/".join(quote(part, safe="-._~") for part in path.split("/"))
    endpoint = f"{base_url}/{safe_path}"
    auth_origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    headers = {"Accept": "application/json", "Origin": auth_origin}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = UrlRequest(endpoint, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as result:
            return result.status, result.read(), result.headers.get_all("Set-Cookie", [])
    except HTTPError as error:
        return error.code, error.read(), error.headers.get_all("Set-Cookie", [])
    except (URLError, TimeoutError, OSError) as error:
        raise HTTPException(status_code=503, detail="Neon Auth is temporarily unavailable") from error


def ensure_same_origin(request: Request) -> None:
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    origin = request.headers.get("origin")
    if not origin:
        return

    parsed_origin = urlsplit(origin)
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    # Browser fetch metadata survives the preview proxy even when it rewrites Host.
    if fetch_site == "same-origin":
        return

    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    host = host.split(",", 1)[0].strip().casefold()
    if not host or parsed_origin.netloc.casefold() != host:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def json_from_auth(body: bytes) -> dict:
    try:
        value = json.loads(body or b"{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def get_current_user(request: Request) -> dict:
    cookie = request.headers.get("cookie", "")
    if not cookie:
        raise HTTPException(status_code=401, detail="Sign in to access your chart data")
    status, body, _ = await asyncio.to_thread(call_neon_auth, "get-session", "GET", cookie, None)
    if status >= 500:
        raise HTTPException(status_code=503, detail="Could not verify the current session")
    payload = json_from_auth(body)
    session_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    user = session_data.get("user") if isinstance(session_data, dict) else None
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    return {"id": user["id"], "email": user.get("email", ""), "name": user.get("name", "")}


def user_id(user: dict = Depends(get_current_user)) -> str:
    return user["id"]


def parse_jsonb(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def row_dict(row: asyncpg.Record) -> dict:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (date,)):
            result[key] = value.isoformat()
        elif isinstance(value, UUID):
            result[key] = str(value)
    if "metrics" in result:
        result["metrics"] = parse_jsonb(result["metrics"])
    return result


class ConfigurationInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    chart_type: Literal["TopSongs", "TopMusicVideos", "TopArtists"]
    entity_type: Literal["all", "video", "channel", "artist"] = "all"
    entity_id: str = Field(default="all", max_length=200)
    markets: list[str] = Field(min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a name for this setup")
        return value

    @field_validator("entity_id")
    @classmethod
    def clean_entity_id(cls, value: str) -> str:
        value = value.strip()
        return value or "all"

    @field_validator("markets")
    @classmethod
    def validate_markets(cls, values: list[str]) -> list[str]:
        unique = list(dict.fromkeys(values))
        if not unique or any(value not in MARKETS for value in unique):
            raise ValueError("Choose supported YouTube chart markets")
        if "global" in unique and len(unique) > 1:
            raise ValueError("Global cannot be combined with country markets")
        return unique

    @model_validator(mode="after")
    def validate_entity(self):
        if self.entity_type == "all":
            self.entity_id = "all"
        elif self.entity_id == "all":
            raise ValueError("Add a video, channel, or artist identifier")
        return self


class CreateJobInput(ConfigurationInput):
    name: str = Field(default="Chart collection", min_length=1, max_length=80)
    start_date: date
    end_date: date
    save_configuration: bool = False

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date")
        if self.end_date > date.today():
            raise ValueError("Future chart dates are not available")
        if self.start_date < date(2016, 1, 1):
            raise ValueError("YouTube Charts archive begins in 2016")
        if (self.end_date - self.start_date).days > MAX_JOB_DAYS:
            raise ValueError("Choose a range of no more than one year per job")
        return self


class RetryInput(BaseModel):
    mode: Literal["failed", "selected"] = "failed"
    markets: list[str] | None = Field(default=None, max_length=10)
    dates: list[date] | None = Field(default=None, max_length=260)

    @model_validator(mode="after")
    def require_selection(self):
        if self.mode == "selected" and not self.markets and not self.dates:
            raise ValueError("Select at least one market or date to retry")
        if self.markets and any(market not in MARKETS for market in self.markets):
            raise ValueError("Choose supported YouTube chart markets")
        return self


class EntriesResponse(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int


async def refresh_job_status(connection: asyncpg.Connection, job_id: UUID) -> None:
    job = await connection.fetchrow("SELECT status FROM collection_jobs WHERE id=$1", job_id)
    if not job or job["status"] == "paused":
        return
    counts = await connection.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE status='pending')::int AS pending,
                  COUNT(*) FILTER (WHERE status='running')::int AS running,
                  COUNT(*) FILTER (WHERE status='succeeded')::int AS succeeded,
                  COUNT(*) FILTER (WHERE status='failed')::int AS failed
           FROM collection_job_tasks WHERE job_id=$1""",
        job_id,
    )
    if counts["pending"] or counts["running"]:
        status, completed = "running", None
    elif counts["failed"] and counts["succeeded"]:
        status, completed = "partial", "now()"
    elif counts["failed"]:
        status, completed = "failed", "now()"
    else:
        status, completed = "completed", "now()"
    if completed:
        await connection.execute(
            "UPDATE collection_jobs SET status=$1, updated_at=now(), completed_at=now() WHERE id=$2",
            status,
            job_id,
        )
    else:
        await connection.execute(
            "UPDATE collection_jobs SET status=$1, updated_at=now() WHERE id=$2",
            status,
            job_id,
        )


async def claim_task() -> asyncpg.Record | None:
    database = require_pool()
    async with database.acquire() as connection:
        async with connection.transaction():
            task = await connection.fetchrow(
                """SELECT t.id, t.job_id, t.user_id, t.market, t.chart_date,
                          j.chart_type, j.entity_type, j.entity_id
                   FROM collection_job_tasks t
                   JOIN collection_jobs j ON j.id=t.job_id AND j.user_id=t.user_id
                   WHERE t.status='pending' AND j.status IN ('queued','running')
                   ORDER BY t.created_at, t.chart_date, t.market
                   LIMIT 1 FOR UPDATE OF t SKIP LOCKED"""
            )
            if not task:
                return None
            await connection.execute(
                "UPDATE collection_job_tasks SET status='running', attempts=attempts+1, started_at=now(), error_message=NULL WHERE id=$1",
                task["id"],
            )
            await connection.execute(
                "UPDATE collection_jobs SET status='running', started_at=COALESCE(started_at, now()), updated_at=now() WHERE id=$1 AND status='queued'",
                task["job_id"],
            )
            return task


async def save_collection(task: asyncpg.Record, entries: list[dict]) -> None:
    database = require_pool()
    async with database.acquire() as connection:
        async with connection.transaction():
            snapshot = await connection.fetchrow(
                """INSERT INTO yt_chart_snapshots
                   (user_id, entity_type, entity_id, chart_type, market, snapshot_date, total_entries)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (user_id, entity_type, entity_id, chart_type, market, snapshot_date)
                   DO UPDATE SET total_entries=EXCLUDED.total_entries, created_at=now()
                   RETURNING id""",
                task["user_id"], task["entity_type"], task["entity_id"], task["chart_type"],
                task["market"], task["chart_date"], len(entries),
            )
            snapshot_id = snapshot["id"]
            values = []
            entry_ids = []
            for entry in entries:
                entry_id = str(entry["entry_id"])
                entry_ids.append(entry_id)
                values.append((
                    task["user_id"], snapshot_id, task["entity_type"], task["entity_id"],
                    task["chart_type"], task["market"], task["chart_date"], entry_id,
                    entry.get("video_id"), entry.get("channel_id"), entry["title"], entry.get("rank"),
                    entry.get("view_count"), entry.get("like_count"), entry.get("comment_count"),
                    json.dumps(entry.get("metrics") or {}),
                ))
            await connection.executemany(
                """INSERT INTO yt_chart_entries
                   (user_id,snapshot_id,entity_type,entity_id,chart_type,market,snapshot_date,entry_id,
                    video_id,channel_id,title,rank,view_count,like_count,comment_count,metrics)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)
                   ON CONFLICT (user_id,entity_type,entity_id,chart_type,market,snapshot_date,entry_id)
                   DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id, video_id=EXCLUDED.video_id,
                     channel_id=EXCLUDED.channel_id, title=EXCLUDED.title, rank=EXCLUDED.rank,
                     view_count=EXCLUDED.view_count, like_count=EXCLUDED.like_count,
                     comment_count=EXCLUDED.comment_count, metrics=EXCLUDED.metrics""",
                values,
            )
            await connection.execute(
                "DELETE FROM yt_chart_entries WHERE snapshot_id=$1 AND NOT (entry_id = ANY($2::text[]))",
                snapshot_id,
                entry_ids,
            )
            await connection.execute(
                "UPDATE collection_job_tasks SET status='succeeded', completed_at=now(), error_message=NULL WHERE id=$1",
                task["id"],
            )
            await refresh_job_status(connection, task["job_id"])


async def fail_task(task: asyncpg.Record, error: Exception) -> None:
    database = require_pool()
    message = str(error).strip()[:1000] or "YouTube Charts collection failed"
    async with database.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "UPDATE collection_job_tasks SET status='failed', completed_at=now(), error_message=$1 WHERE id=$2",
                message,
                task["id"],
            )
            await refresh_job_status(connection, task["job_id"])


async def process_one_task() -> bool:
    task = await claim_task()
    if not task:
        return False
    try:
        entries = await asyncio.to_thread(
            fetch_youtube_chart, task["chart_type"], task["market"], task["chart_date"].isoformat()
        )
        if task["entity_type"] != "all":
            needle = task["entity_id"].casefold()
            entries = [
                entry for entry in entries
                if needle in entry["title"].casefold()
                or needle in " ".join(entry.get("artists", [])).casefold()
                or needle in str(entry.get("video_id") or "").casefold()
                or needle in str(entry.get("channel_id") or "").casefold()
            ]
            if not entries:
                raise RuntimeError("No chart entries matched the selected entity")
        await save_collection(task, entries)
    except Exception as error:
        await fail_task(task, error)
    return True


async def worker_loop() -> None:
    while True:
        try:
            await process_one_task()
            await asyncio.sleep(WORKER_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(WORKER_INTERVAL_SECONDS)


async def recover_interrupted_tasks() -> None:
    database = require_pool()
    async with database.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "UPDATE collection_job_tasks SET status='pending', error_message='Worker restarted before this task completed', completed_at=NULL WHERE status='running'"
            )
            await connection.execute(
                "UPDATE collection_jobs SET status='queued', updated_at=now() WHERE status='running'"
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, worker_task
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to start the chart tracker")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=8)
    try:
        async with pool.acquire() as connection:
            await connection.execute("SELECT 1")
        await recover_interrupted_tasks()
        worker_task = asyncio.create_task(worker_loop(), name="youtube-chart-worker")
        yield
    finally:
        if worker_task:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
            worker_task = None
        await pool.close()
        pool = None


app = FastAPI(title="YouTube Charts", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy-Report-Only"] = (
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'self'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000"
    return response


@app.get("/")
async def dashboard():
    return FileResponse(ROOT / "web" / "index.html")


@app.api_route("/api/auth/{auth_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def auth_proxy(auth_path: str, request: Request):
    if any(part in {".", ".."} for part in auth_path.split("/")):
        raise HTTPException(status_code=400, detail="Invalid auth endpoint")
    if request.method not in {"GET"}:
        ensure_same_origin(request)
    body = await request.body()
    status, response_body, cookies = await asyncio.to_thread(
        call_neon_auth, auth_path, request.method, request.headers.get("cookie", ""), body or None
    )
    response = Response(content=response_body, status_code=status, media_type="application/json")
    response.headers["Cache-Control"] = "no-store"
    for cookie in cookies:
        cookie = re.sub(r";\s*Domain=[^;]+", "", cookie, flags=re.IGNORECASE)
        cookie = re.sub(r";\s*Path=[^;]+", "; Path=/", cookie, flags=re.IGNORECASE)
        cookie = re.sub(r";\s*SameSite=[^;]+", "; SameSite=None", cookie, flags=re.IGNORECASE)
        if not re.search(r";\s*path=", cookie, flags=re.IGNORECASE):
            cookie += "; Path=/"
        if not re.search(r";\s*samesite=", cookie, flags=re.IGNORECASE):
            cookie += "; SameSite=None"
        if not re.search(r";\s*secure(?:;|$)", cookie, flags=re.IGNORECASE):
            cookie += "; Secure"
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


@app.get("/api/session")
async def session_info(user: dict = Depends(get_current_user)):
    return {"user": user}


@app.get("/api/health")
async def health():
    database = require_pool()
    async with database.acquire() as connection:
        await connection.execute("SELECT 1")
    return {"status": "ok", "database": "connected", "worker": "running" if worker_task else "stopped"}


@app.get("/api/markets")
async def markets():
    return [{"code": code, "name": name} for code, name in MARKETS.items()]


@app.get("/api/configurations")
async def list_configurations(current_user: str = Depends(user_id)):
    database = require_pool()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id,name,chart_type,entity_type,entity_id,markets,created_at,updated_at FROM chart_configurations WHERE user_id=$1 ORDER BY updated_at DESC LIMIT 100",
            current_user,
        )
    return [row_dict(row) for row in rows]


@app.post("/api/configurations")
async def save_configuration(configuration: ConfigurationInput, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """INSERT INTO chart_configurations (user_id,name,chart_type,entity_type,entity_id,markets)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (user_id,name) DO UPDATE SET chart_type=EXCLUDED.chart_type,
                 entity_type=EXCLUDED.entity_type, entity_id=EXCLUDED.entity_id,
                 markets=EXCLUDED.markets, updated_at=now()
               RETURNING id,name,chart_type,entity_type,entity_id,markets,created_at,updated_at""",
            current_user, configuration.name, configuration.chart_type, configuration.entity_type,
            configuration.entity_id, configuration.markets,
        )
    return row_dict(row)


@app.delete("/api/configurations/{configuration_id}")
async def delete_configuration(configuration_id: UUID, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    async with database.acquire() as connection:
        result = await connection.execute(
            "DELETE FROM chart_configurations WHERE id=$1 AND user_id=$2", configuration_id, current_user
        )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="Saved setup not found")
    return {"deleted": True}


@app.post("/api/jobs")
async def create_job(job: CreateJobInput, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    dates = []
    current = job.start_date
    while current <= job.end_date:
        dates.append(current)
        current += timedelta(days=1)
    if len(dates) * len(job.markets) > 3_660:
        raise HTTPException(status_code=422, detail="This job creates too many market/day tasks")

    async with database.acquire() as connection:
        async with connection.transaction():
            configuration_id = None
            if job.save_configuration:
                configuration = await connection.fetchrow(
                    """INSERT INTO chart_configurations (user_id,name,chart_type,entity_type,entity_id,markets)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (user_id,name) DO UPDATE SET chart_type=EXCLUDED.chart_type,
                         entity_type=EXCLUDED.entity_type, entity_id=EXCLUDED.entity_id,
                         markets=EXCLUDED.markets, updated_at=now()
                       RETURNING id""",
                    current_user, job.name, job.chart_type, job.entity_type, job.entity_id, job.markets,
                )
                configuration_id = configuration["id"]
            created = await connection.fetchrow(
                """INSERT INTO collection_jobs
                   (user_id,configuration_id,configuration_name,chart_type,entity_type,entity_id,markets,start_date,end_date)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                current_user, configuration_id, job.name, job.chart_type, job.entity_type, job.entity_id,
                job.markets, job.start_date, job.end_date,
            )
            task_rows = [
                (created["id"], current_user, market, chart_date)
                for chart_date in dates for market in job.markets
            ]
            await connection.executemany(
                "INSERT INTO collection_job_tasks (job_id,user_id,market,chart_date) VALUES ($1,$2,$3,$4)",
                task_rows,
            )
    return {"id": str(created["id"]), "status": "queued", "total_tasks": len(task_rows)}


@app.get("/api/jobs")
async def list_jobs(current_user: str = Depends(user_id)):
    database = require_pool()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            """SELECT j.id,j.configuration_name,j.chart_type,j.entity_type,j.entity_id,j.markets,
                      j.start_date,j.end_date,j.status,j.created_at,j.started_at,j.updated_at,j.completed_at,
                      COUNT(t.id)::int AS total_tasks,
                      COUNT(t.id) FILTER (WHERE t.status='succeeded')::int AS succeeded_tasks,
                      COUNT(t.id) FILTER (WHERE t.status='failed')::int AS failed_tasks,
                      COUNT(t.id) FILTER (WHERE t.status IN ('pending','running'))::int AS remaining_tasks
               FROM collection_jobs j LEFT JOIN collection_job_tasks t ON t.job_id=j.id AND t.user_id=j.user_id
               WHERE j.user_id=$1 GROUP BY j.id ORDER BY j.created_at DESC LIMIT 30""",
            current_user,
        )
    return [row_dict(row) for row in rows]


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: UUID,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: str = Depends(user_id),
):
    database = require_pool()
    async with database.acquire() as connection:
        job = await connection.fetchrow(
            "SELECT * FROM collection_jobs WHERE id=$1 AND user_id=$2", job_id, current_user
        )
        if not job:
            raise HTTPException(status_code=404, detail="Collection job not found")
        task_count = await connection.fetchval(
            "SELECT COUNT(*) FROM collection_job_tasks WHERE job_id=$1 AND user_id=$2", job_id, current_user
        )
        tasks = await connection.fetch(
            "SELECT id,market,chart_date,status,attempts,error_message,started_at,completed_at FROM collection_job_tasks WHERE job_id=$1 AND user_id=$2 ORDER BY chart_date,market LIMIT $3 OFFSET $4",
            job_id, current_user, page_size, (page - 1) * page_size,
        )
    result = row_dict(job)
    result["tasks"] = [row_dict(task) for task in tasks]
    result["task_count"] = task_count
    result["task_page"] = page
    result["task_page_size"] = page_size
    return result


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: UUID, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    async with database.acquire() as connection:
        result = await connection.execute(
            "UPDATE collection_jobs SET status='paused',updated_at=now() WHERE id=$1 AND user_id=$2 AND status IN ('queued','running')",
            job_id, current_user,
        )
    if result.endswith("0"):
        raise HTTPException(status_code=409, detail="This job cannot be paused")
    return {"status": "paused"}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: UUID, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    async with database.acquire() as connection:
        result = await connection.execute(
            "UPDATE collection_jobs SET status='queued',updated_at=now(),completed_at=NULL WHERE id=$1 AND user_id=$2 AND status='paused'",
            job_id, current_user,
        )
        if not result.endswith("0"):
            await refresh_job_status(connection, job_id)
    if result.endswith("0"):
        raise HTTPException(status_code=409, detail="This job is not paused")
    return {"status": "queued"}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: UUID, selection: RetryInput, request: Request, current_user: str = Depends(user_id)):
    ensure_same_origin(request)
    database = require_pool()
    async with database.acquire() as connection:
        job = await connection.fetchrow(
            "SELECT id,status FROM collection_jobs WHERE id=$1 AND user_id=$2", job_id, current_user
        )
        if not job:
            raise HTTPException(status_code=404, detail="Collection job not found")
        conditions = ["job_id=$1", "user_id=$2", "status <> 'running'"]
        args: list = [job_id, current_user]
        if selection.mode == "failed":
            conditions.append("status='failed'")
        if selection.markets:
            args.append(selection.markets)
            conditions.append(f"market = ANY(${len(args)}::text[])")
        if selection.dates:
            args.append(selection.dates)
            conditions.append(f"chart_date = ANY(${len(args)}::date[])")
        result = await connection.execute(
            f"UPDATE collection_job_tasks SET status='pending',error_message=NULL,completed_at=NULL WHERE {' AND '.join(conditions)}",
            *args,
        )
        requeued = int(result.rsplit(" ", 1)[-1])
        if not requeued:
            raise HTTPException(status_code=409, detail="No matching tasks are available to retry")
        await connection.execute(
            "UPDATE collection_jobs SET status='queued',updated_at=now(),completed_at=NULL WHERE id=$1 AND user_id=$2",
            job_id, current_user,
        )
    return {"status": "queued", "requeued": requeued}


@app.get("/api/summary")
async def summary(current_user: str = Depends(user_id)):
    database = require_pool()
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT (SELECT COUNT(*)::int FROM yt_chart_entries WHERE user_id=$1) AS entries,
                      (SELECT COUNT(*)::int FROM yt_chart_snapshots WHERE user_id=$1) AS snapshots,
                      (SELECT MAX(snapshot_date) FROM yt_chart_snapshots WHERE user_id=$1) AS latest_date,
                      (SELECT MAX(created_at) FROM yt_chart_snapshots WHERE user_id=$1) AS latest_snapshot_at,
                      (SELECT COUNT(*)::int FROM collection_jobs WHERE user_id=$1 AND status IN ('queued','running')) AS active_jobs,
                      (SELECT COUNT(*)::int FROM collection_job_tasks WHERE user_id=$1 AND status='failed') AS failed_tasks""",
            current_user,
        )
    return row_dict(row)


@app.get("/api/entities")
async def list_entities(current_user: str = Depends(user_id)):
    database = require_pool()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT DISTINCT entity_type,entity_id FROM yt_chart_entries WHERE user_id=$1 ORDER BY entity_type,entity_id LIMIT 500",
            current_user,
        )
    return [row_dict(row) for row in rows]


@app.get("/api/trends")
async def chart_trends(
    chart_type: str | None = None,
    market: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    current_user: str = Depends(user_id),
):
    if chart_type and chart_type not in CHART_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported chart type")
    if market and market not in MARKETS:
        raise HTTPException(status_code=422, detail="Unsupported market")
    if entity_type and entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported entity type")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date")
    conditions = ["user_id=$1"]
    args: list = [current_user]
    for column, value in (("chart_type", chart_type), ("market", market),
                          ("entity_type", entity_type), ("entity_id", entity_id),
                          ("snapshot_date >=", start_date), ("snapshot_date <=", end_date)):
        if value is not None:
            args.append(value)
            conditions.append(f"{column} ${len(args)}")
    database = require_pool()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            f"""SELECT snapshot_date, AVG(rank)::float8 AS average_rank, COUNT(*)::int AS entry_count
                FROM yt_chart_entries WHERE {' AND '.join(conditions)} AND rank IS NOT NULL
                GROUP BY snapshot_date ORDER BY snapshot_date""",
            *args,
        )
    return {"items": [row_dict(row) for row in rows]}


@app.get("/api/entries", response_model=EntriesResponse)
async def browse_entries(
    chart_type: str | None = None,
    market: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=25, ge=1, le=100),
    current_user: str = Depends(user_id),
):
    if chart_type and chart_type not in CHART_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported chart type")
    if market and market not in MARKETS:
        raise HTTPException(status_code=422, detail="Unsupported market")
    if entity_type and entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported entity type")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date")

    conditions = ["e.user_id=$1"]
    args: list = [current_user]
    for column, value in (("e.chart_type", chart_type), ("e.market", market),
                          ("e.entity_type", entity_type), ("e.entity_id", entity_id),
                          ("e.snapshot_date >=", start_date), ("e.snapshot_date <=", end_date)):
        if value is not None:
            args.append(value)
            conditions.append(f"{column} ${len(args)}")
    where = " AND ".join(conditions)
    database = require_pool()
    async with database.acquire() as connection:
        total = await connection.fetchval(f"SELECT COUNT(*) FROM yt_chart_entries e WHERE {where}", *args)
        page_args = [*args, page_size, (page - 1) * page_size]
        rows = await connection.fetch(
            f"""SELECT e.id,e.snapshot_id,e.entity_type,e.entity_id,e.chart_type,e.market,e.snapshot_date,
                       e.entry_id,e.video_id,e.channel_id,e.title,e.rank,e.view_count,e.like_count,e.comment_count,e.metrics,
                       previous.rank AS previous_rank, previous.view_count AS previous_view_count,
                       CASE WHEN e.rank IS NOT NULL AND previous.rank IS NOT NULL THEN previous.rank-e.rank END AS rank_change,
                       CASE WHEN e.view_count IS NOT NULL AND previous.view_count IS NOT NULL THEN e.view_count-previous.view_count END AS view_change
                FROM yt_chart_entries e
                LEFT JOIN LATERAL (
                    SELECT p.rank,p.view_count FROM yt_chart_entries p
                    WHERE p.user_id=e.user_id AND p.entity_type=e.entity_type AND p.entity_id=e.entity_id
                      AND p.chart_type=e.chart_type AND p.market=e.market AND p.entry_id=e.entry_id
                      AND p.snapshot_date < e.snapshot_date
                    ORDER BY p.snapshot_date DESC LIMIT 1
                ) previous ON TRUE
                WHERE {where}
                ORDER BY e.snapshot_date DESC,e.rank ASC NULLS LAST,e.title
                LIMIT ${len(args)+1} OFFSET ${len(args)+2}""",
            *page_args,
        )
    return {"items": [row_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}
