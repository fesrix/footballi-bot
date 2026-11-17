# -*- coding: utf-8 -*-
"""Collect lightweight YouTube Analytics data into the existing bot state.

This module is deliberately file-based so it can run in a free GitHub Actions
job without a database or another hosted service. Analytics data is delayed by
YouTube, so the query ends two days ago and is refreshed on every scheduled run.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from footballi_uploader import load_state, save_state


ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
LOOKBACK_DAYS = 90
MAX_RESULTS_PER_PAGE = 200
SNAPSHOT_RETENTION = 30
METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,"
    "averageViewPercentage,subscribersGained,likes,comments,shares"
)


class AnalyticsUnavailable(RuntimeError):
    """Raised when the configured OAuth token cannot read Analytics."""


def _get_credentials() -> Credentials:
    token_raw = os.environ.get("YOUTUBE_TOKEN", "").strip()
    if not token_raw:
        raise AnalyticsUnavailable("YOUTUBE_TOKEN is missing")

    try:
        token_info = json.loads(token_raw)
        credentials = Credentials.from_authorized_user_info(token_info)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AnalyticsUnavailable("YOUTUBE_TOKEN is not valid JSON") from exc

    if not credentials.valid:
        if not credentials.refresh_token:
            raise AnalyticsUnavailable("YOUTUBE_TOKEN has no refresh token")
        credentials.refresh(Request())

    if not credentials.has_scopes([ANALYTICS_SCOPE]):
        raise AnalyticsUnavailable(
            "YOUTUBE_TOKEN does not include yt-analytics.readonly; regenerate it with refresh_token.yml"
        )
    return credentials


def _date_window(now=None):
    now = now or datetime.now(timezone.utc)
    # YouTube Analytics data can lag by 48–72 hours. Avoid querying partial days.
    end = (now - timedelta(days=2)).date()
    start = end - timedelta(days=LOOKBACK_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _query_video_rows(analytics, start_date: str, end_date: str) -> list[dict]:
    rows = []
    start_index = 1
    while True:
        response = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics=METRICS,
            dimensions="video",
            sort="-estimatedMinutesWatched",
            maxResults=MAX_RESULTS_PER_PAGE,
            startIndex=start_index,
        ).execute()

        headers = [item["name"] for item in response.get("columnHeaders", [])]
        page = [dict(zip(headers, row)) for row in response.get("rows", [])]
        rows.extend(page)
        if len(page) < MAX_RESULTS_PER_PAGE:
            break
        start_index += len(page)
    return rows


def _as_number(value):
    try:
        number = float(value)
        return int(number) if number.is_integer() else round(number, 3)
    except (TypeError, ValueError):
        return 0


def _merge_rows(state: dict, rows: list[dict], start_date: str, end_date: str) -> int:
    analytics_state = state.setdefault("analytics", {})
    videos = analytics_state.setdefault("videos", {})
    synced_at = datetime.now(timezone.utc).isoformat()
    snapshot_key = f"{start_date}:{end_date}"
    updated = 0

    for row in rows:
        video_id = str(row.get("video", "")).strip()
        if not video_id:
            continue
        metrics = {
            key: _as_number(row.get(key, 0))
            for key in METRICS.split(",")
        }
        item = videos.setdefault(video_id, {"snapshots": []})
        item.setdefault("video_id", video_id)
        item.setdefault("source", "youtube")
        item["last_synced_at"] = synced_at
        item["period_start"] = start_date
        item["period_end"] = end_date
        item["metrics"] = metrics

        snapshots = [
            snapshot for snapshot in item.get("snapshots", [])
            if snapshot.get("window") != snapshot_key
        ]
        snapshots.append({
            "window": snapshot_key,
            "synced_at": synced_at,
            "metrics": metrics,
        })
        item["snapshots"] = snapshots[-SNAPSHOT_RETENTION:]
        updated += 1

    analytics_state["last_sync_at"] = synced_at
    analytics_state["period_start"] = start_date
    analytics_state["period_end"] = end_date
    analytics_state["video_count"] = len(videos)
    analytics_state["last_row_count"] = len(rows)
    return updated


def sync_analytics(state: dict | None = None) -> int:
    state = state if state is not None else load_state()
    start_date, end_date = _date_window()
    credentials = _get_credentials()
    analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
    rows = _query_video_rows(analytics, start_date, end_date)
    updated = _merge_rows(state, rows, start_date, end_date)
    _enrich_video_metadata(state, credentials, [str(row.get("video")) for row in rows if row.get("video")])
    save_state(state)
    print(
        f"[ANALYTICS] Synced {updated} video rows "
        f"({start_date} → {end_date}); tracked videos: {state['analytics']['video_count']}"
    )
    return updated


def _enrich_video_metadata(state: dict, credentials: Credentials, video_ids: list[str]) -> None:
    """Attach current YouTube title/category/date and recent statistics."""
    if not video_ids:
        return
    from footballi_uploader import detect_primary_category

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    videos = state.setdefault("analytics", {}).setdefault("videos", {})
    for offset in range(0, len(video_ids), 50):
        chunk = [x for x in video_ids[offset:offset + 50] if x]
        response = youtube.videos().list(
            part="snippet,statistics", id=",".join(chunk), maxResults=50
        ).execute()
        for entry in response.get("items", []):
            video_id = entry["id"]
            snippet = entry.get("snippet", {})
            statistics = entry.get("statistics", {})
            record = videos.setdefault(video_id, {"snapshots": []})
            title = snippet.get("title", "")
            record["video_id"] = video_id
            record["title"] = title
            record["category"] = detect_primary_category(title)
            record["published_at"] = snippet.get("publishedAt")
            record["recent_statistics"] = {
                key: _as_number(statistics.get(key, 0))
                for key in ("viewCount", "likeCount", "commentCount")
            }


def main() -> int:
    try:
        sync_analytics()
    except AnalyticsUnavailable as exc:
        print(f"[ANALYTICS-SKIP] {exc}")
    except HttpError as exc:
        print(f"[ANALYTICS-WARN] YouTube Analytics API request failed: {exc}")
        return 1
    except Exception as exc:
        print(f"[ANALYTICS-WARN] Analytics sync failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

