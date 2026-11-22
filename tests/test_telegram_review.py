import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import footballi_uploader as uploader
import originals_policy as policy
import telegram_review as telegram


@patch.dict("os.environ", {"FOOTBALLI_ORIGINALS_ONLY": "true",
                          "FOOTBALLI_TELEGRAM_PREVIEW_ONLY": "",
                          "FOOTBALLI_TELEGRAM_CHECKPOINT": ""})
class TelegramReviewTests(unittest.TestCase):
    def setUp(self):
        self.source = {"url": "https://footballi.net/video/1234/behind-the-scenes",
                       "title": "گفتگوی اختصاصی | پشت خط", "mp4_url": "https://media.example/video.mp4",
                       "learning_factor": 1, "is_short": False}
        self.key = telegram.source_id(self.source)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "video.mp4"
        self.path.write_bytes(b"original")
        self.reviewer = telegram.TelegramReview("unused", 8)
        self.card = telegram.prepare_card(self.source, "عنوان", (1280, 720, 240, False),
                                          policy.file_sha256(self.path), "عنوان برنامه در منبع آمده است")
        self.card["message_id"] = 99
        self.state = {"telegram_review": {"offset": 0, "items": {self.key: self.card}}}

    def callback(self, action="a", owner=8, message_id=99):
        return {"id": "query", "from": {"id": owner},
                "message": {"message_id": message_id, "chat": {"id": 8, "type": "private"}},
                "data": f"{action}:{self.card['button_id']}"}

    def approve(self):
        self.reviewer.decide(telegram.review_state(self.state), self.callback())

    def test_only_owner_can_decide_and_first_decision_wins(self):
        review = telegram.review_state(self.state)
        self.reviewer.decide(review, self.callback(owner=2))
        self.reviewer.decide(review, self.callback(message_id=28))
        self.assertEqual(self.card["status"], "waiting")
        self.reviewer.decide(review, self.callback("r"))
        self.approve()
        self.assertEqual(self.card["status"], "rejected")
        self.assertEqual(telegram.rejected_sources(self.state), {self.key})
        self.assertNotIn(self.key, telegram.approvals_for(self.state))

    def test_poll_saves_before_acknowledging_and_can_replay_after_cutoff(self):
        original = copy.deepcopy(self.state)
        update = {"update_id": 42, "callback_query": self.callback()}
        with patch.object(self.reviewer, "call", return_value=[update]) as api:
            with self.assertRaises(OSError):
                self.reviewer.poll(self.state, Mock(side_effect=OSError))
            self.assertEqual([call.args[0] for call in api.call_args_list], ["getUpdates"])
        order = Mock()
        order.api.side_effect = [[update], True]
        with patch.object(self.reviewer, "call", order.api):
            self.reviewer.poll(original, order.persist)
        self.assertEqual([call[0] for call in order.mock_calls], ["api", "persist", "api"])
        self.assertEqual(original["telegram_review"]["offset"], 43)
        self.assertEqual(original["telegram_review"]["items"][self.key]["status"], "approved")

    def test_failed_send_keeps_same_button_for_retry(self):
        self.card.pop("message_id")
        with patch.object(self.reviewer, "call", side_effect=[None, {"message_id": 99}]) as api:
            self.assertFalse(self.reviewer.send_card(self.card))
            self.assertTrue(self.reviewer.send_card(self.card))
            self.assertEqual(api.call_args_list[0], api.call_args_list[1])

    def test_excluded_content_never_downloads_for_review(self):
        self.state["originals_waiting"] = [
            {**self.source, "title": "خلاصه بازی پرسپولیس", "originals_decision": "editorial_approval_missing"}
        ]
        with patch.object(uploader, "download_candidate") as download:
            uploader.propose_telegram_reviews(self.reviewer, self.state, str(self.path.parent),
                                              time.monotonic() + 180)
        download.assert_not_called()

    def test_proposes_verified_footballi_file(self):
        self.state["telegram_review"]["items"].clear()
        self.state["originals_waiting"] = [{**self.source, "originals_decision": "editorial_approval_missing"}]
        with patch.object(uploader, "download_candidate", return_value=self.source["mp4_url"]), \
             patch.object(uploader, "get_exact_video_dimensions", return_value=(1280, 720, 233)), \
             patch.object(uploader.media_utils, "inspect_audio_file", return_value=(None, -3)), \
             patch.object(policy, "audit_with_ai", return_value="approved"), \
             patch.object(policy, "file_sha256", return_value=self.card["sha256"]), \
             patch.object(telegram, "checkpoint"), patch.object(self.reviewer, "send_card", return_value=True):
            uploader.propose_telegram_reviews(self.reviewer, self.state, str(self.path.parent),
                                              time.monotonic() + 180)
        proposal = telegram.review_state(self.state)["items"][self.key]
        self.assertIn("پُشتِ‌ خَط", proposal["card"])
        self.assertIn("233", proposal["card"])

    def publish(self, youtube, *, short=False, audit="approved"):
        self.state.update(pending_queue=[self.source], daily_uploads={}, uploaded_urls=[],
                          seen_mp4s=[], uploaded_titles=[], seen_raw_titles=[],
                          last_run={"uploads": {}, "learning": {}})
        with patch.object(policy, "audit_with_ai", return_value=audit), \
             patch.object(uploader, "call_generate_seo", return_value={"title": self.source["title"]}), \
             patch.object(uploader, "MediaFileUpload"), patch.object(uploader, "save_state"), \
             patch.object(uploader, "call_prepare_thumbnail"), patch.object(uploader, "add_video_to_playlists"):
            return uploader.publish_candidate(youtube, self.state, {"revision": "test"}, "today", self.source,
                                              "SHORTS" if short else "VIDEO", short, False,
                                              str(self.path), "", None, time.monotonic() + 120)

    def test_public_upload_for_both_formats(self):
        for short in (False, True):
            self.card["status"] = "waiting"
            self.approve()
            youtube = Mock()
            youtube.videos.return_value.insert.return_value.next_chunk.return_value = None, {"id": "video123"}
            self.assertTrue(self.publish(youtube, short=short))
            body = youtube.videos.return_value.insert.call_args.kwargs["body"]
            self.assertEqual(body["status"]["privacyStatus"], "public")
            self.assertIn("پُشتِ‌ خَط", body["snippet"]["title"])
            self.assertEqual(self.card["status"], "published")

    def test_ambiguous_ai_or_changed_file_never_uploads(self):
        self.approve()
        youtube = Mock()
        self.assertFalse(self.publish(youtube, audit="ai_unconfirmed"))
        self.path.write_bytes(b"changed")
        self.assertFalse(self.publish(youtube))
        self.assertEqual(self.card["status"], "file_changed")
        self.assertNotIn(self.key, telegram.approvals_for(self.state))
        youtube.videos.assert_not_called()

    def test_uncertain_upload_reconciles_without_inserting_again(self):
        self.approve()
        youtube = Mock()
        youtube.videos.return_value.insert.return_value.next_chunk.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.publish(youtube)
        self.assertEqual(self.card["status"], "upload_uncertain")
        self.assertFalse(self.publish(youtube))
        self.assertEqual(youtube.videos.return_value.insert.call_count, 1)
        uploader.reconcile_telegram_uploads(self.state, [])
        self.assertEqual(self.card["status"], "upload_uncertain")
        self.state["published_metadata"] = {}
        uploader.reconcile_telegram_uploads(self.state, [{"public": True, "video_id": "video123",
            "title": "عنوان", "description": f"Footballi ID: {self.card['button_id']}"}])
        self.assertEqual(self.card["status"], "published")
        self.assertEqual(self.state["pending_queue"], [])

    @patch.dict("os.environ", {"FOOTBALLI_TELEGRAM_PREVIEW_ONLY": "true"})
    def test_preview_approval_cannot_authorize_publication(self):
        self.card["preview"] = True
        self.approve()
        self.assertNotIn(self.key, telegram.approvals_for(self.state))
        youtube = Mock()
        self.assertFalse(self.publish(youtube))
        youtube.videos.assert_not_called()
