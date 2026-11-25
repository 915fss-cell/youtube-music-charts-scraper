from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROW_SELECTOR = "ytmc-entry-row.style-scope.ytmc-chart-table-v2"


def _text(locator) -> str:
    try:
        return locator.inner_text().strip()
    except Exception:
        return ""


def _metric_number(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.strip().upper().replace(",", "").replace(" ", "")
    multiplier = 1
    if normalized.endswith("B"):
        multiplier, normalized = 1_000_000_000, normalized[:-1]
    elif normalized.endswith("M"):
        multiplier, normalized = 1_000_000, normalized[:-1]
    elif normalized.endswith("K"):
        multiplier, normalized = 1_000, normalized[:-1]
    normalized = re.sub(r"[^0-9.]", "", normalized)
    if not normalized:
        return None
    try:
        return int(Decimal(normalized) * multiplier)
    except InvalidOperation:
        return None


def _endpoint_url(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        endpoint = json.loads(raw).get("urlEndpoint", {}).get("url")
    except (json.JSONDecodeError, AttributeError, TypeError):
        endpoint = None
    return urljoin("https://music.youtube.com/", endpoint) if endpoint else None


def _ids_from_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    video_id = query.get("v", [None])[0]
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0] or None
    channel_id = None
    match = re.search(r"/(?:channel|browse)/([A-Za-z0-9_-]+)", parsed.path)
    if match:
        channel_id = match.group(1)
    return video_id, channel_id


def _read_rows(page, chart_type: str) -> list[dict]:
    rows = page.locator(ROW_SELECTOR)
    count = rows.count()
    entries: list[dict] = []

    for index in range(count):
        row = rows.nth(index)
        title_node = row.locator("#entity-title")
        title = _text(title_node)
        if not title:
            continue

        url = _endpoint_url(title_node.get_attribute("endpoint"))
        video_id, channel_id = _ids_from_url(url)
        rank_text = _text(row.locator("#rank"))
        rank_match = re.search(r"\d+", rank_text)
        rank = int(rank_match.group()) if rank_match else index + 1
        artists_node = row.locator("#artist-names .artistName")
        artists = [_text(artists_node.nth(i)) for i in range(artists_node.count())]
        artists = [artist for artist in artists if artist]
        metric_nodes = row.locator("div.metric.content.center")
        raw_metrics = [_text(metric_nodes.nth(i)) for i in range(metric_nodes.count())]
        if chart_type in {"TopMusicVideos", "TopShortsSongs"}:
            metrics = {
                "artists": artists,
                "yesterday_position": raw_metrics[0] if len(raw_metrics) > 0 else None,
                "days_on_chart": raw_metrics[1] if len(raw_metrics) > 1 else None,
            }
            if chart_type == "TopShortsSongs":
                metrics["shorts_creations"] = raw_metrics[2] if len(raw_metrics) > 2 else None
        elif chart_type == "TrendingVideos":
            metrics = {
                "artists": artists,
                "release_date": raw_metrics[0] if raw_metrics else None,
            }
        else:
            metrics = {
                "artists": artists,
                "release_date": raw_metrics[0] if len(raw_metrics) > 0 else None,
                "last_week_position": raw_metrics[1] if len(raw_metrics) > 1 else None,
                "weeks_on_chart": raw_metrics[2] if len(raw_metrics) > 2 else None,
                "weekly_views": raw_metrics[3] if len(raw_metrics) > 3 else None,
            }
        thumbnail = row.locator("#thumbnail").get_attribute("src")
        entry_id = video_id or channel_id or (urlparse(url).path.strip("/") if url else "")
        if not entry_id:
            entry_id = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")

        entries.append(
            {
                "entry_id": entry_id,
                "video_id": video_id,
                "channel_id": channel_id,
                "title": title[:300],
                "rank": rank,
                "url": url,
                "thumbnail": thumbnail,
                "artists": artists,
                "view_count": _metric_number(metrics.get("weekly_views")),
                "like_count": None,
                "comment_count": None,
                "metrics": metrics,
            }
        )

    return entries


def _chart_url(chart_type: str, market: str, chart_date: str) -> str:
    chart_slugs = {
        "TrendingVideos": "TrendingVideos",
        "TopMusicVideos": "TopVideos",
        "TopSongs": "TopSongs",
        "TopArtists": "TopArtists",
        "TopShortsSongs": "TopShortsSongs",
    }
    chart_slug = chart_slugs.get(chart_type)
    if not chart_slug:
        raise ValueError(f"Unsupported YouTube Charts type: {chart_type}")
    if chart_type == "TrendingVideos":
        chart_path = f"{market.lower()}/RightNow"
    else:
        frequency = "daily" if chart_type in {"TopMusicVideos", "TopShortsSongs"} else "weekly"
        chart_path = f"{market.lower()}/{frequency}/{chart_date}"
    return f"https://charts.youtube.com/charts/{chart_slug}/{chart_path}"


def fetch_youtube_chart(chart_type: str, market: str, chart_date: str) -> list[dict]:
    url = _chart_url(chart_type, market, chart_date)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.locator(ROW_SELECTOR).first.wait_for(state="attached", timeout=45_000)
            except PlaywrightTimeoutError as error:
                raise RuntimeError("YouTube Charts did not return entries for this date and market.") from error

            previous_count = -1
            stable_scrolls = 0
            for _ in range(24):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(250)
                current_count = page.locator(ROW_SELECTOR).count()
                if current_count == previous_count:
                    stable_scrolls += 1
                    if stable_scrolls >= 3:
                        break
                else:
                    stable_scrolls = 0
                    previous_count = current_count

            entries = _read_rows(page, chart_type)
            if not entries:
                raise RuntimeError("YouTube Charts returned an empty chart for this date and market.")
            return entries
        finally:
            browser.close()
