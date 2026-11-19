import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from googleapiclient.errors import HttpError
from config import PLAYLIST_DEFINITIONS
from footballi_uploader import (
    add_video_to_playlists,
    detect_all_matching_playlists,
    ensure_all_playlists,
    organize_existing_channel_videos,
    playlist_demand,
)


def response(data):
    return SimpleNamespace(execute=lambda: data)


class PlaylistTests(unittest.TestCase):
    def test_headline_evidence_and_verified_shorts(self):
        keys = detect_all_matching_playlists("خلاصه بازی پرسپولیس و فجرسپاسی",
                                             "برچسب: سپاهان #Shorts")
        self.assertEqual(keys, ["SUMMARY", "PERSEPOLIS", "FAJR_SEPASI"])
        keys = detect_all_matching_playlists("اعتراض هواداران فجر سپاسی", is_short=True)
        self.assertEqual(keys, ["SHORTS", "FANS_STADIUM", "FAJR_SEPASI"])

    def test_generic_suffix_or_club_surname_does_not_create_a_match(self):
        self.assertEqual(detect_all_matching_playlists("حضور حسینی در تمرینات | حواشی لیگ برتر"), [])
        self.assertEqual(detect_all_matching_playlists("بازگشت دروازه‌بان به استقلال خوزستان"), [])
        self.assertEqual(detect_all_matching_playlists("روی سکوهای خالی، گزارش بازار"), [])
        self.assertIn("INTERVIEW", detect_all_matching_playlists("گفت‌وگو با هادی طباطبایی"))

    def test_original_title_can_keep_editorial_playlist(self):
        self.assertIn("POSHTE_KHAT", detect_all_matching_playlists(
            "واکنش فرزاد آشوبی به مصاحبه محمودی", source_title="پُشتِ خَط / گفت‌وگو با فرزاد آشوبی"))
        self.assertNotIn("SECOND_TIER", detect_all_matching_playlists("آلومینیوم و مس شهر بابک در جام حذفی"))
        self.assertIn("SECOND_TIER", detect_all_matching_playlists("درگیری در بازی آریو در لیگ آزادگان"))

    def test_demand_counts_distinct_public_video_ids(self):
        public = {"video_id": "a", "title": "باشگاه فجر سپاسی", "public": True}
        private = {"video_id": "b", "title": public["title"], "public": False}
        self.assertEqual(playlist_demand([public, public, private], {}), {"FAJR_SEPASI": 1})

    def test_creates_only_when_ready_and_reuses_a_manual_playlist(self):
        state = {"playlists": {key: key for key, value in PLAYLIST_DEFINITIONS.items()
                               if not value.get("minimum_public_videos")}}
        yt = Mock()
        yt.playlists.return_value.list.side_effect = [
            response({"items": [], "nextPageToken": "next"}),
            response({"items": [{"id": "fajr-existing",
                                 "snippet": {"title": PLAYLIST_DEFINITIONS["FAJR_SEPASI"]["title"]},
                                 "status": {"privacyStatus": "public"}}]}),
        ]
        yt.playlists.return_value.insert.return_value = response({"id": "fans-created"})
        with patch("footballi_uploader.save_state"):
            result = ensure_all_playlists(yt, state, {"FAJR_SEPASI": 4, "FANS_STADIUM": 3, "TACTICAL": 2})
        self.assertEqual(result["FAJR_SEPASI"], "fajr-existing")
        self.assertEqual(result["FANS_STADIUM"], "fans-created")
        self.assertNotIn("TACTICAL", result)
        yt.playlists.return_value.insert.assert_called_once()

    def test_backfill_avoids_duplicate_items_and_private_videos(self):
        yt = Mock()
        yt.playlistItems.return_value.list.return_value = response({"items": [{"id": "existing-item"}]})
        playlists = {"FAJR_SEPASI": "playlist"}
        state = {}
        videos = [{"video_id": "hidden", "title": "فجر سپاسی", "public": False},
                  {"video_id": "already-present", "title": "فجر سپاسی", "public": True}]
        organize_existing_channel_videos(yt, videos, playlists, state, max_ops_per_run=1)
        yt.playlistItems.return_value.insert.assert_not_called()
        self.assertEqual(state["playlist_assignments"], {"already-present": ["FAJR_SEPASI"]})
        self.assertEqual(add_video_to_playlists(
            yt, "already-present", ["FAJR_SEPASI"], playlists, state), 0)
        yt.playlistItems.return_value.list.assert_called_once()

    def test_unreadable_new_playlist_is_kept_for_retry(self):
        yt = Mock()
        yt.playlistItems.return_value.list.return_value.execute.side_effect = HttpError(
            SimpleNamespace(status=404, reason="Not Found"),
            b'{"error":{"message":"playlistNotFound"}}',
        )
        state = {"playlists": {"FAJR_SEPASI": "new-playlist"}}
        add_video_to_playlists(yt, "a", ["FAJR_SEPASI"], state["playlists"], state)
        self.assertEqual(state["playlists"]["FAJR_SEPASI"], "new-playlist")
        self.assertNotIn("a", state["playlist_assignments"])
        yt.playlistItems.return_value.insert.assert_not_called()
