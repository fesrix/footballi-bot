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
    return "\n".join(lines) + "\n"


def main() -> int:
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    REPORT_FILE.write_text(build_report(state), encoding="utf-8")
    print(f"[REPORT] Wrote {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

