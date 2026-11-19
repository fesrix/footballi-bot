"""Convert comparable YouTube results into conservative publishing preferences."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from config import MAIN_UPLOAD_WINDOWS, SHORT_UPLOAD_WINDOWS

TEHRAN = ZoneInfo("Asia/Tehran")
PACIFIC = ZoneInfo("America/Los_Angeles")
MIN_SAMPLES = 5
TITLE_STYLES = {
    "direct": "Keep the main subject first and describe the event directly.",
    "context": "Lead with the main subject and add one supported match or program detail.",
    "quote": "Lead with the main subject and a concise attributed statement when explicitly supplied. Never invent a quote.",
}
METRIC_WEIGHTS = {"views": .30, "estimatedMinutesWatched": .30,
                  "subscribersGained": .25, "averageViewPercentage": .15}
TOPICS = ("استقلال", "پرسپولیس", "سپاهان", "تراکتور", "داوری", "تیم ملی", "مصاحبه")


def number(value, default=0.0):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def timestamp(value):
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def topic_for(title):
    clean = title.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return next((topic for topic in TOPICS if topic in clean), "")


def is_fresh(period_end, now):
    try:
        return 0 <= (now.astimezone(PACIFIC).date() - date.fromisoformat(period_end)).days <= 7
    except (TypeError, ValueError):
        return False


def build_policy(state, now):
    previous = state.get("learning", {})
    analytics = state.get("analytics", {})
    period_end = analytics.get("period_end")
    today = now.astimezone(TEHRAN).date().isoformat()
    groups = defaultdict(list)
    observations = []
    if is_fresh(period_end, now):
        for video_id, record in sorted(analytics.get("videos", {}).items()):
            metrics = (record.get("first_three_days") or {}).get("metrics")
            published = timestamp(record.get("published_at"))
            fmt = record.get("format")
            if not metrics or fmt not in {"main", "short"} or not published:
                continue
            if not timedelta(0) <= now - published <= timedelta(days=90):
                continue
            values = {key: number(metrics.get(key), None) for key in METRIC_WEIGHTS}
            if any(value is None for value in values.values()):
                continue
            local = published.astimezone(TEHRAN)
            metadata = state.get("published_metadata", {}).get(video_id, {})
            style = metadata.get("title_style", "")
            topic = metadata.get("topic") or topic_for(record.get("title") or "")
            category = record.get("category") or "VIDEO"
            slot = local.hour // 2 * 120
            group = (video_id, category, topic, style, slot, values)
            groups[fmt].append(group)
            observations.append((fmt, group))

    fingerprint = hashlib.sha256(json.dumps(observations, ensure_ascii=False,
                                            sort_keys=True).encode()).hexdigest()[:16]
    result = {"version": 1, "revision": fingerprint, "data_through": period_end,
              "as_of": today, "formats": {}, "changes": []}
    for fmt in ("main", "short"):
        rows = groups[fmt]
        profile = {"samples": len(rows), "categories": {}, "topics": {}, "titles": {}, "slots": {}}
        result["formats"][fmt] = profile
        if len(rows) < MIN_SAMPLES:
            profile["reason"] = "insufficient_or_stale_data"
            continue
        cohorts = defaultdict(list)
        for row in rows:
            cohorts[row[1]].append(row[-1])
        mature_cohorts = [cohort for cohort in cohorts.values() if len(cohort) >= MIN_SAMPLES]
        baseline = {
            key: max(1, median(median(value[key] for value in cohort) for cohort in mature_cohorts))
            if mature_cohorts else max(1, median(row[-1][key] for row in rows))
            for key in METRIC_WEIGHTS
        }
        buckets = defaultdict(list)
        for _, category, topic, style, slot, values in rows:
            score = sum(weight * min(2, values[key] / baseline[key])
                        for key, weight in METRIC_WEIGHTS.items())
            for kind, label in (("categories", category), ("topics", topic),
                                ("titles", style if style in TITLE_STYLES else ""),
                                ("slots", str(slot) if category != "SUMMARY" else "")):
                if label:
                    buckets[kind, label].append(score)
        for (kind, label), scores in buckets.items():
            profile[kind][label] = {"n": len(scores), "score": round(median(scores), 3)}
        profile["reason"] = "first_three_days_by_format"
        old = previous.get("formats", {}).get(fmt, {})
        _select_preferred_minute(fmt, profile, old, previous.get("as_of"), today)
        if any(profile.get(key) != old.get(key) for key in ("categories", "topics", "titles", "slots",
                                                             "preferred_minutes")):
            result["changes"].append(fmt)
    return result


def _select_preferred_minute(fmt, profile, old, previous_date, today):
    windows = SHORT_UPLOAD_WINDOWS if fmt == "short" else MAIN_UPLOAD_WINDOWS
    preferred = {}
    for start, end in windows:
        slots = [(max(int(slot), start), item["score"]) for slot, item in profile["slots"].items()
                 if item["n"] >= MIN_SAMPLES and max(int(slot), start) < min(int(slot) + 120, end)]
        if len(slots) < 2:
            continue
        target, score = max(slots, key=lambda pair: (pair[1], -pair[0]))
        if score <= min(value for _, value in slots) + .05:
            target = start
        target = min(target, start + 60)
        current = min(start + 60, old.get("preferred_minutes", {}).get(str(start), start))
        preferred[str(start)] = current if previous_date == today else (
            current + max(-30, min(30, target - current)))
    profile["preferred_minutes"] = preferred


def ranking(item, policy, now):
    is_short = item.get("is_short") or item.get("category") == "SHORTS"
    fmt = "short" if is_short else "main"
    profile = policy.get("formats", {}).get(fmt, {}) if is_fresh(policy.get("data_through"), now) else {}
    groups = (profile.get("categories", {}).get(item.get("category")),
              profile.get("topics", {}).get(topic_for(item.get("title") or "")))
    boosts = [max(.8, min(1.2, .8 + .2 * g["score"])) for g in groups if g and g["n"] >= MIN_SAMPLES]
    factor = sum(boosts) / len(boosts) if boosts else 1.0
    published = timestamp(item.get("source_published_at") or item.get("published_at"))
    age_hours = max(0, (now - published).total_seconds() / 3600) if published else 24
    base = (.65 * number(item.get("ai_score"), 6) / 10
            + .20 * min(1, math.log1p(number(item.get("site_views"))) / math.log1p(100_000))
            + .15 * max(0, 1 - age_hours / 48))
    return base * factor, factor


def title_choice(candidate, policy, fmt, now):
    profile = policy.get("formats", {}).get(fmt, {}) if is_fresh(policy.get("data_through"), now) else {}
    measured = [(style, row["score"]) for style, row in profile.get("titles", {}).items()
                if style in TITLE_STYLES and row["n"] >= MIN_SAMPLES]
    best = max(measured, key=lambda pair: pair[1])[0] if measured else "direct"
    key = candidate.get("source_url") or candidate.get("url") or ""
    draw = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    explore = profile.get("samples", 0) >= MIN_SAMPLES and draw % 10 == 0
    if explore:
        alternatives = [style for style in TITLE_STYLES if style != best]
        best = alternatives[draw // 10 % len(alternatives)]
    return best, explore


def defer_for_timing(category, is_short, policy, now):
    if category == "SUMMARY" or not is_fresh(policy.get("data_through"), now):
        return False
    fmt = "short" if is_short else "main"
    preferred = policy.get("formats", {}).get(fmt, {}).get("preferred_minutes", {})
    minute = now.hour * 60 + now.minute
    windows = SHORT_UPLOAD_WINDOWS if is_short else MAIN_UPLOAD_WINDOWS
    return any(start <= minute < min(preferred.get(str(start), start), start + 60, end)
               for start, end in windows)
