import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


ROOT = Path(__file__).resolve().parent
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_REGION = "US"
YOUTUBE_MUSIC_CATEGORY_ID = "10"


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


def youtube_api_request(parameters: dict[str, str]) -> dict:
    api_key = os.environ.get("YOUTUBE_DATA_API_KEY", "").strip()
    if not api_key:
        raise ChartUnavailable(
            "Add YOUTUBE_DATA_API_KEY under Project Settings → Vars, then reload popular music videos."
        )

    query = urlencode({**parameters, "key": api_key})
    request = Request(
        f"{YOUTUBE_API_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "YouTubeChartsTracker/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
            reason = payload.get("error", {}).get("errors", [{}])[0].get("reason", "")
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            reason = ""

        if reason == "quotaExceeded":
            message = "YouTube Data API quota is exhausted. Try again after the quota resets."
        elif reason in {"accessNotConfigured", "serviceDisabled"}:
            message = "Enable YouTube Data API v3 in the Google Cloud project for this API key."
        elif reason in {"keyInvalid", "invalid"}:
            message = "YOUTUBE_DATA_API_KEY is invalid. Check the key in the project environment variables."
        else:
            message = "YouTube Data API rejected the request. Check that the API is enabled and the key is valid."
        raise ChartUnavailable(message) from error
    except (URLError, TimeoutError) as error:
        raise ChartUnavailable("YouTube Data API could not be reached. Please try again shortly.") from error
    except json.JSONDecodeError as error:
        raise ChartUnavailable("YouTube Data API returned an unreadable response.") from error


def video_entry(video: dict, rank: int | None = None) -> dict | None:
    video_id = video.get("id")
    if not isinstance(video_id, str) or not video_id:
        return None

    snippet = video.get("snippet") or {}
    statistics = video.get("statistics") or {}
    channel = snippet.get("channelTitle") or "Unknown channel"
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail = next(
        (thumbnails[size]["url"] for size in ("maxres", "high", "medium", "default")
         if thumbnails.get(size, {}).get("url")),
        None,
    )
    view_count = statistics.get("viewCount")

    return {
        "rank": str(rank) if rank is not None else None,
        "title": snippet.get("title") or "Untitled video",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "artists": [channel],
        "thumbnail": thumbnail,
        "metrics": {
            "view_count": str(view_count) if view_count is not None else None,
            "weekly_views": str(view_count) if view_count is not None else None,
            "release_date": (snippet.get("publishedAt") or "")[:10] or None,
        },
    }


def fetch_popular_music() -> list[dict]:
    payload = youtube_api_request(
        {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "videoCategoryId": YOUTUBE_MUSIC_CATEGORY_ID,
            "regionCode": YOUTUBE_REGION,
            "maxResults": "50",
        }
    )
    videos = payload.get("items") or []
    entries = [entry for index, video in enumerate(videos, start=1) if (entry := video_entry(video, index))]
    if not entries:
        raise ChartUnavailable("YouTube returned no popular music videos for this region.")
    return entries


def youtube_video_id(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/").split("/")[0] or None
    if parsed.hostname in {"youtube.com", "www.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            return parts[1]
    return None


def fetch_video_details(video_ids: list[str]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    for start in range(0, len(unique_ids), 50):
        batch = unique_ids[start : start + 50]
        payload = youtube_api_request(
            {"part": "snippet,statistics", "id": ",".join(batch)}
        )
        for video in payload.get("items") or []:
            entry = video_entry(video)
            if entry:
                entries[video["id"]] = entry
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
        entries = await asyncio.to_thread(fetch_popular_music)
        return [entry for entry in entries if entry.get("url") and entry.get("title")]
    except ChartUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="Popular music videos could not be loaded. Please try again shortly.") from error


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
        entries = await asyncio.to_thread(fetch_popular_music)
        async with database.acquire() as connection:
            tracked = await connection.fetch("SELECT id, video_url FROM tracked_items WHERE is_active = TRUE")
        tracked_ids = [youtube_video_id(item["video_url"]) for item in tracked]
        details = await asyncio.to_thread(fetch_video_details, tracked_ids)
    except ChartUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="Popular music videos could not be refreshed.") from error

    chart_by_id = {
        youtube_video_id(entry["url"]): entry
        for entry in entries
        if youtube_video_id(entry["url"])
    }

    updated = 0
    async with database.acquire() as connection:
        async with connection.transaction():
            for item in tracked:
                video_id = youtube_video_id(item["video_url"])
                entry = details.get(video_id)
                if not entry:
                    continue
                entry["rank"] = chart_by_id.get(video_id, {}).get("rank")
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
