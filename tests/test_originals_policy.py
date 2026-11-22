import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import footballi_uploader as uploader
import originals_policy as policy


@patch.dict("os.environ", {"FOOTBALLI_ORIGINALS_ONLY": "", "GEMINI_API_KEY": "mock"})
class OriginalsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.item = {"url": "https://footballi.net/video/1076500/مصاحبه",
                     "title": "مصاحبه اختصاصی فوتبالی با مربی", "is_short": False,
                     "mp4_url": "https://cdn.example.test/original.mp4", "learning_factor": 1}
        self.record = {"production_evidence": "Footage and audio recorded by Footballi",
                       "visual_rights_confirmed": True, "audio_rights_confirmed": True,
                       "sha256": hashlib.sha256(b"approved file").hexdigest()}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "video.mp4"
        self.path.write_bytes(b"approved file")
        registry = Path(temp.name) / "approved.json"
        registry.write_text(json.dumps({self.item["url"]: self.record}), encoding="utf-8")
        mock_registry = patch.object(policy, "APPROVALS", registry)
        mock_registry.start()
        self.addCleanup(mock_registry.stop)

    def test_queue_keeps_previous_items_and_ignores_legacy_rights(self):
        highlights = {**self.item, "title": "خلاصه‌بازی پرسپولیس", "priority_class": "summary"}
        repost = {**self.item, "url": "https://footballi.net/video/1076501", "rights_status": "internal_authorized"}
        state = {"pending_queue": [highlights, repost], "published_metadata": {"history": {}}}
        policy.repartition_queue(state)
        self.assertEqual(state["pending_queue"], [])
        self.assertEqual([x["originals_decision"] for x in state["originals_waiting"]],
                         ["excluded_content", "editorial_approval_missing"])
        self.assertEqual(state["published_metadata"], {"history": {}})
        with patch.object(policy, "load_approvals", return_value={"footballi:1076501": self.record}):
            policy.repartition_queue(state)
        self.assertEqual(state["pending_queue"], [repost])

    def test_changed_file_and_blocked_final_tags_never_reach_ai(self):
        with patch.object(policy, "audit_with_ai") as audit:
            self.assertEqual(policy.verify_upload(self.item, self.path, {"tags": ["Shabhaye Footballi"]}),
                             "excluded_programme")
            self.path.write_bytes(b"changed")
            self.assertEqual(policy.verify_upload(self.item, self.path, {}), "file_hash_mismatch")
        audit.assert_not_called()

    def test_ambiguous_ai_cannot_upload(self):
        reply = {"candidates": [{"content": {"parts": [{"text":
                 '{"footballi_original":"true","third_party_or_excluded":false}'}]}}]}
        youtube = Mock()
        state = {"pending_queue": [self.item]}
        with patch.object(policy.requests, "post") as post, \
             patch.object(uploader, "call_generate_seo", return_value={"title": self.item["title"]}), \
             patch.object(uploader, "save_state"):
            post.return_value.json.return_value = reply
            self.assertFalse(uploader.publish_candidate(youtube, state, {}, "", self.item, "VIDEO",
                                                        False, False, str(self.path), "", None,
                                                        time.monotonic() + 100))
        youtube.videos.assert_not_called()
        self.assertEqual(self.item["originals_decision"], "ai_unconfirmed")

    def test_verified_main_and_short_publish_public_outside_normal_limits(self):
        for width, height, seconds in ((1920, 1080, 180), (1080, 1920, 60)):
            item = dict(self.item)
            youtube = Mock()
            state = {"pending_queue": [item], "published_metadata": {}, "daily_uploads": {},
                     "uploaded_urls": [], "seen_mp4s": [], "uploaded_titles": [], "seen_raw_titles": [],
                     "last_run": {"uploads": {}, "learning": {}}, "rejected_urls": []}
            youtube.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "test"})
            with patch.object(uploader, "is_in_upload_window", return_value=False), \
                 patch.object(uploader, "daily_publishing_counts", return_value={"main": 100, "short": 100}), \
                 patch.object(uploader, "download_candidate", return_value=item["mp4_url"]), \
                 patch.object(uploader, "get_exact_video_dimensions", return_value=(width, height, seconds)), \
                 patch.object(uploader, "is_strictly_domestic_short", return_value=True), \
                 patch.object(uploader, "call_ai_copyright_audit", return_value=True), \
                 patch.object(uploader.media_utils, "inspect_audio_file", return_value=(None, -2.0)), \
                 patch.object(policy, "audit_with_ai", return_value="approved"), \
                 patch.object(uploader, "call_generate_seo", return_value={"title": item["title"]}), \
                 patch.object(uploader, "save_state"), patch.object(uploader, "MediaFileUpload"), \
                 patch.object(uploader, "call_prepare_thumbnail"), patch.object(uploader, "add_video_to_playlists"):
                uploader.process_queue(youtube, state, {"revision": "test"}, "", set(), set(), set(),
                                       str(self.path.parent), time.monotonic() + 120, priority_only=True)
            body = youtube.videos.return_value.insert.call_args.kwargs["body"]
            self.assertEqual(body["status"]["privacyStatus"], "public")
            self.assertEqual(state["published_metadata"]["test"]["originals_decision"], "approved")
