import unittest
from unittest.mock import Mock, patch

import footballi_uploader as uploader
from programme_hold import blocked_candidate_programme, blocked_programme


class ProgrammeHoldTests(unittest.TestCase):
    @patch.dict("os.environ", {"FOOTBALLI_PROGRAMME_HOLD": "true"})
    def test_source_metadata_and_spelling(self):
        for phrase in ("فوتبال برتر", "فوتبال‌برتر", "فوتبالبرتر", "فوتبال۳۶۰/برتر",
                       "football_bartar", "FootballBartar", "شب‌های فوتبالی",
                       "شبهای فوتبالی", "shabhaye footballi", "SHAB-HAYE_FOOTBALLI"):
            with self.subTest(phrase=phrase):
                self.assertTrue(blocked_programme({"source": {"tags": [phrase]}}))
        self.assertTrue(blocked_programme("https://footballi.net/video/42/football%20bartar.mp4"))
        self.assertFalse(blocked_programme("خلاصه لیگ برتر، فوتبال 360 و ویدیوکست فوتبالی"))
        self.assertFalse(blocked_candidate_programme({
            "title": "مصاحبه با میثاقی", "category": "FOOTBALL_BARTAR",
            "priority_reason": "category:FOOTBALL_BARTAR",
        }))

    @patch.dict("os.environ", {"FOOTBALLI_PROGRAMME_HOLD": "false"})
    def test_switch_disables_the_hold(self):
        self.assertFalse(blocked_programme("شب‌های فوتبالی", "football bartar"))

    @patch.dict("os.environ", {"FOOTBALLI_PROGRAMME_HOLD": "true"})
    def test_generated_tags_are_checked_before_upload(self):
        youtube = Mock()
        candidate = {"title": "خلاصه بازی استقلال", "video_id": 42}
        with patch.object(uploader, "call_generate_seo", return_value={"tags": ["football bartar"]}), \
             patch.object(uploader, "save_state"):
            result = uploader.publish_candidate(youtube, {}, {}, "", candidate, "SUMMARY", False,
                                                True, "video.mp4", "", None, 0)
        self.assertFalse(result)
        self.assertEqual(candidate["programme_hold"], "football_bartar")
        youtube.videos.assert_not_called()
