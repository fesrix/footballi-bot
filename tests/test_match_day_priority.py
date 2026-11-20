import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import scrapers
import footballi_uploader as uploader


TEHRAN = ZoneInfo("Asia/Tehran")


LIVE_HTML = """
<h3>لیگ برتر خلیج فارس</h3>
<div class="match-card">
  <a>ملوان</a><span>۱۶:۳۰</span><a>گل گهر سیرجان</a>
</div>
<h3>لیگ ستارگان قطر</h3>
<div class="match-card">
  <a>الاهلی قطر</a><span>۱۹:۰۰</span><a>الشمال</a>
</div>
"""


class MatchDayPriorityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=TEHRAN)

    def test_live_parser_normalizes_digits_and_keeps_iranian_fixture(self):
        fixtures = scrapers.parse_live_fixtures_html(LIVE_HTML, now=self.now)
        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["home_team"], "ملوان")
        self.assertEqual(fixture["away_team"], "گل گهر سیرجان")
        self.assertEqual(fixture["kickoff_at"], "2026-11-20T16:30:00+03:30")

    def test_rendered_sports_event_metadata_is_authoritative(self):
        html = """
        <h3>لیگ برتر خلیج فارس</h3>
        <div class="one-game noselect" itemscope="itemscope">
          <a class="overlay-link" href="/match/1/foo"></a>
          <meta itemprop="startDate" content="2026-11-20T15:30:00.000Z">
          <div class="home-team"><img alt="فجر سپاسی"></div>
          <div class="center-block"><div class="status-text">پایان</div></div>
          <div class="away-team"><img alt="چادرملو اردکان"></div>
        </div>
        """
        fixtures = scrapers.parse_live_fixtures_html(html, now=self.now)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["status"], "finished")
        self.assertEqual(fixtures[0]["kickoff_at"], "2026-11-20T19:00:00+03:30")

    def test_match_day_window_and_content_type(self):
        kickoff = self.now.replace(hour=16, minute=30)
        fixture = {
            "fixture_id": "fixture-1", "home_team": "ملوان", "away_team": "گل گهر سیرجان",
            "competition": "لیگ برتر", "kickoff_at": kickoff.isoformat(),
        }
        item = {
            "source": "footballi_web", "source_url": "https://footballi.net/video/1076000",
            "rights_status": "internal_authorized", "title": "مصاحبه هواداران ملوان قبل از بازی",
            "description": "", "source_published_at": (self.now - timedelta(minutes=15)).isoformat(),
        }
        state = {"live_fixtures": {"fixtures": [fixture]}}
        context = uploader.priority_item_context(item, state, self.now)
        self.assertEqual(context["priority_class"], "match_day")
        self.assertEqual(context["fixture_relation"], "home_team")
        self.assertTrue(uploader.is_priority_item(item, state, self.now))
        self.assertFalse(uploader.is_priority_item(item, state, self.now - timedelta(hours=7)))

    def test_generic_description_cannot_turn_a_goal_clip_into_fan_priority(self):
        kickoff = self.now.replace(hour=16, minute=30)
        fixture = {
            "fixture_id": "fixture-1", "home_team": "پرسپولیس", "away_team": "پیکان",
            "competition": "لیگ برتر", "kickoff_at": kickoff.isoformat(),
        }
        item = {
            "source": "footballi_web", "source_url": "https://footballi.net/video/1076002",
            "rights_status": "internal_authorized", "title": "گل سوم پرسپولیس به پیکان",
            "description": "سکو و هواداران در پوشش فوتبال امروز", "source_published_at": self.now.isoformat(),
        }
        self.assertFalse(uploader.is_priority_item(item, {"live_fixtures": {"fixtures": [fixture]}}, self.now))

    def test_editorial_categories_bypass_routine_schedule_with_rights(self):
        item = {
            "source": "footballi_web", "source_url": "https://footballi.net/video/1076001",
            "rights_status": "internal_authorized", "title": "فوتبال برتر؛ بررسی بازی امروز",
            "source_published_at": self.now.isoformat(),
        }
        state = {"live_fixtures": {"fixtures": []},
                 "published_metadata": {
                     str(i): {"category": "VIDEO", "published_at": self.now.isoformat()}
                     for i in range(4)
                 },
                 "last_upload_timestamp": 0, "last_short_upload_timestamp": 0}
        self.assertEqual(uploader.detect_primary_category(item["title"]), "FOOTBALL_BARTAR")
        self.assertFalse(uploader.publication_block(item, "FOOTBALL_BARTAR", state, {}, self.now))
        item["rights_status"] = ""
        self.assertEqual(uploader.publication_block(item, "FOOTBALL_BARTAR", state, {}, self.now),
                         "daily main upload cap")

    @patch.object(scrapers.requests, "get")
    def test_live_fetch_falls_back_to_empty_without_crashing(self, get):
        get.side_effect = RuntimeError("offline")
        with patch.object(scrapers, "_render_live_scores_html", return_value=""):
            self.assertEqual(scrapers.fetch_live_fixtures(now=self.now), [])
        self.assertEqual(scrapers.LAST_LIVE_SCORES_STATUS, "failed")


if __name__ == "__main__":
    unittest.main()
