# -*- coding: utf-8 -*-
"""Render a compact growth report from the file-based channel state."""

import json
from collections import defaultdict
from pathlib import Path


STATE_FILE = Path("uploaded_matches.json")
REPORT_FILE = Path("growth_report.md")


def build_report(state: dict) -> str:
    analytics = state.get("analytics", {})
    videos = analytics.get("videos", {})
    by_category = defaultdict(lambda: {"videos": 0, "views": 0, "watch_minutes": 0, "subs": 0})
    for record in videos.values():
        category = record.get("category") or "UNKNOWN"
        metrics = record.get("metrics", {})
        row = by_category[category]
        row["videos"] += 1
        row["views"] += int(metrics.get("views", 0) or 0)
        row["watch_minutes"] += float(metrics.get("estimatedMinutesWatched", 0) or 0)
        row["subs"] += int(metrics.get("subscribersGained", 0) or 0)

    lines = [
        "# Footballi Growth Report",
        "",
        f"- Analytics window: {analytics.get('period_start', 'unknown')} → {analytics.get('period_end', 'unknown')}",
        f"- Tracked videos: {analytics.get('video_count', 0)}",
        f"- Queue size: {len(state.get('pending_queue', []))}",
        f"- Uploads today: {state.get('daily_uploads', {}).get('count', 0)}",
        "",
        "## Performance by category",
        "",
        "| Category | Videos | Views | Watch minutes | Subscribers gained |",
        "|---|---:|---:|---:|---:|",
    ]
    for category, row in sorted(by_category.items(), key=lambda x: -x[1]["views"]):
        lines.append(
            f"| {category} | {row['videos']} | {row['views']} | "
            f"{row['watch_minutes']:.1f} | {row['subs']} |"
        )
    lines.extend([
        "",
        "## Last run",
        "",
        f"- New web candidates: {state.get('last_run', {}).get('new_web_candidates', 0)}",
        f"- New Instagram candidates: {state.get('last_run', {}).get('new_instagram_candidates', 0)}",
        f"- Rejections: {state.get('last_run', {}).get('rejected_by_reason', {})}",
    ])
    run = state.get("last_run", {})
    for label, key in (("Published this run", "uploads"), ("Published today by format", "daily_uploads")):
        counts = run.get(key)
        if counts:
            lines.append(f"- {label}: {counts['main']} main, {counts['short']} Shorts, "
                         f"{counts['priority_summaries']} priority highlights")
    if run.get("media_retries"):
        lines.append(f"- Retained media failures: {run['media_retries']}")
    comments = state.get("comment_manager", {}).get("last_run", {})
    if comments:
        lines.extend(["", "## Comment review",
                      f"- Discovered: {comments['discovered']} | Reviewed by AI: {comments['reviewed']}",
                      f"- Kept without reply: {comments['reviewed_no_reply']} | Replied: {comments['replied']} | Removed: {comments['deleted']}",
                      f"- Errors: {comments['errors']} | In queue: {comments['remaining']} | Threads awaiting a reply scan: {comments['reply_threads_remaining']}",
                      f"- Archive pages processed: {comments['archive_pages']} | Read quota: {comments['read_units']} | Write quota: {comments['write_units']}"])
    policy = state.get("learning", {})
    decisions = state.get("last_run", {}).get("learning", {})
    if policy:
        lines.extend(["", "## Publishing decisions from Analytics",
                      f"- Data through: {policy.get('data_through') or 'unavailable'} | Revision: {policy.get('revision')}",
                      f"- Updated profiles: {', '.join(policy.get('changes', [])) or 'none'}",
                      f"- Candidates with an Analytics adjustment: {decisions.get('queue_adjusted', 0)}"])
        for fmt, profile in policy.get("formats", {}).items():
            preferred = ", ".join(f"{m // 60:02d}:{m % 60:02d}"
                                  for m in profile.get("preferred_minutes", {}).values())
            lines.append(f"- {fmt}: {profile['samples']} mature samples, preferred Tehran times: {preferred or 'default'}")
        if decisions.get("published_tehran"):
            lines.append(f"- Published at {decisions['published_tehran']} Tehran | Title style: "
                         f"{decisions.get('title_style') or 'default'} | Queue factor: {decisions.get('selected_factor')}")
        if decisions.get("timing_deferred"):
            lines.append(f"- Timing preference deferred a {decisions['timing_deferred']} candidate.")
    return "\n".join(lines) + "\n"


def main() -> int:
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    REPORT_FILE.write_text(build_report(state), encoding="utf-8")
    print(f"[REPORT] Wrote {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
