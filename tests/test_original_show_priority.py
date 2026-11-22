import time
import unittest
from unittest.mock import patch

import footballi_uploader as uploader


class OriginalShowPriorityTests(unittest.TestCase):
    def test_queue_attempts_both_programmes_before_other_originals(self):
        titles = ["گفتگوی اختصاصی", "ویدیو کست فوتبالی", "گزارش اختصاصی", "مصاحبه | پشت‌خط"]
        queue = [{"title": title, "url": f"video/{i}", "mp4_url": f"video/{i}.mp4", "is_short": i == 3}
                 for i, title in enumerate(titles)]
        with patch.object(uploader.originals, "originals_only", return_value=True), \
             patch.object(uploader, "publication_block", return_value=""), \
             patch.object(uploader, "call_ai_copyright_audit", return_value=True), \
             patch.object(uploader, "is_strictly_domestic_short", return_value=True), \
             patch.object(uploader, "download_candidate", return_value="") as download, \
             patch.object(uploader, "retain_media_retry"):
            uploader.process_queue(None, {"pending_queue": queue}, {}, "today", set(), set(), set(),
                                   "unused", time.monotonic() + 120, priority_only=True)
        self.assertEqual([call.args[0] for call in download.call_args_list],
                         [queue[1], queue[3], queue[0], queue[2]])

    def test_title_keeps_exact_programme_spelling_for_main_and_shorts(self):
        name = "پُشتِ‌ خَط"
        candidate = {"source_title": "مصاحبه مربی | پشت خط"}
        for category in ("VIDEO", "SHORTS", "POSHTE_KHAT"):
            for variant in ("پشت خط", "پشت‌خط", "پشتخط", "پُشتِ خَط", name):
                title = uploader.format_upload_title(f"گفتگو | {variant}", candidate, category)
                self.assertEqual(title, f"گفتگو | {name}")
            self.assertEqual(uploader.format_upload_title("گفتگو", candidate, category), f"{name} | گفتگو")
        title = uploader.format_upload_title("گ" * 100 + " | پشت خط", candidate, "VIDEO")
        self.assertTrue(title.startswith(name))
        self.assertEqual(len(title), 100)
        self.assertEqual(uploader.format_upload_title("مصاحبه", {}, "VIDEO"), "مصاحبه")
