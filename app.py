import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from config import URL as CHART_URL
from scraper.browser_manager import BrowserManager
from scraper.fetcher import Fetcher
from scraper.processor import Parser


pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to start the chart tracker")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    try:
        async with pool.acquire() as connection:
            await connection.execute("SELECT 1")
        yield
    finally:
        await pool.close()
        pool = None


app = FastAPI(title="YouTube Charts", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")


class ChartEntry(BaseModel):
    rank: str = Field(max_length=40)
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    artists: list[str] = Field(default_factory=list, max_length=20)
    thumbnail: str | None = Field(default=None, max_length=2000)
    metrics: dict[str, str | None] = Field(default_factory=dict)


class TrackSelection(BaseModel):
    items: list[ChartEntry] = Field(min_length=1, max_length=50)


class ChartUnavailable(RuntimeError):
    pass


def normalize_youtube_url(value: str) -> str:
    absolute = urljoin("https://music.youtube.com/", value)
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or parsed.hostname not in {
        "music.youtube.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
    }:
        raise HTTPException(status_code=422, detail="Only YouTube video links can be tracked")
    if not parsed.path.startswith(("/watch", "/playlist", "/shorts/")) and parsed.hostname != "youtu.be":
        raise HTTPException(status_code=422, detail="The selected link is not a YouTube video")
    return absolute


def rank_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def scrape_chart() -> list[dict]:
    with BrowserManager(headless=True) as browser:
        fetcher = Fetcher(browser=browser)
        fetcher.fetch(CHART_URL, scroll=True)
        if any("recaptcha/enterprise" in frame.url for frame in browser.page.frames):
            raise ChartUnavailable(
                "YouTube Charts is showing a Google verification challenge. This app does not bypass verification; try again later."
            )
        entries = Parser(browser=browser).parse_songs()
        if not entries:
            raise ChartUnavailable(
                "YouTube Music returned no chart entries. The chart may be unavailable or its page structure may have changed."
            )
        return entries


def require_pool() -> asyncpg.Pool:
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not ready")
    return pool


@app.get("/")
async def dashboard():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
async def health():
    database = require_pool()
    async with database.acquire() as connection:
        await connection.execute("SELECT 1")
    return {"status": "ok", "database": "connected"}


@app.get("/api/chart")
async def current_chart():
    try:
        entries = await asyncio.to_thread(scrape_chart)
        return [entry for entry in entries if entry.get("url") and entry.get("title")]
    except ChartUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="The YouTube Music chart could not be loaded. Please try again shortly.") from error


@app.get("/api/tracked")
async def tracked_items():
    database = require_pool()
    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT t.id, t.video_url, t.title, t.artists, t.thumbnail_url, t.created_at,
                   latest.rank, latest.last_week_position, latest.weeks_on_chart,
                   latest.weekly_views, latest.release_date, latest.snapshot_date,
                   (SELECT COUNT(*)::int FROM chart_snapshots s WHERE s.tracked_item_id = t.id) AS snapshot_count
            FROM tracked_items t
            LEFT JOIN LATERAL (
                SELECT rank, last_week_position, weeks_on_chart, weekly_views, release_date, snapshot_date
                FROM chart_snapshots
                WHERE tracked_item_id = t.id
                ORDER BY snapshot_date DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE t.is_active = TRUE
            ORDER BY t.created_at DESC
            """
        )
    return [
        {
            **dict(row),
            "artists": row["artists"] if isinstance(row["artists"], list) else json.loads(row["artists"]),
            "created_at": row["created_at"].isoformat(),
            "snapshot_date": row["snapshot_date"].isoformat() if row["snapshot_date"] else None,
        }
        for row in rows
    ]


@app.post("/api/tracked")
async def save_tracked(selection: TrackSelection):
    database = require_pool()
    saved = 0
    async with database.acquire() as connection:
        async with connection.transaction():
            for item in selection.items:
                video_url = normalize_youtube_url(str(item.url))
                row = await connection.fetchrow(
                    """
                    INSERT INTO tracked_items (video_url, title, artists, thumbnail_url, is_active)
                    VALUES ($1, $2, $3::jsonb, $4, TRUE)
                    ON CONFLICT (video_url) DO UPDATE SET
                        title = EXCLUDED.title,
                        artists = EXCLUDED.artists,
                        thumbnail_url = EXCLUDED.thumbnail_url,
                        is_active = TRUE
                    RETURNING id
                    """,
                    video_url,
                    item.title,
                    __import__("json").dumps(item.artists),
                    item.thumbnail,
                )
                metrics = item.metrics or {}
                await connection.execute(
                    """
                    INSERT INTO chart_snapshots (
                        tracked_item_id, snapshot_date, rank, last_week_position,
                        weeks_on_chart, weekly_views, release_date
                    ) VALUES ($1, CURRENT_DATE, $2, $3, $4, $5, $6)
                    ON CONFLICT (tracked_item_id, snapshot_date) DO UPDATE SET
                        rank = EXCLUDED.rank,
                        last_week_position = EXCLUDED.last_week_position,
                        weeks_on_chart = EXCLUDED.weeks_on_chart,
                        weekly_views = EXCLUDED.weekly_views,
                        release_date = EXCLUDED.release_date
                    """,
                    row["id"],
                    rank_number(item.rank),
                    metrics.get("last_week_position"),
                    metrics.get("weeks_on_chart"),
                    metrics.get("weekly_views"),
                    metrics.get("release_date"),
                )
                saved += 1
    return {"saved": saved}


@app.post("/api/refresh")
async def refresh_tracked():
    database = require_pool()
    try:
        entries = await asyncio.to_thread(scrape_chart)
    except ChartUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="The YouTube Music chart could not be refreshed.") from error

    by_url = {}
    for entry in entries:
        try:
            by_url[normalize_youtube_url(entry["url"])] = entry
        except (HTTPException, KeyError, TypeError):
            continue

    updated = 0
    async with database.acquire() as connection:
        async with connection.transaction():
            tracked = await connection.fetch("SELECT id, video_url FROM tracked_items WHERE is_active = TRUE")
            for item in tracked:
                entry = by_url.get(item["video_url"])
                if not entry:
                    continue
                metrics = entry.get("metrics") or {}
                await connection.execute(
                    """
                    INSERT INTO chart_snapshots (
                        tracked_item_id, snapshot_date, rank, last_week_position,
                        weeks_on_chart, weekly_views, release_date
                    ) VALUES ($1, CURRENT_DATE, $2, $3, $4, $5, $6)
                    ON CONFLICT (tracked_item_id, snapshot_date) DO UPDATE SET
                        rank = EXCLUDED.rank,
                        last_week_position = EXCLUDED.last_week_position,
                        weeks_on_chart = EXCLUDED.weeks_on_chart,
                        weekly_views = EXCLUDED.weekly_views,
                        release_date = EXCLUDED.release_date
                    """,
                    item["id"],
                    rank_number(entry.get("rank")),
                    metrics.get("last_week_position"),
                    metrics.get("weeks_on_chart"),
                    metrics.get("weekly_views"),
                    metrics.get("release_date"),
                )
                updated += 1
    return {"updated": updated, "chart_entries": len(entries)}


@app.delete("/api/tracked/{item_id}")
async def untrack_item(item_id: int):
    database = require_pool()
    async with database.acquire() as connection:
        result = await connection.execute(
            "UPDATE tracked_items SET is_active = FALSE WHERE id = $1 AND is_active = TRUE",
            item_id,
        )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="Tracked item not found")
    return {"removed": True}
