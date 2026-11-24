# -*- coding: utf-8 -*-
import os
import re
import json
import time
import inspect
from collections import defaultdict
import subprocess
import sys
import tempfile
import math
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from config import (
    ROUTINE_DAILY_LIMITS,
    ROUTINE_SPACING_MINUTES,
    PRIORITY_SUMMARY_MAX_AGE_HOURS,
    PRIORITY_EDITORIAL_MAX_AGE_HOURS,
    MATCH_DAY_PRIORITY_BEFORE_HOURS,
    MATCH_DAY_PRIORITY_AFTER_HOURS,
    PRIORITY_EDITORIAL_CATEGORIES,
    MATCH_FAN_KEYWORDS,
    MATCH_PREVIEW_KEYWORDS,
    MATCH_INTERVIEW_KEYWORDS,
    IRANIAN_DOMESTIC_CLUBS,
    FOOTBALL_BARTAR_KEYWORDS,
    SHABHAYE_FOOTBALLI_KEYWORDS,
    MAIN_UPLOAD_WINDOWS,
    SHORT_UPLOAD_WINDOWS,
    STATE_FILE,
    SUMMARY_KEYWORDS,
    VIDEOCAST_KEYWORDS,
    POSHTE_KHAT_KEYWORDS,
    PLAYLIST_DEFINITIONS,
)
from scrapers import (
    crawl_footballi_videos,
    fetch_instagram_reels,
    normalize_persian,
    is_foreign_or_blacklisted,
    is_strictly_domestic_short,
    is_video_too_old,
    extract_numeric_id,
    extract_video_page_details,
    fetch_live_fixtures,
    MIN_ABSOLUTE_VIDEO_ID,
)
import ai_seo
import media_utils
import scrapers
import originals_policy as originals
import telegram_review as telegram
from programme_hold import blocked_candidate_programme, programme_hold_enabled
from learning import build_policy, defer_for_timing, ranking, timestamp, title_choice, topic_for


def is_trusted_source_url(value: str) -> bool:
    try:
        host = (urlsplit(str(value).strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in {"footballi.net", "www.footballi.net", "instagram.com", "www.instagram.com"}

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

MAX_MAIN_QUEUE_SIZE = 15
MAX_SHORTS_QUEUE_SIZE = 5
MIN_HORIZONTAL_DURATION_SECONDS = 180
MIN_HORIZONTAL_HEIGHT_PX = 720  # رد خودکار ویدیوهای بی‌کیفیت 360p و 480p
MIN_SHORTS_WIDTH_PX = 720       # شورتس‌ها فقط 720p و 1080p
# Leave time for a final checkpoint before the workflow's 595-second hard timeout.
PUBLISHING_BUDGET_SECONDS = 510
MAX_TELEGRAM_PROPOSALS_PER_RUN = 10
MIN_TIME_TO_START_CANDIDATE = 85


def get_exact_video_dimensions(video_path: str) -> tuple:
    return media_utils.inspect_video_file(video_path)


def get_tehran_reset_key() -> str:
    return datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d")


def get_quota_reset_key() -> str:
    # YouTube resets API quota at midnight Pacific, including daylight saving.
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def is_in_upload_window(category: str, tehran_now) -> bool:
    minute = tehran_now.hour * 60 + tehran_now.minute
    windows = SHORT_UPLOAD_WINDOWS if category == "SHORTS" else MAIN_UPLOAD_WINDOWS
    return any(start <= minute < end for start, end in windows)


def is_priority_summary(item, now):
    source_url = item.get("source_url") or item.get("url", "")
    if (item.get("source") != "footballi_web"
            or item.get("rights_status") != "internal_authorized"
            or not is_trusted_source_url(source_url)
            or (urlsplit(source_url).hostname or "").lower() not in {"footballi.net", "www.footballi.net"}
            or not extract_numeric_id(source_url)):
        return False
    title = normalize_persian(item.get("title", "")).lower().strip()
    if not any(title.startswith(normalize_persian(keyword).lower() + " ") for keyword in SUMMARY_KEYWORDS):
        return False

    # Prefer source publication and first-seen timestamps. A recent discovery
    # timestamp is also valid because the crawler only sets it for a page
    # inspected in the current run.
    published = timestamp(item.get("source_published_at") or item.get("published_at"))
    seen = timestamp(item.get("source_first_seen_at"))
    discovered = timestamp(item.get("discovery_timestamp"))
    observed = published or seen or discovered
    return observed is not None and timedelta(0) <= now - observed <= timedelta(
        hours=PRIORITY_SUMMARY_MAX_AGE_HOURS
    )


def _item_text(item) -> str:
    return normalize_persian(" ".join(str(item.get(key, "")) for key in
                                       ("title", "description", "source_title"))).casefold()


def _fresh_item_timestamp(item):
    return timestamp(item.get("source_published_at") or item.get("published_at")
                     or item.get("source_first_seen_at") or item.get("discovery_timestamp"))


def _fixture_team_match(text: str, fixture: dict) -> list:
    matches = []
    for key in ("home_team", "away_team"):
        team = normalize_persian(fixture.get(key, "")).casefold()
        if team and team in text:
            matches.append(key)
    return matches


def _match_day_priority(item, fixtures, now):
    if item.get("rights_status") != "internal_authorized" or not is_trusted_source_url(
            item.get("source_url") or item.get("url", "")):
        return None
    text = _item_text(item)
    title_text = normalize_persian(" ".join(str(item.get(key, "")) for key in
                                              ("title", "source_title"))).casefold()
    keywords = (
        [("fan", MATCH_FAN_KEYWORDS), ("preview", MATCH_PREVIEW_KEYWORDS),
         ("interview", MATCH_INTERVIEW_KEYWORDS)]
    )
    relation = next(((kind, keyword) for kind, values in keywords
                     for keyword in values if normalize_persian(keyword).casefold() in title_text), None)
    if not relation:
        return None
    published = _fresh_item_timestamp(item)
    if not published:
        return None
    for fixture in fixtures:
        kickoff = timestamp(fixture.get("kickoff_at"))
        if not kickoff:
            continue
        before = kickoff - timedelta(hours=MATCH_DAY_PRIORITY_BEFORE_HOURS)
        after = kickoff + timedelta(hours=MATCH_DAY_PRIORITY_AFTER_HOURS)
        if not before <= now <= after:
            continue
        team_matches = _fixture_team_match(text, fixture)
        if not team_matches:
            continue
        if now - published > timedelta(hours=48):
            continue
        return {
            "priority_class": "match_day",
            "priority_reason": f"{relation[0]}:{relation[1]}",
            "fixture_id": fixture["fixture_id"],
            "fixture_relation": "both_teams" if len(team_matches) == 2 else team_matches[0],
            "priority_until": after.isoformat(),
            "home_team": fixture.get("home_team"),
            "away_team": fixture.get("away_team"),
            "competition": fixture.get("competition"),
            "kickoff_at": fixture.get("kickoff_at"),
        }
    return None


def priority_item_context(item, state, now):
    if is_priority_summary(item, now):
        return {"priority_class": "summary", "priority_reason": "fresh_summary"}
    category = item.get("category") or detect_primary_category(item.get("title", ""))
    source = item.get("source_url") or item.get("url", "")
    if (category in PRIORITY_EDITORIAL_CATEGORIES
            and category != "SUMMARY"
            and item.get("rights_status") == "internal_authorized"
            and is_trusted_source_url(source)):
        observed = _fresh_item_timestamp(item)
        if observed and now - observed <= timedelta(hours=PRIORITY_EDITORIAL_MAX_AGE_HOURS):
            return {"priority_class": "editorial", "priority_reason": f"category:{category}"}
    return _match_day_priority(item, state.get("live_fixtures", {}).get("fixtures", []), now)


def is_priority_item(item, state, now):
    return bool(priority_item_context(item, state, now))


def daily_publishing_counts(state, now):
    today = now.date().isoformat()
    # Publications are already checkpointed on success. Reconstruct counts from
    # that log so old state needs no migration and midnight resets correctly.
    counts = {"main": 0, "short": 0, "priority_summaries": 0,
              "priority_editorial": 0, "priority_match_day": 0}
    for metadata in state.get("published_metadata", {}).values():
        published = timestamp(metadata.get("published_at"))
        if not published or published.astimezone(now.tzinfo).date().isoformat() != today:
            continue
        if metadata.get("priority_class") == "summary" or metadata.get("priority_summary"):
            counts["priority_summaries"] += 1
        elif metadata.get("priority_class") == "editorial":
            counts["priority_editorial"] += 1
        elif metadata.get("priority_class") == "match_day":
            counts["priority_match_day"] += 1
        else:
            fmt = metadata.get("format") or ("short" if metadata.get("category") == "SHORTS" else "main")
            if fmt in ROUTINE_DAILY_LIMITS:
                counts[fmt] += 1
    return counts


def last_routine_timestamp(state, fmt):
    # The legacy fields only include routine uploads made before this policy.
    # Priority summaries never update them.
    key = "last_short_upload_timestamp" if fmt == "short" else "last_upload_timestamp"
    return float(state.get(key, 0) or 0)


def publication_block(item, category, state, policy, now, *, is_short=None):
    if telegram.preview_only():
        return "Telegram review preview only"
    key = telegram.source_id(item)
    review = telegram.review_state(state)["items"].get(key, {})
    if review.get("status") in {"upload_uncertain", "published"}:
        return "YouTube upload already started"
    if key in telegram.rejected_sources(state):
        return "rejected in Telegram"
    if originals.originals_only():
        return originals.admission(item, telegram.approvals_for(state))[1]
    if not is_priority_item(item, state, now):
        short = category == "SHORTS" if is_short is None else is_short
        fmt = "short" if short else "main"
        if daily_publishing_counts(state, now)[fmt] >= ROUTINE_DAILY_LIMITS[fmt]:
            return f"daily {fmt} upload cap"
        if not is_in_upload_window("SHORTS" if short else category, now):
            return "outside the Tehran publish window"
        if now.timestamp() - last_routine_timestamp(state, fmt) < ROUTINE_SPACING_MINUTES[fmt] * 60:
            return "minimum spacing for this format"
        if defer_for_timing(category, short, policy, now):
            published = timestamp(item.get("source_published_at") or item.get("published_at"))
            if not published or now - published < timedelta(hours=47):
                return "Analytics preferred time"
    return ""


def is_duplicate_title(candidate_title: str, existing_norm_titles: set) -> bool:
    clean = re.sub(r"\|.*$", "", candidate_title).replace("#Shorts", "").replace("#shorts", "").strip()
    norm_cand = normalize_persian(clean).lower()
    if not norm_cand:
        return True
    if norm_cand in existing_norm_titles:
        return True
    for ext in existing_norm_titles:
        clean_ext = re.sub(r"\|.*$", "", ext).replace("#shorts", "").strip()
        if not clean_ext:
            continue
        if norm_cand == clean_ext:
            return True
        if len(norm_cand) > 15 and len(clean_ext) > 15:
            if SequenceMatcher(None, norm_cand, clean_ext).ratio() >= 0.85:
                return True
    return False


def load_state() -> dict:
    default_state = {
        "uploaded_urls": [],
        "rejected_urls": [],
        "seen_mp4s": [],
        "uploaded_titles": [],
        "seen_raw_titles": [],
        "pending_queue": [],
        "daily_uploads": {"date": "", "count": 0},
        "last_upload_timestamp": 0,
        "last_short_upload_timestamp": 0,
        "live_fixtures": {"fixtures": [], "fetched_at": "", "parser_version": 1},
        "upload_limit_cooldown_until": 0,
        "quota_exhausted_date": "",
        "max_seen_video_id": MIN_ABSOLUTE_VIDEO_ID,
        "last_apify_check": 0,
        "playlists": {},
        "playlist_assignments": {},
        "processed_comments": [],
        "ignored_comments": [],
        "deleted_comments": [],
        "analytics": {},
        "published_metadata": {},
        "rights_migration_version": 0,
        "priority_migration_version": 0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State root must be an object")
            for k, v in default_state.items():
                if k not in data:
                    data[k] = v
                elif isinstance(v, (list, dict)) and not isinstance(data[k], type(v)):
                    raise ValueError(f"Invalid state type for {k}")
            if int(data.get("rights_migration_version", 0) or 0) < 1:
                for item in data.get("pending_queue", []):
                    if isinstance(item, dict) and not item.get("rights_status"):
                        source_url = item.get("source_url") or item.get("url") or ""
                        trusted = is_trusted_source_url(source_url)
                        item["rights_status"] = "internal_authorized" if trusted else "quarantine"
                        if trusted:
                            item.setdefault("source_url", item.get("url", ""))
                            item.setdefault("content_type", "short" if item.get("is_short") else "video")
                data["rights_migration_version"] = 1
            if int(data.get("priority_migration_version", 0) or 0) < 1:
                for metadata in data.get("published_metadata", {}).values():
                    if not isinstance(metadata, dict) or metadata.get("priority_class") != "match_day":
                        continue
                    title_text = normalize_persian(" ".join(str(metadata.get(key, ""))
                                                               for key in ("title", "source_title"))).casefold()
                    match_signal = any(normalize_persian(keyword).casefold() in title_text
                                       for keyword in MATCH_FAN_KEYWORDS + MATCH_PREVIEW_KEYWORDS + MATCH_INTERVIEW_KEYWORDS)
                    if not match_signal:
                        for key in ("priority_class", "priority_reason", "fixture_id",
                                    "fixture_relation", "priority_until", "home_team",
                                    "away_team", "competition", "kickoff_at"):
                            metadata.pop(key, None)
                        metadata["priority_summary"] = False
                data["priority_migration_version"] = 1
            return data
        except Exception as e:
            # Never replace damaged upload history with an empty successful state.
            raise RuntimeError(f"Cannot safely load {STATE_FILE}; preserving original") from e
    return default_state


def save_state(state: dict):
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    fd, tmp_path = tempfile.mkstemp(prefix=".footballi-state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_youtube_client():
    token_raw = os.environ.get("YOUTUBE_TOKEN", "").strip()
    if not token_raw:
        raise RuntimeError("YOUTUBE_TOKEN secret is missing!")
    token_info = json.loads(token_raw)
    token_info.pop("scopes", None)
    token_info.pop("scope", None)
    creds = Credentials.from_authorized_user_info(token_info)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("YouTube credentials have no usable refresh token")
    return build("youtube", "v3", credentials=creds)


load_history = load_state
save_history = save_state
get_authenticated_service = get_youtube_client
get_youtube_service = get_youtube_client


def detect_primary_category(title: str, description: str = "", is_short: bool = False) -> str:
    if is_short:
        return "SHORTS"
    clean_title = re.sub(r"\|.*$", "", title).strip()
    norm_title = normalize_persian(clean_title).lower()
    for kw in SUMMARY_KEYWORDS:
        if normalize_persian(kw).lower() in norm_title:
            return "SUMMARY"
    for kw in VIDEOCAST_KEYWORDS:
        if normalize_persian(kw).lower() in norm_title:
            return "VIDEOCAST"
    for kw in POSHTE_KHAT_KEYWORDS:
        if normalize_persian(kw).lower() in norm_title:
            return "POSHTE_KHAT"
    for kw in FOOTBALL_BARTAR_KEYWORDS:
        if normalize_persian(kw).lower() in norm_title:
            return "FOOTBALL_BARTAR"
    for kw in SHABHAYE_FOOTBALLI_KEYWORDS:
        if normalize_persian(kw).lower() in norm_title:
            return "SHABHAYE_FOOTBALLI"
    return "VIDEO"


POSHTE_KHAT_TITLE = "پُشتِ‌ خَط"
_PERSIAN_MARKS = r"[\u064B-\u065F\u0670]*"
_POSHTE_KHAT_TITLE_RE = re.compile(
    r"(?<!\w)" + _PERSIAN_MARKS.join("پشت") + _PERSIAN_MARKS
    + r"[\s\u200c\u200d_-]*" + _PERSIAN_MARKS.join("خط") + _PERSIAN_MARKS + r"(?!\w)"
)


def footballi_show(item):
    for title in (item.get("title") or "", item.get("source_title") or ""):
        if _POSHTE_KHAT_TITLE_RE.search(title):
            return "POSHTE_KHAT"
        if any(detect_primary_category(part) == "VIDEOCAST" for part in title.split("|")):
            return "VIDEOCAST"
    return ""


def format_upload_title(title, candidate, category):
    if category != "POSHTE_KHAT" and footballi_show(candidate) != "POSHTE_KHAT":
        return title[:100]
    title = _POSHTE_KHAT_TITLE_RE.sub(POSHTE_KHAT_TITLE, title)
    if POSHTE_KHAT_TITLE not in title[:100]:
        topic = _POSHTE_KHAT_TITLE_RE.sub("", title).strip(" |–—:-‌")
        title = f"{POSHTE_KHAT_TITLE} | {topic}" if topic else POSHTE_KHAT_TITLE
    return title[:100]


def _playlist_text(value: str) -> str:
    return normalize_persian(value.split("|", 1)[0]).casefold()


def _has_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(_playlist_text(phrase))}(?!\w)", text))


def detect_all_matching_playlists(title: str, description: str = "", is_short: bool = False,
                                  *, source_title: str = "") -> list:
    # The original headline and display title provide evidence. Generic
    # description footers and tags can mention unrelated clubs.
    texts = [_playlist_text(t) for t in (source_title, title) if t]
    matches = ["SHORTS"] if is_short else []
    for key, definition in PLAYLIST_DEFINITIONS.items():
        if key == "SHORTS":
            continue
        if key == "INTERVIEW" and "SUMMARY" in matches:
            continue
        for text in texts:
            if key == "ESTEGHLAL":
                text = re.sub(r"\bاستقلال (?:خوزستان|ملاثانی)\b", "", text)
            if any(_has_phrase(text, kw) for kw in definition["keywords"]):
                matches.append(key)
                break
            if key == "INTERVIEW" and re.match(r"^[آ-ی][آ-ی ]{2,35}\s*[:：]", text):
                if not any(code in matches for code in ("VIDEOCAST", "POSHTE_KHAT", "FOOTBALL_BARTAR",
                                                         "SHABHAYE_FOOTBALLI")):
                    matches.append(key)
                    break
    return matches


def video_playlist_keys(video, state):
    video_id = video["video_id"]
    metadata = state.get("published_metadata", {}).get(video_id, {})
    fmt = metadata.get("format")
    if fmt:
        is_short = fmt == "short"
    elif metadata.get("category"):
        is_short = metadata["category"] == "SHORTS"
    else:
        is_short = state.get("analytics", {}).get("videos", {}).get(video_id, {}).get("format") == "short"
    return detect_all_matching_playlists(video["title"], is_short=is_short,
                                         source_title=metadata.get("source_title", ""))


def playlist_demand(videos, state):
    evidence = defaultdict(set)
    for video in videos:
        if not video.get("public"):
            continue
        for key in video_playlist_keys(video, state):
            evidence[key].add(video["video_id"])
    return {key: len(ids) for key, ids in evidence.items()}


def ensure_all_playlists(youtube, state: dict, demand: dict | None = None, max_new: int = 2) -> dict:
    playlists_state = state.setdefault("playlists", {})
    demand = demand or {}
    missing = [key for key, definition in PLAYLIST_DEFINITIONS.items()
               if not playlists_state.get(key)
               and demand.get(key, 0) >= definition.get("minimum_public_videos", 0)]
    if not missing:
        return playlists_state

    existing = {}
    token = None
    try:
        while True:
            page = youtube.playlists().list(part="snippet,status", mine=True,
                                            maxResults=50, pageToken=token).execute()
            for playlist in page.get("items", []):
                if playlist.get("status", {}).get("privacyStatus") == "public":
                    existing[normalize_persian(playlist["snippet"]["title"]).casefold()] = playlist["id"]
            token = page.get("nextPageToken")
            if not token:
                break
    except HttpError as exc:
        if "quotaExceeded" in str(exc):
            state["quota_exhausted_date"] = get_quota_reset_key()
        print(f"[PLAYLIST-WARN] Could not list channel playlists ({exc.resp.status}).")
        return playlists_state

    for key in missing:
        definition = PLAYLIST_DEFINITIONS[key]
        title = definition["title"]
        aliases = [normalize_persian(t).casefold() for t in [title, *definition.get("legacy_titles", [])]]
        match = next((existing[name] for name in aliases if name in existing), None)
        if match:
            playlists_state[key] = match
            save_state(state)
            continue
        if max_new <= 0:
            continue

        max_new -= 1
        try:
            created = youtube.playlists().insert(
                part="snippet,status",
                body={"snippet": {"title": title, "description": definition["description"]},
                      "status": {"privacyStatus": "public"}},
            ).execute()
        except HttpError as exc:
            if "quotaExceeded" in str(exc):
                state["quota_exhausted_date"] = get_quota_reset_key()
            print(f"[PLAYLIST-WARN] Could not create {key} ({exc.resp.status}).")
            break
        playlists_state[key] = created["id"]
        save_state(state)
        print(f"[PLAYLIST-CREATED] {key}: {title} ({created['id']})")
    return playlists_state


def add_video_to_playlists(youtube, video_id: str, playlist_keys: list, playlists_map: dict,
                           state: dict, max_checks: int = 3) -> int:
    assignments = state.setdefault("playlist_assignments", {})
    already_assigned = set(assignments.get(video_id, []))
    checked = 0

    # Keep format and highlights first, then give evidence-backed new
    # playlists a chance before the more general editorial categories.
    ordered_keys = sorted(playlist_keys, key=lambda key: (
        0 if key in {"SHORTS", "SUMMARY"} else
        1 if PLAYLIST_DEFINITIONS[key].get("minimum_public_videos") else 2))
    for key in ordered_keys:
        if checked >= max_checks:
            break
        if key in already_assigned:
            continue
        pl_id = playlists_map.get(key)
        if not pl_id:
            continue
        checked += 1
        try:
            contents = youtube.playlistItems().list(part="id", playlistId=pl_id, videoId=video_id,
                                                   maxResults=1).execute()
            if contents.get("items"):
                already_assigned.add(key)
                continue
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pl_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            already_assigned.add(key)
            print(f"📂 [PLAYLIST-ADD] Added video {video_id} -> {PLAYLIST_DEFINITIONS[key]['title']}")
        except HttpError as e:
            if e.resp.status == 409 or "videoAlreadyInPlaylist" in str(e):
                already_assigned.add(key)
            elif "playlistNotFound" in str(e):
                # A newly-created playlist may not be immediately readable
                # through playlistItems. Keep its ID and retry next run.
                print(f"[PLAYLIST-WARN] Playlist {key} is not readable yet; will retry.")
            elif "quotaExceeded" in str(e):
                state["quota_exhausted_date"] = get_quota_reset_key()
                break
            else:
                print(f"[PLAYLIST-WARN] Could not add {video_id} to {key}: {e}")
        except Exception:
            pass

    if already_assigned:
        assignments[video_id] = sorted(already_assigned)
    return checked


def fetch_channel_existing_videos(youtube, state: dict, max_items: int = 120) -> list:
    videos = []
    try:
        ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch_res.get("items", [])
        if not items:
            return []
        uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        next_page = None
        while len(videos) < max_items:
            pl_res = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_pl,
                maxResults=50,
                pageToken=next_page,
            ).execute()
            for it in pl_res.get("items", []):
                snip = it["snippet"]
                vid_id = snip.get("resourceId", {}).get("videoId")
                title = snip.get("title", "")
                desc = snip.get("description", "")
                if vid_id and title:
                    videos.append({"video_id": vid_id, "title": title, "description": desc})
            next_page = pl_res.get("nextPageToken")
            if not next_page:
                break
        videos = videos[:max_items]
        for start in range(0, len(videos), 50):
            group = videos[start:start + 50]
            details = youtube.videos().list(part="status", id=",".join(row["video_id"] for row in group)).execute()
            public_ids = {item["id"] for item in details.get("items", [])
                          if item["status"].get("privacyStatus") == "public"}
            for row in group:
                row["public"] = row["video_id"] in public_ids
    except HttpError as e:
        if "quotaExceeded" in str(e):
            state["quota_exhausted_date"] = get_quota_reset_key()
            print("⏸️ [QUOTA-PAUSE] Google Cloud daily API quota (10,000 units) is full until midnight Pacific quota reset.")
        else:
            print(f"[WARN] Could not index channel videos: {e}")
    except Exception as e:
        print(f"[WARN] Could not index channel videos: {e}")
    return videos


def organize_existing_channel_videos(youtube, channel_videos: list, playlists_map: dict, state: dict, max_ops_per_run: int = 3):
    ops = 0
    assignments = state.setdefault("playlist_assignments", {})

    matches = [(vid, video_playlist_keys(vid, state)) for vid in channel_videos if vid.get("public")]
    fresh_categories = {key for key in playlists_map if PLAYLIST_DEFINITIONS.get(key, {}).get("minimum_public_videos")}
    matches.sort(key=lambda entry: not any(
        key in fresh_categories and key not in assignments.get(entry[0]["video_id"], [])
        for key in entry[1]))
    for vid, target_keys in matches:
        if ops >= max_ops_per_run or state.get("quota_exhausted_date") == get_quota_reset_key():
            break
        vid_id = vid["video_id"]
        current_keys = set(assignments.get(vid_id, []))
        missing_keys = [key for key in target_keys
                        if key in playlists_map and key not in current_keys]
        if missing_keys:
            ops += add_video_to_playlists(youtube, vid_id, missing_keys, playlists_map, state,
                                          max_checks=max_ops_per_run - ops)


def curate_channel_playlists(youtube, state, channel_videos, *, max_new=2, max_ops=3):
    demand = playlist_demand(channel_videos, state)
    playlists = ensure_all_playlists(youtube, state, demand, max_new=max_new)
    organize_existing_channel_videos(youtube, channel_videos, playlists, state, max_ops_per_run=max_ops)
    save_state(state)
    for key, definition in PLAYLIST_DEFINITIONS.items():
        if "minimum_public_videos" in definition:
            print(f"[PLAYLIST-SUMMARY] {key}: {demand.get(key, 0)} public matches | "
                  f"{playlists.get(key) or 'waiting for enough videos'}")
    return playlists


def run_playlists_only():
    state = load_state()
    youtube = get_youtube_client()
    channel = youtube.channels().list(part="snippet", mine=True).execute()["items"][0]
    print(f"[PLAYLIST] Channel: https://www.youtube.com/channel/{channel['id']}")
    videos = fetch_channel_existing_videos(youtube, state, max_items=180)
    if not any(video.get("public") for video in videos):
        raise RuntimeError("Could not verify public channel videos")
    curate_channel_playlists(youtube, state, videos, max_new=4, max_ops=12)


def normalize_queue_item(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    u = item.get("url") or item.get("page_url") or item.get("link") or ""
    mp4 = item.get("mp4_url") or item.get("video_url") or item.get("mp4") or ""
    title = item.get("title") or item.get("raw_title") or item.get("seo_title") or ""
    title = re.sub(r"\s*\|\s*خلاصه بازی\s*$", "", title).strip()
    poster = item.get("poster_url") or item.get("poster") or item.get("thumbnail") or ""
    is_short = bool(item.get("verified_is_short", item.get("is_short", False)))
    item["is_short"] = is_short
    vid_id = extract_numeric_id(u) or 0
    item["video_id"] = vid_id
    item["url"] = u
    item["mp4_url"] = mp4
    item["title"] = title
    item["poster_url"] = poster
    item["ai_score"] = safe_int(item.get("ai_score"), 6)
    item["site_views"] = safe_int(item.get("site_views"), 0)
    item["category"] = detect_primary_category(title, "", is_short=is_short)
    item.setdefault("source", "unknown")
    item.setdefault("source_url", u)
    item.setdefault("source_published_at", item.get("published_at"))
    item.setdefault("rights_status", "")
    item.setdefault("content_type", "short" if is_short else "video")
    item.setdefault("discovery_timestamp", time.time())
    item["attempt_count"] = safe_int(item.get("attempt_count"), 0) + 1
    return item


def safe_int(value, default=0):
    try:
        return int(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def deduplicate_and_balance_queue(
    queue: list,
    uploaded_urls: set,
    rejected_urls: set,
    seen_mp4s: set,
    known_norm_titles: set,
    max_site_id: int,
    policy: dict | None = None,
    now: datetime | None = None,
    priority_state: dict | None = None,
) -> list:
    main_items = []
    short_items = []
    held_items = []
    hold_enabled = programme_hold_enabled()
    now = now or datetime.now(timezone.utc)
    q_urls = set()
    q_mp4s = set()
    q_titles = set()
    purged = 0
    rejection_counts = {}

    def reject(reason: str):
        nonlocal purged
        purged += 1
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    all_q_ids = [extract_numeric_id(it.get("url", "")) for it in queue if isinstance(it, dict)]
    highest_id = max([max_site_id, MIN_ABSOLUTE_VIDEO_ID] + all_q_ids)

    for raw_item in queue:
        item = normalize_queue_item(raw_item)
        u = item.get("url", "")
        mp4 = item.get("mp4_url", "")
        t = normalize_persian(item.get("title", "")).lower()
        vid_id = extract_numeric_id(u)

        if not u or not mp4 or not t:
            continue
        if item.get("rights_status") != "internal_authorized" or not is_trusted_source_url(item["source_url"]):
            reject("rights_status")
            continue
        if is_video_too_old(item.get("title", ""), published_at=item.get("source_published_at") or item.get("published_at")):
            reject("too_old")
            continue
        if item.get("source") == "telegram_shorts" or "t.me/" in u:
            reject("unsupported_source")
            continue
        if u in uploaded_urls or u in rejected_urls or u in q_urls or mp4 in seen_mp4s or mp4 in q_mp4s:
            continue
        programme = (item.get("programme_hold") or blocked_candidate_programme(item)) if hold_enabled else ""
        if programme:
            observed = _fresh_item_timestamp(item)
            if not observed or now - observed <= timedelta(hours=48):
                if not item.get("programme_hold"):
                    print(f"[PROGRAMME-HOLD] {programme}: candidate {vid_id or u.split('?')[0]}")
                item["programme_hold"] = programme
                item["learning_factor"] = 1
                held_items.append(item)
                q_urls.add(u)
            continue
        item.pop("programme_hold", None)
        if (is_duplicate_title(item.get("title", ""), known_norm_titles | q_titles)
                and not (priority_state and is_priority_item(item, priority_state, now))):
            continue

        if item.get("is_short"):
            if not is_strictly_domestic_short(item.get("title", ""), item.get("description", "")):
                reject("foreign_or_blacklisted_short")
                continue
        else:
            if is_video_too_old(item.get("title", ""), "", video_id=vid_id, max_site_id=highest_id):
                reject("old_video_id")
                continue
            if is_foreign_or_blacklisted(item.get("title", ""), item.get("description", "")):
                reject("foreign_or_blacklisted")
                continue

        q_urls.add(u)
        q_mp4s.add(mp4)
        q_titles.add(t)

        if item.get("is_short") or item.get("category") == "SHORTS":
            short_items.append(item)
        else:
            main_items.append(item)

    if purged > 0:
        print(f"🧹 [QUEUE-PURGE] Removed {purged} old, foreign, or duplicate items from queue.")
    state_rejection_counts = globals().get("_QUEUE_REJECTION_COUNTS")
    if isinstance(state_rejection_counts, dict):
        state_rejection_counts.clear()
        state_rejection_counts.update(rejection_counts)

    policy = policy or {}

    def sort_key(item):
        score, factor = ranking(item, policy, now)
        item["learning_factor"] = round(factor, 3)
        priority = bool(priority_state and is_priority_item(item, priority_state, now))
        fresh = timestamp(item.get("source_published_at") or item.get("published_at")
                          or item.get("source_first_seen_at")
                          or item.get("discovery_timestamp"))
        return (not priority, -fresh.timestamp() if priority and fresh else 0,
                -score, -item["ai_score"], -item["site_views"], -item["video_id"])

    main_items.sort(key=sort_key)
    short_items.sort(key=sort_key)

    # Burst match days can generate more highlights than the routine queue cap.
    # Keep every approved priority summary until it is handled or ages out.
    main_priority = [item for item in main_items
                     if priority_state and is_priority_item(item, priority_state, now)]
    short_priority = [item for item in short_items
                      if priority_state and is_priority_item(item, priority_state, now)]
    routine_main = main_items[len(main_priority):][:MAX_MAIN_QUEUE_SIZE]
    routine_short = short_items[len(short_priority):][:MAX_SHORTS_QUEUE_SIZE]
    return sorted(main_priority + short_priority, key=sort_key) + routine_main + routine_short + held_items


def call_ai_copyright_audit(title: str, desc: str) -> bool:
    if is_foreign_or_blacklisted(title, desc):
        return False
    audit_fn = getattr(ai_seo, "ai_free_copyright_audit", None)
    if audit_fn:
        try:
            sig = inspect.signature(audit_fn)
            if len(sig.parameters) >= 3:
                res = audit_fn(title, desc, "WEB")
            elif len(sig.parameters) == 2:
                res = audit_fn(title, desc)
            else:
                res = audit_fn(title)
            if isinstance(res, bool):
                return res
            if isinstance(res, dict):
                return bool(res.get("approved", True))
        except Exception:
            pass
    return True


def call_prepare_thumbnail(video_path: str, poster_url: str, output_path: str) -> str:
    fn = getattr(media_utils, "prepare_clean_thumbnail", None)
    if not fn:
        return None
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if len(params) >= 3:
            return fn(video_path, poster_url, output_path)
        elif len(params) == 2:
            return fn(poster_url, output_path)
        else:
            return fn(poster_url)
    except Exception as e:
        print(f"[THUMB-WARN] Thumbnail preparation fallback note: {e}")
        return None


def call_generate_seo(title: str, desc: str, category: str, is_short: bool,
                      learning_context: str | None = None,
                      match_context: dict | None = None) -> dict:
    for fn_name in ["generate_seo_metadata", "ai_generate_seo_metadata", "build_seo_package"]:
        fn = getattr(ai_seo, fn_name, None)
        if fn:
            try:
                sig = inspect.signature(fn)
                if 'learning_context' in sig.parameters:
                    kwargs = {"learning_context": learning_context}
                    if "match_context" in sig.parameters:
                        kwargs["match_context"] = match_context
                    return fn(title, desc, category, is_short, **kwargs)
                if len(sig.parameters) >= 4:
                    return fn(title, desc, category, is_short)
                elif len(sig.parameters) == 3:
                    return fn(title, desc, category)
                else:
                    return fn(title, desc)
            except Exception:
                pass

    suffix = " | خلاصه بازی" if category == "SUMMARY" else (" #Shorts" if is_short else " | فوتبال ایران")
    clean_t = title[: 95 - len(suffix)] + suffix
    full_desc = (
        f"{title}\n\n{desc}\n\n"
        "⚽ رسانه رسمی فوتبالی در یوتیوب | پوشش لحظه‌ای اخبار، خلاصه بازی‌ها، ویدیوکست و پُشتِ خَط\n"
        "#فوتبالی #فوتبال_ایران #لیگ_برتر #استقلال #پرسپولیس"
    )
    tags = ["فوتبالی", "فوتبال ایران", "لیگ برتر ایران", "استقلال", "پرسپولیس", "خلاصه بازی"]
    return {"title": clean_t, "description": full_desc, "tags": tags}


def download_candidate(top, path, state, deadline):
    source_url = top.get("source_url") or top["url"]
    attempts = safe_int(top.get("download_failures"))
    candidates = [top["mp4_url"], top.get("alternate_mp4_url")]
    if (attempts and top.get("source") == "footballi_web" and is_trusted_source_url(source_url)
            and extract_numeric_id(source_url) and deadline - time.monotonic() > 120):
        review = state.get("telegram_review", {}).get("items", {}).get(telegram.source_id(top))
        refresh_options = {"allow_existing": True} if review and originals.originals_only() else {}
        fresh = extract_video_page_details(source_url, max_site_id=state.get("max_seen_video_id", 0),
                                           **refresh_options)
        if fresh:
            programme = blocked_candidate_programme(fresh)
            if not programme and originals.originals_only():
                programme = originals.prohibited(fresh)
            if programme:
                top["programme_hold"] = programme
                print(f"[PROGRAMME-HOLD] {programme}: refreshed source for candidate {top.get('video_id')}")
                return ""
            candidates = [fresh["mp4_url"], fresh.get("alternate_mp4_url"), *candidates]
    elif attempts and telegram.source_id(top).startswith("instagram:") and deadline - time.monotonic() > 210:
        key = telegram.source_id(top)
        reel_url = f"https://www.instagram.com/reel/{key.split(':', 1)[1]}/"
        for fresh in fetch_instagram_reels(state, originals_review=True, refresh_url=reel_url):
            owner = (fresh.get("source_metadata") or {}).get("ownerUsername")
            if telegram.source_id(fresh) != key or str(owner).casefold() != "footballiapp":
                continue
            if originals.originals_only() and originals.prohibited(fresh):
                return ""
            top["source_metadata"] = fresh["source_metadata"]
            candidates.insert(0, fresh["mp4_url"])
            break
    for url in dict.fromkeys(filter(None, candidates)):
        remaining = deadline - time.monotonic() - 75
        if remaining <= 0:
            break
        if url in state["seen_mp4s"]:
            continue
        if media_utils.download_file(url, path, referer=source_url, max_seconds=min(105, remaining)):
            return url
    return ""


def retain_media_retry(item, state, reason, *, priority=False):
    # Delay only this item, limiting repeated requests against broken source URLs.
    failures = safe_int(item.get("download_failures")) + 1
    item["download_failures"] = failures
    delay_minutes = 1 if priority else min(120, 20 * 2 ** min(failures - 1, 3))
    item["next_media_attempt"] = time.time() + delay_minutes * 60
    label = item.get("video_id") or item.get("source", "unknown")
    print(f"[MEDIA-RETRY] {label}: {reason}; trying the next eligible candidate.")
    counts = state["last_run"]["media_retries"]
    counts[reason] = counts.get(reason, 0) + 1
    review = state.get("telegram_review", {}).get("items", {}).get(telegram.source_id(item))
    if review and review.get("request_message_id") and review["status"] == "approved":
        telegram.set_notice(review, "دریافت یا بررسی فایل موقتاً ناموفق بود. درخواست در صف می‌ماند.")
    save_state(state)


def platform_upload_block(state, quota_key):
    if state.get("quota_exhausted_date") == quota_key:
        return "YouTube API quota reached until midnight Pacific"
    if time.time() < float(state.get("upload_limit_cooldown_until", 0)):
        return "YouTube rejected uploads because of the channel limit"
    return ""


def process_queue(youtube, state, policy, quota_key, known_norm_titles, uploaded_urls, seen_mp4s,
                  media_directory, deadline, *, priority_only):
    if telegram.preview_only() or platform_upload_block(state, quota_key):
        return
    video_file = os.path.join(media_directory, "video.mp4")
    thumb_file = os.path.join(media_directory, "thumbnail.jpg")
    now = datetime.now(ZoneInfo("Asia/Tehran"))
    counts = daily_publishing_counts(state, now)
    strict = originals.originals_only()
    queue = [item for item in state["pending_queue"]
             if (priority_only if strict else is_priority_item(item, state, now) == priority_only)]
    if strict:
        review_items = state.get("telegram_review", {}).get("items", {})
        queue.sort(key=lambda item: (
            not review_items.get(telegram.source_id(item), {}).get("request_message_id"),
            not footballi_show(item),
        ))
    if not priority_only and not strict:
        def format_order(item):
            fmt = "short" if item["is_short"] else "main"
            return counts[fmt] / ROUTINE_DAILY_LIMITS[fmt], last_routine_timestamp(state, fmt)
        queue.sort(key=format_order)

    for top in queue:
        if programme_hold_enabled() and (top.get("programme_hold") or blocked_candidate_programme(top)):
            continue
        if deadline - time.monotonic() < MIN_TIME_TO_START_CANDIDATE:
            print("[BUDGET] Leaving remaining candidates for the next half-hour run.")
            break

        priority_context = priority_item_context(top, state, datetime.now(ZoneInfo("Asia/Tehran")))
        priority_item = bool(priority_context)
        review_item = state.get("telegram_review", {}).get("items", {}).get(telegram.source_id(top))
        if ((not priority_item and is_duplicate_title(top["title"], known_norm_titles))
                or top["url"] in uploaded_urls or top["mp4_url"] in seen_mp4s):
            print(f"♻️ [SKIP-DUPLICATE] Already uploaded to channel: {top['title']}")
            if review_item and review_item["status"] == "approved":
                review_item["status"] = "duplicate"
                telegram.set_notice(review_item, "انتشار انجام نشد. منبع، فایل یا عنوان مشابه قبلاً منتشر شده است.")
            state["pending_queue"].remove(top)
            continue
        tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
        cat_check = "SHORTS" if top.get("is_short") else top.get("category", "VIDEO")
        priority_context = priority_item_context(top, state, tehran_now)
        priority_item = bool(priority_context)
        next_attempt = timestamp(top.get("next_media_attempt"))
        if next_attempt and datetime.now(timezone.utc) < next_attempt and not priority_item:
            continue
        blocked = publication_block(top, cat_check, state, policy, tehran_now)
        # A website clip with unverified dimensions may turn out to be a Short.
        may_be_short = top.get("source") == "footballi_web" and "verified_is_short" not in top
        if blocked and may_be_short and not publication_block(top, "SHORTS", state, policy, tehran_now):
            blocked = ""
        if blocked:
            print(f"[TIMING] {cat_check}: {blocked}")
            if blocked == "Analytics preferred time":
                state["last_run"]["learning"]["timing_deferred"] = cat_check
            continue

        if top.get("is_short"):
            if not is_strictly_domestic_short(top["title"], top.get("description", "")):
                print(f"🚫 [SHORT-REJECT] Blocked non-domestic Short before upload: {top['title']}")
                state["pending_queue"].remove(top)
                state["rejected_urls"].append(top["url"])
                continue
        else:
            if not call_ai_copyright_audit(top["title"], top.get("description", "")):
                print(f"🚫 [AI-AUDIT-REJECT] Blocked unsafe video before upload: {top['title']}")
                state["pending_queue"].remove(top)
                state["rejected_urls"].append(top["url"])
                if review_item and review_item["status"] == "approved":
                    review_item["status"] = "rights_rejected"
                    telegram.set_notice(review_item, "فایل از بررسی کپی‌رایت اولیه عبور نکرد.")
                continue

        downloaded_url = download_candidate(top, video_file, state, deadline)
        if (not downloaded_url and not top.get("programme_hold") and priority_item
                and deadline - time.monotonic() > 150):
            top["download_failures"] = max(1, safe_int(top.get("download_failures")))
            print(f"[PRIORITY-RETRY] Refreshing source page for {top.get('video_id', 'candidate')}.")
            downloaded_url = download_candidate(top, video_file, state, deadline)
        if not downloaded_url:
            if top.get("programme_hold"):
                continue
            retain_media_retry(top, state, "download_failed", priority=priority_item)
            continue

        if review_item and review_item["status"] == "approved" and not review_item["preview"]:
            try:
                digest = originals.file_sha256(video_file)
            except OSError:
                retain_media_retry(top, state, "file_unreadable", priority=priority_item)
                continue
            if digest != review_item["sha256"]:
                review_item["status"] = "file_changed"
                top["mp4_url"] = downloaded_url
                telegram.set_notice(review_item, "فایل منبع تغییر کرده است. نسخهٔ تازه دوباره بررسی می‌شود.")
                save_state(state)
                continue

        w, h, dur = get_exact_video_dimensions(video_file)
        if w <= 0 or h <= 0 or not math.isfinite(dur) or dur <= 0:
            retain_media_retry(top, state, "video_probe_failed", priority=priority_item)
            continue
        top["mp4_url"] = downloaded_url
        top.pop("next_media_attempt", None)
        top.pop("download_failures", None)

        is_short = h >= w and dur <= 180
        top["verified_is_short"] = is_short
        top["is_short"] = is_short
        if is_short and not is_strictly_domestic_short(top["title"], top.get("description", "")):
            state["pending_queue"].remove(top)
            state["rejected_urls"].append(top["url"])
            continue
        actual_category = detect_primary_category(top["title"], top.get("description", ""), is_short=is_short)
        if priority_context:
            top.update(priority_context)
        tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
        if publication_block(top, actual_category, state, policy, tehran_now, is_short=is_short):
            print(f"[TIMING] Verified {actual_category}: format is currently ineligible.")
            continue

        reason = ""
        if (is_short and w < MIN_SHORTS_WIDTH_PX) or (not is_short and h < MIN_HORIZONTAL_HEIGHT_PX):
            reason = "resolution_under_720p"
        elif not is_short and dur < MIN_HORIZONTAL_DURATION_SECONDS:
            reason = "horizontal_under_three_minutes"
        elif is_short or priority_only or strict:
            audio_issue, peak = media_utils.inspect_audio_file(video_file)
            if audio_issue == "audio_probe_failed":
                retain_media_retry(top, state, audio_issue, priority=priority_item)
                continue
            if audio_issue:
                reason = audio_issue
            else:
                print(f"[AUDIO] Decoded audio peak: {peak:.1f} dBFS")

        if reason:
            print(f"[MEDIA-REJECT] {reason}: {top['title']} ({w}x{h}, {dur:.0f}s)")
            state["pending_queue"].remove(top)
            state["rejected_urls"].append(top["url"])
            state["seen_mp4s"].append(top["mp4_url"])
            seen_mp4s.add(top["mp4_url"])
            rejected = state["last_run"]["rejected_by_reason"]
            rejected[reason] = rejected.get(reason, 0) + 1
            if review_item and review_item["status"] == "approved":
                review_item["status"] = "media_rejected"
                telegram.set_notice(review_item, f"فایل شرایط انتشار را ندارد. علت: {reason}")
            continue
        if deadline - time.monotonic() < 60:
            print("[BUDGET] Video verified; deferring upload to allow a safe checkpoint.")
            break

        save_state(state)
        published = publish_candidate(
            youtube, state, policy, quota_key, top, actual_category, is_short,
            priority_only and not strict, video_file, thumb_file, tehran_now, deadline
        )
        if not published:
            if top.get("programme_hold") or (strict and top.get("originals_decision") != "approved"):
                continue
            break  # The platform-wide limit applies to every candidate.
        uploaded_urls.add(top["url"])
        seen_mp4s.add(top["mp4_url"])
        known_norm_titles.add(normalize_persian(top["title"]).lower())
        if platform_upload_block(state, quota_key) or not priority_only:
            break  # One routine publication per scheduled run.

    save_state(state)


def publish_candidate(youtube, state, policy, quota_key, candidate, cat, is_short,
                      priority_summary, video_file, thumb_file, tehran_now, deadline):
    if telegram.preview_only():
        return False
    source_id = telegram.source_id(candidate)
    review_item = telegram.review_state(state)["items"].get(source_id)
    if review_item and not review_item["preview"]:
        if review_item["status"] != "approved":
            return False
    approval_card = (review_item if review_item and review_item["status"] == "approved"
                     and not review_item["preview"] else None)
    fmt = "short" if is_short else "main"
    priority_class = candidate.get("priority_class") or ("summary" if priority_summary else "")
    if priority_summary or originals.originals_only():
        style, explore = "direct", False
    else:
        style, explore = title_choice(candidate, policy, fmt, tehran_now)
    match_context = None
    if candidate.get("fixture_id"):
        match_context = {
            "fixture_id": candidate.get("fixture_id"),
            "home_team": candidate.get("home_team"),
            "away_team": candidate.get("away_team"),
            "competition": candidate.get("competition"),
            "kickoff_at": candidate.get("kickoff_at"),
        }
    seo = call_generate_seo(candidate["title"], candidate.get("description", ""), cat, is_short,
                            learning_context=style, match_context=match_context)
    final_title = format_upload_title(seo.get("title", candidate["title"]), candidate, cat)
    final_desc = seo.get("description", candidate.get("description", ""))
    final_tags = seo.get("tags", ["فوتبالی", "فوتبال ایران"])
    source_url = candidate.get("source_url") or candidate.get("url")
    if source_url and source_url not in final_desc:
        final_desc = f"{final_desc}\n\nمنبع رسمی: {source_url}"

    if approval_card:
        marker = f"Footballi ID: {approval_card['button_id']}"
        final_desc = final_desc[:4900] + "\n\n" + marker
    if originals.originals_only() or approval_card:
        decision = originals.verify_upload(candidate, video_file, {
            "title": final_title, "description": final_desc, "tags": final_tags,
        }, telegram.approvals_for(state))
        candidate["originals_decision"] = decision
        if decision != "approved":
            print(f"[ORIGINALS] skipped {originals.source_key(source_url)} | {decision}")
            if approval_card:
                changed = decision == "file_hash_mismatch"
                if changed:
                    approval_card["status"] = "file_changed"
                telegram.set_notice(approval_card, (
                    "فایل منبع تغییر کرده است. نسخهٔ تازه دوباره بررسی می‌شود." if changed
                    else f"انتشار متوقف شد. نتیجهٔ بررسی نهایی: {decision}"
                ))
            save_state(state)
            return False

    programme = blocked_candidate_programme(candidate, video_file, final_title, final_desc, final_tags)
    if programme:
        candidate["programme_hold"] = programme
        if approval_card:
            telegram.set_notice(approval_card, "انتشار متوقف شد. متادیتای نهایی مشمول فیلتر برنامه‌هاست.")
        print(f"[PROGRAMME-HOLD] {programme}: skipped before upload ({candidate.get('video_id', 'candidate')})")
        save_state(state)
        return False

    print(f"[UPLOAD] Publishing {cat} ({'priority' if priority_summary else fmt} | Score: {candidate.get('ai_score', 6)}/10): {final_title}")

    try:
        body = {
            "snippet": {
                "title": final_title,
                "description": final_desc,
                "tags": final_tags,
                "categoryId": "17",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(video_file, chunksize=32 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        if approval_card:
            approval_card["status"] = "upload_uncertain"
            telegram.set_notice(approval_card, "در حال تطبیق نتیجهٔ آپلود با کانال یوتیوب.")
            telegram.checkpoint(state, save_state)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = req.next_chunk(num_retries=3)
            if status:
                print(f"⬆️ [UPLOADING] {int(status.progress() * 100)}% complete...")

        vid_id = response["id"]
        if approval_card:
            telegram.mark_published(approval_card, vid_id)
        print(f"✅ [SUCCESS] Uploaded Video ID: {vid_id}")

        state["pending_queue"].remove(candidate)
        state["uploaded_urls"].append(candidate["url"])
        state["seen_mp4s"].append(candidate["mp4_url"])
        state["uploaded_titles"].append(final_title)
        state["seen_raw_titles"].append(candidate["title"])
        state.setdefault("published_metadata", {})[vid_id] = {
            "title": final_title,
            "category": cat,
            "format": fmt,
            "priority_summary": priority_class == "summary",
            "priority_class": priority_class,
            "priority_reason": candidate.get("priority_reason"),
            "fixture_id": candidate.get("fixture_id"),
            "fixture_relation": candidate.get("fixture_relation"),
            "priority_until": candidate.get("priority_until"),
            "home_team": candidate.get("home_team"),
            "away_team": candidate.get("away_team"),
            "competition": candidate.get("competition"),
            "kickoff_at": candidate.get("kickoff_at"),
            "rights_audit": candidate.get("rights_audit", "approved"),
            "source": candidate.get("source"),
            "source_url": source_url,
            "rights_status": candidate.get("rights_status"),
            "originals_decision": candidate.get("originals_decision") if originals.originals_only() else None,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "title_style": seo.get("title_style"),
            "title_experiment": explore and seo.get("title_style") == style,
            "learning_revision": policy["revision"],
            "topic": topic_for(candidate["title"]),
            "source_title": candidate["title"],
        }
        tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
        day = tehran_now.date().isoformat()
        if state["daily_uploads"].get("date") != day:
            state["daily_uploads"] = {"date": day, "count": 0}
        state["daily_uploads"]["count"] += 1
        if not priority_class:
            state["last_short_upload_timestamp" if is_short else "last_upload_timestamp"] = tehran_now.timestamp()
        upload_key = f"priority_{priority_class}" if priority_class else fmt
        state["last_run"]["uploads"].setdefault(upload_key, 0)
        state["last_run"]["uploads"][upload_key] += 1
        if not is_short:
            state["last_run"].setdefault("end_screen_video_ids", []).append(vid_id)
            print(f"[END-SCREEN] Add Best for viewer and Subscribe in Studio: "
                  f"https://studio.youtube.com/video/{vid_id}/editor")
        state["last_run"]["learning"].update(title_style=seo.get("title_style"),
                                             selected_factor=candidate["learning_factor"],
                                             published_tehran=tehran_now.strftime("%H:%M"))
        state["upload_limit_cooldown_until"] = 0
        state.pop("upload_limit_rejections", None)
        save_state(state)

        thumb_to_use = None
        if not is_short and deadline - time.monotonic() >= 35:
            poster = "" if originals.originals_only() else candidate.get("poster_url", "")
            thumb_to_use = call_prepare_thumbnail(video_file, poster, thumb_file)
        if thumb_to_use and os.path.exists(thumb_to_use):
            try:
                youtube.thumbnails().set(
                    videoId=vid_id,
                    media_body=MediaFileUpload(thumb_to_use, mimetype="image/jpeg"),
                ).execute()
                print("🖼️ [THUMBNAIL] Official HD poster attached to video.")
            except Exception as te:
                print(f"[THUMB-WARN] Could not attach custom thumbnail: {te}")

        matched_playlists = detect_all_matching_playlists(
            final_title, is_short=is_short, source_title=candidate["title"]
        )
        if deadline - time.monotonic() >= 20:
            add_video_to_playlists(youtube, vid_id, matched_playlists, state.get("playlists", {}), state)

        print(f"[DONE] Uploaded: 1 | Remaining in Queue: {len(state['pending_queue'])}")
        return True

    except HttpError as e:
        err_str = str(e)
        print(f" YouTube API Upload error: {e}")
        if "uploadLimitExceeded" in err_str:
            if approval_card:
                approval_card["status"] = "approved"
                telegram.set_notice(approval_card, "به سقف آپلود یوتیوب رسیدیم. بعداً دوباره تلاش می‌شود.")
            rejections = safe_int(state.get("upload_limit_rejections")) + 1
            state["upload_limit_rejections"] = rejections
            retry_minutes = 60 if rejections == 1 else 120
            state["upload_limit_cooldown_until"] = time.time() + retry_minutes * 60
            print(f"[QUOTA-SHIELD] YouTube channel limit; retry in {retry_minutes} minutes.")
            save_state(state)
            return False
        elif "quotaExceeded" in err_str:
            if approval_card:
                approval_card["status"] = "approved"
                telegram.set_notice(approval_card, "سهمیهٔ یوتیوب تمام شده است. بعد از بازنشانی دوباره تلاش می‌شود.")
            state["quota_exhausted_date"] = quota_key
            print("⏸️ [QUOTA-GUARD] Daily API quota reached! Holding queue until midnight Pacific quota reset.")
            save_state(state)
            return False
        else:
            print("[UPLOAD-WARN] Retaining candidate after API failure for retry.")
            save_state(state)
            raise
    except Exception:
        save_state(state)
        raise


def reconcile_telegram_uploads(state, channel_videos):
    items = telegram.review_state(state)["items"]
    for key, record in items.items():
        if record["status"] != "upload_uncertain":
            continue
        marker = f"Footballi ID: {record['button_id']}"
        matches = [row for row in channel_videos if row.get("public")
                   and marker in row.get("description", "").splitlines()]
        if len(matches) != 1:
            telegram.set_notice(record, "نتیجهٔ آپلود هنوز مشخص نیست. برای جلوگیری از تکرار، آپلود دوباره انجام نمی‌شود.")
            continue
        row = matches[0]
        telegram.mark_published(record, row["video_id"])
        for item in state["pending_queue"]:
            if telegram.source_id(item) == key:
                state["uploaded_urls"].append(item["url"])
                state["seen_mp4s"].append(item["mp4_url"])
                state["uploaded_titles"].append(row["title"])
                state["seen_raw_titles"].append(item["title"])
                state["published_metadata"].setdefault(row["video_id"], {
                    "title": row["title"], "source": item.get("source"),
                    "source_url": item.get("source_url") or item["url"],
                    "source_title": item["title"], "originals_decision": "approved",
                    "category": detect_primary_category(item["title"], is_short=item.get("is_short", False)),
                    "format": "short" if item.get("is_short") else "main",
                })
                break
        state["pending_queue"] = [item for item in state["pending_queue"] if telegram.source_id(item) != key]


def prepare_telegram_link_requests(reviewer, state, media_directory, deadline):
    """Bind authenticated publishing requests to the actual bytes before queue admission."""
    review = telegram.review_state(state)
    requests = review.get("link_requests", {})
    prepared = []
    path = os.path.join(media_directory, "telegram-request.mp4")
    for key, request in list(requests.items()):
        if telegram.preview_only() or deadline - time.monotonic() < 210:
            break
        if request.get("retry_at", 0) > time.time():
            continue
        old = review["items"].get(key)
        if old and not old["preview"] and old["status"] not in {"waiting", "file_changed"}:
            review.setdefault("receipts", {})[str(request["message_id"])] = (
                old.get("result") or "این ویدیو قبلاً بررسی شده است."
            )
            requests.pop(key)
            continue
        url = request["url"]
        details = extract_video_page_details(url, allow_existing=True)
        reason = ""
        if not details:
            reason = "صفحه یا فایل منبع فعلاً در دسترس نیست. دوباره تلاش می‌شود."
        elif telegram.source_id(details) != key or not (details.get("title") and details.get("mp4_url")):
            reason = "اطلاعات منبع کامل نیست. دوباره تلاش می‌شود."
        else:
            category = detect_primary_category(details["title"])
            candidate = {**details, "source": "footballi_web", "source_url": url,
                         "category": category, "is_short": False, "learning_factor": 1}
            blocked = originals.prohibited(candidate)
            if blocked:
                reason = "انتشار متوقف شد. این محتوا مشمول فیلتر کپی‌رایت یا برنامه‌های مسدود است."
                requests.pop(key)
            else:
                downloaded = download_candidate(candidate, path, state, deadline)
                if downloaded:
                    candidate["mp4_url"] = downloaded
                    if originals.prohibited(candidate):
                        reason = "انتشار متوقف شد. فایل منبع مشمول فیلتر محتواست."
                        requests.pop(key)
                    else:
                        try:
                            digest = originals.file_sha256(path)
                        except OSError:
                            reason = "بررسی فایل ممکن نبود. دوباره تلاش می‌شود."
                        else:
                            width, height, seconds = get_exact_video_dimensions(path)
                            short = height >= width and seconds <= 180
                            card = telegram.prepare_card(
                                candidate, format_upload_title(candidate["title"], candidate, category),
                                (width, height, seconds, short), digest,
                                "The reviewer supplied the exact official source link.",
                                previous=old,
                            )
                            card["request_message_id"] = request["message_id"]
                            reviewer.record_decision(card, "a")
                            card.pop("notice", None)  # The durable intake receipt already confirms queueing.
                            if old and old.get("message_id"):
                                card["message_id"] = old["message_id"]
                            review["items"][key] = card
                            prepared.append(candidate)
                            requests.pop(key)
                else:
                    reason = "دانلود فایل منبع انجام نشد. دوباره تلاش می‌شود."
        if reason and request.get("last_reason") != reason:
            review.setdefault("receipts", {})[str(request["message_id"])] = reason
            request["last_reason"] = reason
        if key in requests:
            request["retry_at"] = time.time() + 25 * 60
    return prepared


def propose_telegram_reviews(reviewer, state, media_directory, deadline):
    items = telegram.review_state(state)["items"]
    sent = 0
    def review_order(item):
        discovered = timestamp(item.get("discovery_timestamp") or item.get("discovered_at"))
        return not footballi_show(item), discovered.timestamp() if discovered else 0

    waiting = sorted(state.get("originals_waiting", []), key=review_order)
    path = os.path.join(media_directory, "review.mp4")
    for candidate in waiting:
        if sent >= MAX_TELEGRAM_PROPOSALS_PER_RUN or deadline - time.monotonic() < 120:
            break
        key = telegram.source_id(candidate)
        if (not key or originals.prohibited(candidate)
                or candidate.get("originals_decision") != "editorial_approval_missing"
                or (candidate.get("telegram_screening")
                    and (candidate["telegram_screening"] != "ai_unconfirmed"
                         or candidate.get("telegram_screening_version") == 2))
                or float(candidate.get("next_media_attempt") or 0) > time.time()):
            continue
        if key.startswith("instagram:"):
            owner = (candidate.get("source_metadata") or {}).get("ownerUsername")
            if not isinstance(owner, str) or owner.casefold() != "footballiapp":
                continue
        record = items.get(key)
        if record and record["preview"] == telegram.preview_only():
            if record["status"] == "waiting" and not record.get("message_id"):
                sent += reviewer.send_card(record)
            if record["status"] != "file_changed":
                continue
        downloaded = download_candidate(candidate, path, state, deadline)
        if not downloaded:
            retain_media_retry(candidate, state, "review_download_failed", priority=False)
            continue
        width, height, seconds = get_exact_video_dimensions(path)
        if width <= 0 or height <= 0 or not math.isfinite(seconds) or seconds <= 0:
            retain_media_retry(candidate, state, "review_probe_failed", priority=False)
            continue
        short = height >= width and seconds <= 180
        if (short and (width < MIN_SHORTS_WIDTH_PX or not is_strictly_domestic_short(
                candidate["title"], candidate.get("description", "")))) or (
                not short and (height < MIN_HORIZONTAL_HEIGHT_PX or seconds < MIN_HORIZONTAL_DURATION_SECONDS)):
            candidate["telegram_screening"] = "quality_or_format_rejected"
            continue
        audio_issue, _ = media_utils.inspect_audio_file(path)
        if audio_issue:
            if audio_issue == "audio_probe_failed":
                retain_media_retry(candidate, state, audio_issue, priority=False)
            else:
                candidate["telegram_screening"] = audio_issue
            continue
        title = format_upload_title(candidate["title"], candidate, "")
        if originals.prohibited(candidate, downloaded, title):
            continue
        show = {"POSHTE_KHAT": POSHTE_KHAT_TITLE, "VIDEOCAST": "ویدیوکست"}.get(footballi_show(candidate))
        evidence = (f"نام برنامه در عنوان منبع: {show}." if show
                    else "هوش مصنوعی در متادیتای منبع نشانهٔ تولید فوتبالی تشخیص داد.")
        verdict = originals.audit_with_ai(candidate, {"title": title},
                                         "Review the official source title and description; rights are unconfirmed.",
                                         screening=True)
        if verdict != "approved":
            if verdict == "ai_unavailable":
                retain_media_retry(candidate, state, "review_ai_unavailable", priority=False)
            else:
                candidate["telegram_screening"] = verdict
                candidate["telegram_screening_version"] = 2
            continue
        candidate.pop("telegram_screening", None)
        candidate.pop("telegram_screening_version", None)
        digest = originals.file_sha256(path)
        record = telegram.prepare_card(candidate, title, (width, height, seconds, short),
                                       digest, evidence, previous=record)
        items[key] = record
        candidate["mp4_url"] = downloaded
        telegram.checkpoint(state, save_state)
        sent += reviewer.send_card(record)
    print(f"[TELEGRAM] {sent} review cards sent.")
    reasons = {}
    for item in waiting:
        label = item.get("telegram_screening") or item.get("originals_decision", "unreviewed")
        reasons[label] = reasons.get(label, 0) + 1
    print(f"[TELEGRAM] Waiting candidates by reason: {json.dumps(reasons, sort_keys=True)}")
    if not sent and deadline - time.monotonic() > 65:
        reviewer.send_preview_probe(state, lambda: telegram.checkpoint(state, save_state))


def main():
    print("[START] Footballi Bot — independent format schedules and priority match highlights.")
    deadline = time.monotonic() + PUBLISHING_BUDGET_SECONDS
    state = load_state()
    strict = originals.originals_only()
    reviewer = telegram.TelegramReview.from_env() if strict else None
    if reviewer:
        reviewer.poll(state, lambda: telegram.checkpoint(state, save_state))
        reviewer.flush_receipts(state, min(deadline, time.monotonic() + 32))
    if strict:
        originals.repartition_queue(state, approvals=telegram.approvals_for(state),
                                    rejected=telegram.rejected_sources(state))
        originals.report_queue(state)
        save_state(state)
    else:
        state["pending_queue"].extend(state.pop("originals_waiting", []))
    global _QUEUE_REJECTION_COUNTS
    policy = state.get("learning") or build_policy(state, datetime.now(timezone.utc))
    state["learning"] = policy
    _QUEUE_REJECTION_COUNTS = {}
    reset_key = get_tehran_reset_key()
    quota_key = get_quota_reset_key()

    if not isinstance(state.get("daily_uploads"), dict) or state["daily_uploads"].get("date") != reset_key:
        state["daily_uploads"] = {"date": reset_key, "count": 0}

    uploaded_urls = set(state.get("uploaded_urls", []))
    rejected_urls = set(state.get("rejected_urls", []))
    seen_mp4s = set(state.get("seen_mp4s", []))
    known_norm_titles = {
        normalize_persian(t).lower()
        for t in (state.get("uploaded_titles", []) + state.get("seen_raw_titles", []))
    }

    youtube = None
    youtube_auth_failed = False
    channel_videos = []
    if state.get("quota_exhausted_date") != quota_key:
        try:
            youtube = get_youtube_client()
        except RefreshError:
            youtube_auth_failed = True
            print("[YOUTUBE-OAUTH] Token refresh failed. Renew YOUTUBE_TOKEN; continuing discovery and Telegram.")
    if youtube:
        channel_videos = fetch_channel_existing_videos(youtube, state, max_items=180)
        reconcile_telegram_uploads(state, channel_videos)
        for v in channel_videos:
            known_norm_titles.add(normalize_persian(v["title"]).lower())

    tehran_now = datetime.now(ZoneInfo("Asia/Tehran"))
    live_fixtures = fetch_live_fixtures(now=tehran_now)
    live_status = scrapers.LAST_LIVE_SCORES_STATUS
    if live_status == "ok":
        state["live_fixtures"] = {
            "fixtures": live_fixtures,
            "fetched_at": tehran_now.isoformat(),
            "source_url": "https://footballi.net/live-scores",
            "parser_version": 1,
        }
    else:
        previous = state.get("live_fixtures", {})
        previous_at = timestamp(previous.get("fetched_at"))
        if previous_at and tehran_now - previous_at <= timedelta(hours=12):
            live_fixtures = previous.get("fixtures", [])
            live_status = "stale_fallback"
        else:
            live_fixtures = []
    new_web_videos = crawl_footballi_videos(state, max_new_candidates=20, originals_review=strict)
    state["last_run"] = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "new_web_candidates": len(new_web_videos),
        "new_instagram_candidates": 0,
        "rejected_by_reason": {},
        "media_retries": {},
        "uploads": {"main": 0, "short": 0, "priority_summaries": 0,
                    "priority_editorial": 0, "priority_match_day": 0},
        "live_scores": {
            "status": live_status,
            "fixtures": len(live_fixtures),
            "match_day_candidates": 0,
            "priority_approved": 0,
        },
        "learning": {"revision": policy["revision"], "changes": policy["changes"]},
    }

    def merge_queue(items):
        if strict:
            originals.repartition_queue(state, items, approvals=telegram.approvals_for(state),
                                        rejected=telegram.rejected_sources(state))
            state["pending_queue"] = [normalize_queue_item(item) for item in state["pending_queue"]]
            for item in state["pending_queue"]:
                item["learning_factor"] = 1
            originals.report_queue(state)
            save_state(state)
            return
        state["pending_queue"] = deduplicate_and_balance_queue(
            state["pending_queue"] + items, uploaded_urls, set(state["rejected_urls"]),
            seen_mp4s, known_norm_titles, int(state["max_seen_video_id"]), policy,
            priority_state=state,
        )
        now_for_priority = datetime.now(ZoneInfo("Asia/Tehran"))
        for item in state["pending_queue"]:
            context = priority_item_context(item, state, now_for_priority)
            if context:
                item.update(context)
                if context.get("priority_class") == "match_day":
                    state["last_run"]["live_scores"]["match_day_candidates"] += 1
        state["last_run"]["live_scores"]["priority_approved"] = sum(
            1 for item in state["pending_queue"]
            if not item.get("programme_hold") and is_priority_item(item, state, now_for_priority)
        )
        for reason, count in _QUEUE_REJECTION_COUNTS.items():
            rejections = state["last_run"]["rejected_by_reason"]
            rejections[reason] = rejections.get(reason, 0) + count
        state["last_run"]["learning"]["queue_adjusted"] = sum(
            item["learning_factor"] != 1 for item in state["pending_queue"]
        )
        save_state(state)

    merge_queue(new_web_videos)
    with tempfile.TemporaryDirectory(prefix="footballi-media-") as media_directory:
        if reviewer and youtube and not platform_upload_block(state, quota_key):
            requested = prepare_telegram_link_requests(reviewer, state, media_directory, deadline)
            if requested:
                merge_queue(requested)
        if youtube:
            process_queue(youtube, state, policy, quota_key, known_norm_titles, uploaded_urls,
                          seen_mp4s, media_directory, deadline, priority_only=True)

        if deadline - time.monotonic() >= 150:
            new_ig_reels = fetch_instagram_reels(state, originals_review=strict)
            state["last_run"]["new_instagram_candidates"] = len(new_ig_reels)
            if new_ig_reels:
                merge_queue(new_ig_reels)
                if strict and youtube:
                    process_queue(youtube, state, policy, quota_key, known_norm_titles, uploaded_urls,
                                  seen_mp4s, media_directory, deadline, priority_only=True)
        else:
            print("[BUDGET] Instagram check deferred after priority work.")

        if reviewer:
            reviewer.flush_notices(state, min(deadline, time.monotonic() + 35))
            reviewer.flush_receipts(state, min(deadline, time.monotonic() + 22))
            originals.repartition_queue(state, approvals=telegram.approvals_for(state),
                                        rejected=telegram.rejected_sources(state))
            propose_telegram_reviews(reviewer, state, media_directory, deadline)
            reviewer.refresh_keyboard(state)

        platform_block = platform_upload_block(state, quota_key)
        if not youtube or platform_block:
            print(f"[QUOTA-GUARD] {platform_block or 'YouTube unavailable'}; queue preserved.")
        else:
            process_queue(youtube, state, policy, quota_key, known_norm_titles, uploaded_urls,
                          seen_mp4s, media_directory, deadline, priority_only=False)
            if deadline - time.monotonic() >= 60 and not platform_upload_block(state, quota_key):
                curate_channel_playlists(youtube, state, channel_videos)

    finished = datetime.now(ZoneInfo("Asia/Tehran"))
    daily = daily_publishing_counts(state, finished)
    state["last_run"].update(finished_at=finished.isoformat(),
                             queue_size=len(state["pending_queue"]), daily_uploads=daily)
    print(f"[STATUS] Routine main {daily['main']}/{ROUTINE_DAILY_LIMITS['main']}, "
          f"routine Shorts {daily['short']}/{ROUTINE_DAILY_LIMITS['short']}, "
          f"priority summaries {daily['priority_summaries']} | "
          f"editorial {daily['priority_editorial']} | match-day {daily['priority_match_day']}, "
          f"queue {len(state['pending_queue'])}.")
    save_state(state)
    if youtube_auth_failed:
        raise SystemExit("YouTube authentication failed. Discovery and Telegram state were saved.")


if __name__ == "__main__":
    if "--audit-originals-queue" in sys.argv[1:]:
        audit_state = load_state()
        originals.repartition_queue(audit_state, approvals=telegram.approvals_for(audit_state),
                                    rejected=telegram.rejected_sources(audit_state))
        originals.report_queue(audit_state)
    elif "--playlists-only" in sys.argv[1:]:
        run_playlists_only()
    else:
        main()
