import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import footballi_uploader as uploader
from learning import TEHRAN


def summary(title, video_id, now):
    source_url = f"https://footballi.net/video/{video_id}"
    return {
        "video_id": video_id, "url": source_url, "source_url": source_url,
        "source": "footballi_web", "title": f"خلاصه بازی {title}",
        "rights_status": "internal_authorized", "mp4_url": source_url + "/video.mp4",
        "source_published_at": (now - timedelta(hours=1)).isoformat(),
        "is_short": False, "category": "SUMMARY", "learning_factor": 1,
    }


@patch.dict("os.environ", {"FOOTBALLI_ORIGINALS_ONLY": "false"})
class PublishingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(TEHRAN)

    def test_fresh_authorized_summary_bypasses_routine_caps_windows_and_spacing(self):
        now = self.now.replace(hour=3, minute=0)
        item = summary("سپاهان و استقلال", 1075800, now)
        state = {"last_upload_timestamp": now.timestamp(), "last_short_upload_timestamp": now.timestamp(),
                 "published_metadata": {str(i): {"category": "VIDEO", "published_at": now.isoformat()}
                                        for i in range(4)}}
        self.assertFalse(uploader.publication_block(item, "SUMMARY", state, {}, now))
        self.assertEqual(uploader.publication_block({}, "VIDEO", state, {}, now), "daily main upload cap")
        self.assertFalse(uploader.is_priority_summary(
            {**item, "title": "تحلیل خلاصه بازی سپاهان و استقلال"}, now))
        self.assertTrue(uploader.publication_block({**item, "rights_status": ""}, "SUMMARY", state, {}, now))
        self.assertTrue(uploader.publication_block({**item, "source_url": "https://example.org"},
                                                    "SUMMARY", state, {}, now))
        self.assertTrue(uploader.publication_block({**item, "source_published_at": (now - timedelta(days=2)).isoformat()},
                                                    "SUMMARY", state, {}, now))

    def test_fourth_main_upload_can_follow_after_two_and_a_half_hours(self):
        now = self.now.replace(hour=21, minute=0, second=0, microsecond=0)
        previous_uploads = [now.replace(hour=h, minute=m) for h, m in ((10, 0), (12, 30), (18, 30))]
        state = {"published_metadata": {str(i): {"category": "VIDEO", "published_at": stamp.isoformat()}
                                        for i, stamp in enumerate(previous_uploads)},
                 "last_upload_timestamp": previous_uploads[-1].timestamp()}
        self.assertEqual(uploader.publication_block({}, "VIDEO", state, {}, now - timedelta(minutes=1)),
                         "minimum spacing for this format")
        self.assertFalse(uploader.publication_block({}, "VIDEO", state, {}, now))
        state["published_metadata"]["fourth"] = {"category": "VIDEO", "published_at": now.isoformat()}
        self.assertEqual(uploader.publication_block({}, "VIDEO", state, {}, now), "daily main upload cap")

    def test_only_authoritative_freshness_can_override_routine_schedule(self):
        item = summary("سپاهان و استقلال", 1075800, self.now)
        item["source_published_at"] = None
        item["discovery_timestamp"] = self.now.timestamp()
        self.assertTrue(uploader.is_priority_summary(item, self.now))
        item["source_first_seen_at"] = self.now.timestamp()
        self.assertTrue(uploader.is_priority_summary(item, self.now))
        item["source_published_at"] = (self.now - timedelta(hours=25)).isoformat()
        self.assertFalse(uploader.is_priority_summary(item, self.now))

    def test_daily_counts_use_tehran_midnight_and_exclude_all_priority_formats(self):
        now = self.now.replace(hour=0, minute=30)
        state = {"published_metadata": {
            "a": {"category": "VIDEO", "published_at": (now - timedelta(hours=1)).isoformat()},
            "b": {"category": "SUMMARY", "format": "short", "priority_summary": True,
                  "published_at": now.isoformat()},
            "c": {"category": "SHORTS", "published_at": now.isoformat()},
        }}
        self.assertEqual(uploader.daily_publishing_counts(state, now),
                         {"main": 0, "short": 1, "priority_summaries": 1,
                          "priority_editorial": 0, "priority_match_day": 0})

    def test_failed_media_does_not_block_second_summary_and_two_valid_summaries_upload(self):
        failed = summary("مس و ملوان", 1075800, self.now)
        first = summary("استقلال و گل گهر", 1075801, self.now)
        second = summary("فولاد و سپاهان", 1075802, self.now)
        pending = [failed, first, second]
        state = {"pending_queue": pending.copy(), "seen_mp4s": [], "rejected_urls": [],
                 "last_run": {"media_retries": {}, "rejected_by_reason": {}}}

        def publish(youtube, state, policy, quota_key, candidate, *args):
            state["pending_queue"].remove(candidate)
            return True

        with patch.object(uploader, "save_state"), \
             patch.object(uploader, "call_ai_copyright_audit", return_value=True), \
             patch.object(uploader, "download_candidate", side_effect=["", "", first["mp4_url"], second["mp4_url"]]), \
             patch.object(uploader, "get_exact_video_dimensions", return_value=(1920, 1080, 180)), \
             patch.object(uploader.media_utils, "inspect_audio_file", return_value=(None, -4)), \
             patch.object(uploader, "publish_candidate", side_effect=publish) as upload:
            uploader.process_queue(Mock(), state, {}, "today", set(), set(), set(),
                                   ".", time.monotonic() + 500, priority_only=True)
        self.assertEqual(state["pending_queue"], [failed])
        self.assertEqual([c.args[4] for c in upload.call_args_list], [first, second])
        self.assertEqual(state["last_run"]["media_retries"], {"download_failed": 1})
        self.assertGreater(failed["next_media_attempt"], time.time())

    def test_priority_success_records_format_without_moving_routine_clocks(self):
        item = summary("استقلال ۲ - ۰ گل گهر", 1075801, self.now)
        old_clock = (self.now - timedelta(hours=1)).timestamp()
        state = {"pending_queue": [item], "uploaded_urls": [], "seen_mp4s": [],
                 "uploaded_titles": [], "seen_raw_titles": [], "daily_uploads": {},
                 "last_run": {"uploads": {"priority_summaries": 0}, "learning": {}},
                 "last_upload_timestamp": old_clock, "last_short_upload_timestamp": old_clock}
        youtube = Mock()
        youtube.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "vid1"})
        with patch.object(uploader, "save_state"), patch.object(uploader, "MediaFileUpload"), \
             patch.object(uploader, "add_video_to_playlists"), \
             patch.object(uploader, "call_generate_seo", return_value={"title": item["title"]}):
            self.assertTrue(uploader.publish_candidate(
                youtube, state, {"revision": "test"}, "today", item,
                "SUMMARY", True, True, "video.mp4", "thumbnail.jpg", self.now, time.monotonic() + 180
            ))
        record = state["published_metadata"]["vid1"]
        self.assertEqual((record["category"], record["format"], record["priority_summary"]),
                         ("SUMMARY", "short", True))
        self.assertEqual(state["last_upload_timestamp"], old_clock)
        self.assertEqual(state["last_short_upload_timestamp"], old_clock)
        self.assertIn(item["source_url"], youtube.videos.return_value.insert.call_args.kwargs["body"]["snippet"]["description"])

    def test_platform_cooldown_still_prevents_uploads(self):
        item = summary("استقلال و گل گهر", 1075801, self.now)
        state = {"pending_queue": [item], "upload_limit_cooldown_until": time.time() + 60}
        with patch.object(uploader, "publish_candidate") as upload:
            uploader.process_queue(Mock(), state, {}, "today", set(), set(), set(),
                                   ".", time.monotonic() + 500, priority_only=True)
        upload.assert_not_called()

    def test_priority_media_retry_is_short_and_ignores_backoff(self):
        item = summary("پرسپولیس و پیکان", 1075803, self.now)
        state = {"last_run": {"media_retries": {}}}
        with patch.object(uploader, "save_state"):
            uploader.retain_media_retry(item, state, "download_failed", priority=True)
        self.assertLess(item["next_media_attempt"] - time.time(), 70)

    def test_priority_horizontal_video_under_three_minutes_is_rejected(self):
        item = summary("پرسپولیس و پیکان", 1075804, self.now)
        state = {"pending_queue": [item], "uploaded_urls": [], "seen_mp4s": [],
                 "rejected_urls": [], "last_run": {"media_retries": {}, "rejected_by_reason": {}}}
        with patch.object(uploader, "save_state"), \
             patch.object(uploader, "call_ai_copyright_audit", return_value=True), \
             patch.object(uploader, "download_candidate", return_value=item["mp4_url"]), \
             patch.object(uploader, "get_exact_video_dimensions", return_value=(1920, 1080, 179)), \
             patch.object(uploader, "publish_candidate") as upload:
            uploader.process_queue(Mock(), state, {}, "today", set(), set(), set(),
                                   ".", time.monotonic() + 500, priority_only=True)
        upload.assert_not_called()
        self.assertEqual(state["last_run"]["rejected_by_reason"], {"horizontal_under_three_minutes": 1})


if __name__ == "__main__":
    unittest.main()
