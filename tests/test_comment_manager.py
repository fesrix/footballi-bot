import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import comment_manager as cm


def comment(cid, author="viewer", text="Test comment"):
    return {"id": cid, "snippet": {"authorChannelId": {"value": author},
                                   "textDisplay": text,
                                   "publishedAt": datetime.now(timezone.utc).isoformat()}}


class YouTube:
    def __init__(self, count):
        self.top = {str(i): comment(str(i)) for i in range(count)}
        self.replies = []
        self.insert_calls = 0
        self.lose_response = False

    def commentThreads(self):
        def list_threads(pageToken=None, maxResults=100, **kwargs):
            offset = int(pageToken or 0)
            ids = list(self.top)[offset:offset + maxResults]
            items = [{"snippet": {"videoId": "video", "topLevelComment": self.top[cid],
                                  "totalReplyCount": len(self.replies) if cid == "0" else 0}}
                     for cid in ids]
            return SimpleNamespace(execute=lambda: {
                "items": items,
                "nextPageToken": str(offset + maxResults) if offset + maxResults < len(self.top) else None,
            })
        return SimpleNamespace(list=list_threads)

    def comments(self):
        def list_comments(id=None, parentId=None, **kwargs):
            by_id = {**self.top, **{r["id"]: r for r in self.replies}}
            items = self.replies if parentId is not None else [
                by_id[cid] for cid in id.split(",") if cid in by_id]
            return SimpleNamespace(execute=lambda: {"items": items})

        def insert(**kwargs):
            def execute():
                self.insert_calls += 1
                self.replies.append(comment("from-channel", "owner",
                                            kwargs["body"]["snippet"]["textOriginal"]))
                if self.lose_response:
                    raise TimeoutError("Response dropped after commit")
            return SimpleNamespace(execute=execute)

        return SimpleNamespace(list=list_comments, insert=insert)

    def videos(self):
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"items": []}))


class CommentReviewTests(unittest.TestCase):
    def setUp(self):
        self.state = {"processed_comments": ["0"], "ignored_comments": ["0"],
                      "pending_queue": [{"title": "video"}]}
        self.addCleanup(patch.stopall)
        patch.object(cm, "save_history").start()

    def review(self, youtube):
        worker = cm.CommentReview(self.state, youtube, time.monotonic() + 210)
        worker.channel_id = "owner"
        return worker

    def test_archive_continues_until_older_than_first_thousand(self):
        youtube = YouTube(1101)
        seen = set()
        for _ in range(4):
            worker = self.review(youtube)
            worker.scan("archive_token", 3)
            seen.update(worker.queue)
            for cid in list(worker.queue):
                worker.terminal(cid, "reviewed_no_reply", "NO_REPLY_NEEDED")
        self.assertEqual(len(seen), 1101)
        self.assertIsNone(worker.data["archive_token"])
        self.assertEqual(self.state["pending_queue"], [{"title": "video"}])
        self.assertEqual(self.state["processed_comments"], ["0"])

    def test_queue_limit_preserves_page_for_resume(self):
        youtube = YouTube(600)
        worker = self.review(youtube)
        worker.scan("archive_token", 10)
        self.assertEqual(len(worker.queue), 500)
        self.assertEqual(worker.data["archive_token"], "500")
        for cid in list(worker.queue)[:100]:
            worker.terminal(cid, "reviewed_no_reply", "NO_REPLY_NEEDED")
        worker.scan("archive_token", 1)
        self.assertIn("599", worker.queue)
        self.assertIsNone(worker.data["archive_token"])

    def test_hidden_replies_and_edited_comments(self):
        youtube = YouTube(1)
        youtube.replies = [comment("owner-comment", "owner"), comment("viewer-reply")]
        worker = self.review(youtube)
        worker.scan(None, 1)
        worker.scan_replies()
        self.assertIn("viewer-reply", worker.queue)
        self.assertNotIn("owner-comment", worker.queue)
        worker.terminal("0", "reviewed_no_reply", "NO_REPLY_NEEDED")
        youtube.top["0"]["snippet"]["textDisplay"] = "Edited"
        worker.scan(None, 1)
        self.assertEqual(worker.queue["0"]["status"], "pending")

    def test_lost_reply_response_is_reconciled_after_restart(self):
        youtube = YouTube(1)
        youtube.lose_response = True
        worker = self.review(youtube)
        worker.scan(None, 1)
        worker.queue["0"].update(action="REPLY", reason="ENGAGE", reply="درست می‌گی")
        worker.apply_decisions()
        self.assertEqual(worker.queue["0"]["status"], "retry")
        self.assertEqual(worker.quota["write"], 50)
        worker = self.review(youtube)
        worker.queue["0"]["retry_at"] = 0
        worker.apply_decisions()
        self.assertEqual(youtube.insert_calls, 1)
        self.assertEqual(worker.records["0"]["status"], "replied")

    def test_partial_ai_reply_is_preserved_even_at_write_cap(self):
        youtube = YouTube(2)
        worker = self.review(youtube)
        worker.scan(None, 1)
        worker.reply_attempts = cm.MAX_REPLIES_PER_RUN
        with patch.object(cm, "ai_evaluate_comments",
                          return_value={"0": {"action": "REPLY", "reason": "ENGAGE", "reply": "درست می‌گی"}}):
            worker.review_pending()
        worker.apply_decisions()
        self.assertEqual(worker.queue["0"]["action"], "REPLY")
        self.assertEqual(worker.queue["1"]["status"], "retry")
        self.assertEqual(youtube.insert_calls, 0)

    def test_quota_and_deadline_prevent_extra_calls(self):
        worker = self.review(YouTube(1))
        worker.quota["read"] = cm.READ_BUDGET
        request = SimpleNamespace(execute=lambda: self.fail("Quota exhausted"))
        with self.assertRaises(cm.ReviewDeferred):
            worker.api(request)
        worker.deadline = time.monotonic()
        with self.assertRaises(cm.ReviewDeferred):
            worker.api(request, cost=50, kind="write")

    def test_invalid_model_output_cannot_mark_a_comment_complete(self):
        self.assertIsNone(cm.validated_decision({"action": "REPLY", "reason": "ENGAGE", "reply": None}))
        self.assertIsNone(cm.validated_decision({"action": "IGNORE", "reason": "SPAM_SCAM", "reply": ""}))
        self.assertEqual(cm._parse_ai_json('{"decisions":[{"action":"DELETE"}]}'), {})
