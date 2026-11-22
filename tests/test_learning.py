import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import analytics
import footballi_uploader as uploader
from learning import TEHRAN, build_policy, defer_for_timing, title_choice

NOW = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)


def metrics(value):
    return {"views": value * 10, "estimatedMinutesWatched": value * 2,
            "subscribersGained": max(1, value // 10), "averageViewPercentage": 45}


def sample_state():
    records, metadata = {}, {}
    for i in range(12):
        category = "VIDEOCAST" if i < 6 else "VIDEO"
        records[str(i)] = {
            "published_at": (NOW - timedelta(days=i + 7)).replace(hour=15 if i < 6 else 13).isoformat(),
            "format": "main", "category": category, "title": category,
            "first_three_days": {"metrics": metrics(100 if i < 6 else 10)},
        }
        metadata[str(i)] = {"title_style": "context" if i < 6 else "direct"}
    return {"analytics": {"period_end": "2026-11-16", "videos": records}, "published_metadata": metadata}


def candidate(title, category, score, video_id):
    url = f"https://footballi.net/video/{video_id}"
    return {"url": url, "mp4_url": url + ".mp4", "title": title, "category": category,
            "ai_score": score, "published_at": (NOW - timedelta(hours=3)).timestamp(),
            "rights_status": "internal_authorized"}


@patch.dict("os.environ", {"FOOTBALLI_ORIGINALS_ONLY": "false"})
class LearningTests(unittest.TestCase):
    def ranked(self, items, policy):
        with patch.object(uploader, "is_video_too_old", return_value=False), \
             patch.object(uploader, "is_foreign_or_blacklisted", return_value=False), \
             patch.object(uploader, "detect_primary_category",
                          side_effect=lambda title, *args, **kwargs: "VIDEOCAST" if title == "Podcast" else "VIDEO"):
            return uploader.deduplicate_and_balance_queue(
                [dict(i) for i in items], set(), set(), set(), set(), 1075000, policy, NOW)

    def test_analytics_changes_real_queue_order(self):
        items = [candidate("Match interview", "VIDEO", 8, 1075000),
                 candidate("Podcast", "VIDEOCAST", 7, 1075001)]
        self.assertEqual(self.ranked(items, {})[0]["title"], "Match interview")
        state = sample_state()
        self.assertEqual(self.ranked(items, build_policy(state, NOW))[0]["title"], "Podcast")
        self.assertEqual(self.ranked(items, build_policy(state, NOW + timedelta(days=8)))[0]["title"],
                         "Match interview")

    def test_outlier_invalid_and_unknown_format_do_not_move_scores(self):
        state = sample_state()
        original = build_policy(state, NOW)["formats"]["main"]["categories"]
        videos = state["analytics"]["videos"]
        videos["0"]["first_three_days"]["metrics"] = metrics(10_000_000)
        videos["6"]["first_three_days"]["metrics"]["views"] = float("nan")
        videos["12"] = {**videos["1"], "format": "unknown"}
        policy = build_policy(state, NOW)
        self.assertAlmostEqual(original["VIDEOCAST"]["score"],
                               policy["formats"]["main"]["categories"]["VIDEOCAST"]["score"], delta=.1)
        self.assertEqual(policy["formats"]["main"]["samples"], 11)
        self.assertEqual(policy["formats"]["short"]["samples"], 0)

    def test_timing_moves_at_most_half_hour_daily_and_summary_does_not_wait(self):
        state = sample_state()
        policy = build_policy(state, NOW)
        start = str(16 * 60)
        minute = policy["formats"]["main"]["preferred_minutes"][start]
        self.assertEqual(minute, 16 * 60 + 30)
        state["learning"] = policy
        self.assertEqual(build_policy(state, NOW)["formats"]["main"]["preferred_minutes"][start], minute)
        self.assertEqual(build_policy(state, NOW + timedelta(days=1))
                         ["formats"]["main"]["preferred_minutes"][start], minute + 30)
        now = datetime(2026, 11, 19, 16, 10, tzinfo=TEHRAN)
        self.assertTrue(defer_for_timing("VIDEO", False, policy, now))
        self.assertFalse(defer_for_timing("SUMMARY", False, policy, now))

    def test_formats_have_independent_pacing(self):
        now = datetime(2026, 11, 19, 18, 30, tzinfo=TEHRAN)
        state = {"daily_uploads": {"count": 1}, "last_upload_timestamp": now.timestamp()}
        self.assertTrue(uploader.publication_block({}, "VIDEO", state, {}, now))
        self.assertFalse(uploader.publication_block({}, "SHORTS", state, {}, now))
        state["published_metadata"] = {
            str(i): {"category": "SHORTS", "published_at": now.isoformat()} for i in range(2)
        }
        self.assertEqual(uploader.publication_block({}, "SHORTS", state, {}, now),
                         "daily short upload cap")

    def test_style_learning_has_stable_exploration(self):
        policy = build_policy(sample_state(), NOW)
        candidates = [{"url": f"https://footballi.net/video/{i}"} for i in range(500)]
        choices = [title_choice(item, policy, "main", NOW) for item in candidates]
        self.assertGreater(sum(experiment for _, experiment in choices), 30)
        self.assertLess(sum(experiment for _, experiment in choices), 75)
        self.assertTrue(all(style == "context" for style, experiment in choices if not experiment))
        self.assertEqual(choices, [title_choice(item, policy, "main", NOW) for item in candidates])

    def test_owned_file_format_fallback_respects_duration_rotation_and_livestream(self):
        def details(video_id, duration, width, height, rotation="none", **extra):
            return {"id": video_id, "fileDetails": {
                "durationMs": duration,
                "videoStreams": [{"widthPixels": width, "heightPixels": height, "rotation": rotation}],
            }, **extra}

        records = {key: {"published_at": NOW.isoformat(), "format": "unknown"}
                   for key in ("short", "long", "vertical_long", "stream", "invalid")}
        state = {"analytics": {"videos": records}}
        youtube = Mock()
        youtube.videos.return_value.list.return_value.execute.return_value = {"items": [
            details("short", "180000", 1920, 1080, "clockwise"),
            details("long", "90000", 1920, 1080),
            details("vertical_long", "180001", 1080, 1920),
            details("stream", "90000", 1920, 1080, liveStreamingDetails={"actualStartTime": NOW.isoformat()}),
            details("invalid", "90000", 1920, 1080, "other"),
        ]}
        analytics._backfill_formats_from_owned_files(state, youtube, "2026-11-16")
        self.assertEqual([records[key]["format"] for key in records],
                         ["short", "main", "main", "unknown", "unknown"])
        self.assertEqual(records["short"]["format_source"], "owner_file_details")
        analytics._backfill_formats_from_owned_files(state, youtube, "2026-11-16")
        video_list = youtube.videos.return_value.list
        video_list.assert_called_once()
        fields = video_list.call_args.kwargs["fields"]
        self.assertEqual(fields.count("("), fields.count(")"))

    def test_backfill_uses_first_three_days_and_authoritative_short_type(self):
        from datetime import date

        state = sample_state()
        record = {"published_at": "2026-11-10T14:00:00+00:00", "title": "No Shorts hashtag"}
        state["analytics"]["videos"]["new"] = record
        service = Mock()
        historical = {"format": "unknown", "first_three_days": {"metrics": metrics(7)}}
        state["analytics"]["videos"]["historical"] = historical
        service.reports.return_value.query.side_effect = [
            SimpleNamespace(execute=lambda: {"rows": [["new", "SHORTS", 1],
                                                       ["historical", "VIDEO_ON_DEMAND", 7]]}),
            SimpleNamespace(execute=lambda: {"columnHeaders": [{"name": k} for k in metrics(20)],
                                              "rows": [list(metrics(20).values())]}),
        ]
        with patch.object(analytics, "save_state"):
            analytics._backfill_content_formats(state, service, "2026-08-19", "2026-11-16")
            analytics._collect_first_three_days(state, service, date(2026, 11, 16))
        self.assertEqual(historical["format"], "main")
        self.assertEqual(record["format"], "short")
        self.assertEqual(record["first_three_days"]["end"], "2026-11-12")
        self.assertEqual(record["first_three_days"]["metrics"]["views"], 200)
