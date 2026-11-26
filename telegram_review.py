"""Private Telegram approval cards for exact Footballi media files."""

import hashlib
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

import originals_policy as originals

KEYBOARD_REVIEW_ROWS = 12


def preview_only():
    return os.getenv("FOOTBALLI_TELEGRAM_PREVIEW_ONLY", "").lower() == "true"


def review_state(state):
    return state.setdefault("telegram_review", {"version": 1, "offset": 0, "items": {}})


def source_id(candidate):
    return originals.source_key(candidate.get("source_url") or candidate.get("url"))


def approval_route(state, candidate):
    item = state.get("telegram_review", {}).get("items", {}).get(source_id(candidate), {})
    if item.get("status") == "approved" and not item.get("preview"):
        return item.get("approval", {}).get("route", "")
    return ""


def approvals_for(state):
    approvals = originals.load_approvals()
    for key, item in review_state(state)["items"].items():
        if item["status"] == "rejected" and not item["preview"]:
            approvals.pop(key, None)
        elif item["status"] in {"approved", "upload_uncertain"} and not item["preview"]:
            approvals[key] = item["approval"]
    return approvals


def rejected_sources(state):
    return {key for key, item in review_state(state)["items"].items()
            if item["status"] == "rejected" and not item["preview"]}


def checkpoint(state, save):
    """Push before acknowledging decisions or starting a non-idempotent upload."""
    save(state)
    if os.getenv("FOOTBALLI_TELEGRAM_CHECKPOINT") != "true":
        return
    subprocess.run(["git", "add", "--", "uploaded_matches.json"], check=True, timeout=10)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], timeout=10)
    if diff.returncode == 0:
        return
    if diff.returncode != 1:
        raise RuntimeError("Could not inspect the review checkpoint")
    subprocess.run(["git", "commit", "-m", "Checkpoint Footballi review [skip ci]"],
                   check=True, timeout=15)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, timeout=25)


class TelegramReview:
    def __init__(self, token, owner):
        self.token = token
        self.owner = owner

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        owner = os.getenv("TELEGRAM_REVIEWER_ID", "").strip()
        if re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) and re.fullmatch(r"[1-9]\d{0,19}", owner):
            return cls(token, int(owner))
        if token or owner:
            print("[TELEGRAM] Review configuration incomplete or invalid.")
        return None

    def call(self, method, **payload):
        try:
            response = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                     json=payload, timeout=(4, 8))
            result = response.json()
            if response.ok and result.get("ok") is True:
                return result.get("result")
            if method == "editMessageText" and "message is not modified" in result.get("description", ""):
                return True
            print(f"[TELEGRAM] {method} failed (HTTP {response.status_code}).")
        except (requests.RequestException, ValueError, AttributeError):
            print(f"[TELEGRAM] {method} unavailable.")
        return None

    def poll(self, state, persist):
        review = review_state(state)
        offset = review["offset"] if time.time() - review.get("updated_at", 0) < 6 * 86400 else 0
        updates = self.call("getUpdates", offset=offset, timeout=2, limit=10,
                            allowed_updates=["message", "callback_query"])
        if not isinstance(updates, list) or not updates:
            return
        callbacks = []
        for update in sorted(updates, key=lambda value: value["update_id"]):
            callback = update.get("callback_query")
            if callback:
                callbacks.append((callback["id"], self.decide(review, callback)))
            message = update.get("message")
            if message and self.is_owner(message.get("from") or {}, message):
                content = (message.get("text") or "").strip()
                match = re.fullmatch(r"(تأیید|رد) ([A-Fa-f0-9]{12})", content)
                if match:
                    action, code = match.groups()
                    items = [item for item in review["items"].values()
                             if item["button_id"].startswith(code.lower())]
                    result = (self.record_decision(items[0], "a" if action == "تأیید" else "r")
                              if len(items) == 1 and items[0].get("message_id")
                              else "این پیشنهاد پیدا نشد یا منقضی شده است.")
                    review.setdefault("receipts", {})[str(message["message_id"])] = result
                elif content.startswith(("/upload", "https://", "http://")):
                    link = content.removeprefix("/upload ").strip()
                    result = self.register_link(state, link, message["message_id"])
                    review.setdefault("receipts", {})[str(message["message_id"])] = result
        review["offset"] = max(update["update_id"] for update in updates) + 1
        review["updated_at"] = time.time()
        persist()
        answer_until = time.monotonic() + 8
        for callback_id, result in callbacks:
            if time.monotonic() >= answer_until:
                break
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=result)

    def is_owner(self, sender, message):
        chat = message.get("chat") or {}
        return sender.get("id") == self.owner and chat.get("id") == self.owner and chat.get("type") == "private"

    def register_link(self, state, link, message_id):
        key = originals.source_key(link) if not re.search(r"\s", link) else ""
        if not key.startswith("footballi:"):
            return "یک لینک مستقیم ویدیوی وب‌سایت فوتبالی با https بفرست."
        if preview_only():
            return "انتشار در حالت آزمایشی غیرفعال است. پس از فعال شدن، لینک را دوباره بفرست."
        review = review_state(state)
        if any(originals.source_key(url) == key for url in state.get("uploaded_urls", [])):
            return "این ویدیو قبلاً منتشر شده است."
        previous = review["items"].get(key)
        if previous and not previous["preview"] and previous["status"] not in {"waiting", "file_changed", "rejected"}:
            return previous.get("result") or "این ویدیو قبلاً بررسی شده است."
        requests = review.setdefault("link_requests", {})
        if key in requests:
            return "این لینک از قبل در صف بررسی است."
        url = urlsplit(link)._replace(query="", fragment="").geturl()
        requests[key] = {"url": url, "message_id": message_id}
        return "درخواست انتشار ثبت شد. پس از بررسی نهایی فایل و حقوق محتوا نتیجه را همین‌جا می‌فرستم."

    def decide(self, review, callback):
        message = callback.get("message") or {}
        if not self.is_owner(callback.get("from") or {}, message):
            return "این حساب اجازهٔ بررسی ندارد."
        match = re.fullmatch(r"([ar]):([a-f0-9]{32})", callback.get("data") or "")
        if not match:
            return "دکمه معتبر نیست."
        action, button_id = match.groups()
        item = next((item for item in review["items"].values() if item["button_id"] == button_id), None)
        if not item:
            return "این پیشنهاد منقضی شده است."
        if item.get("message_id") not in (None, message.get("message_id")):
            return "این پیام معتبر نیست."
        item["message_id"] = message["message_id"]
        return self.record_decision(item, action)

    def record_decision(self, item, action):
        if item["preview"] != preview_only():
            return "این پیشنهاد منقضی شده است."
        if item["status"] != "waiting":
            return "تصمیم این پیشنهاد قبلاً ثبت شده است. " + item.get("result", "")
        item["status"] = "approved" if action == "a" else "rejected"
        item["decided_at"] = datetime.now(timezone.utc).isoformat()
        if action == "a":
            mechanism = ("by submitting a direct Footballi URL as a publishing request"
                         if item.get("request_message_id") else "via the private approval card")
            item["approval"] = {
                "sha256": item["sha256"],
                "production_evidence": (
                    f"An authorized Footballi reviewer confirmed {mechanism} that "
                    "Footballi produced this exact source video and cleared its visual and audio rights "
                    "for YouTube. " + item["evidence"]
                ),
                "visual_rights_confirmed": True,
                "audio_rights_confirmed": True,
                "route": "manual_link" if item.get("request_message_id") else "telegram_review",
            }
        label = "رد شد." if action == "r" else (
            "تأیید آزمایشی ثبت شد. انتشار غیرفعال است." if item["preview"]
            else "تأیید ثبت شد. در انتظار بررسی نهایی فایل است."
        )
        set_notice(item, label)
        return label

    def send_card(self, item):
        code = item["button_id"][:12].upper()
        text = (f"{item['card']}\n\nکد پیشنهاد: {code}\n"
                "دکمهٔ این کد را از صفحه‌کلید بزن، یا فرمان را از همین کارت کپی کن و در چت بفرست. "
                "رسید ثبت تصمیم در اجرای بعدی می‌رسد. پیشنهاد تا زمان تصمیم باز می‌ماند.")
        reply = self.call("sendMessage", chat_id=self.owner, text=text,
                          reply_markup={"inline_keyboard": [[
                              {"text": "کپی فرمان تأیید", "copy_text": {"text": f"تأیید {code}"}},
                              {"text": "کپی فرمان رد", "copy_text": {"text": f"رد {code}"}},
                          ]]})
        if isinstance(reply, dict) and isinstance(reply.get("message_id"), int):
            item["message_id"] = reply["message_id"]
            return True
        return False

    def refresh_keyboard(self, state):
        review = review_state(state)
        waiting = sorted((item for item in review["items"].values()
                          if item["preview"] == preview_only() and item["status"] == "waiting"
                          and item.get("message_id")), key=lambda item: item["message_id"], reverse=True)
        codes = [item["button_id"][:12].upper() for item in waiting[:KEYBOARD_REVIEW_ROWS]]
        count = len(waiting)
        if codes == review.get("keyboard_codes", []) and count == review.get("keyboard_waiting", 0):
            return
        markup = {"keyboard": [[f"تأیید {code}", f"رد {code}"] for code in codes],
                  "resize_keyboard": True} if codes else {"remove_keyboard": True}
        text = (f"{count} پیشنهاد منتظر تصمیم است. دکمهٔ مربوط به کد پیشنهاد را انتخاب کن. "
                "برای کارت‌های قدیمی هم می‌توانی فرمان همان کارت را کپی و ارسال کنی. "
                "✔️ رسید ثبت تصمیم در اجرای نیم‌ساعتهٔ بعد می‌رسد."
                if codes else "تصمیم‌های این صفحه‌کلید پردازش شدند.")
        if self.call("sendMessage", chat_id=self.owner, text=text, reply_markup=markup) is not None:
            review["keyboard_codes"] = codes
            review["keyboard_waiting"] = count

    def flush_receipts(self, state, deadline):
        receipts = review_state(state).get("receipts", {})
        for message_id, result in list(receipts.items()):
            if time.monotonic() + 13 >= deadline:
                break
            reply = self.call("sendMessage", chat_id=self.owner, text=f"✔️ {result}",
                              reply_parameters={"message_id": int(message_id),
                                                "allow_sending_without_reply": True})
            if reply is not None:
                receipts.pop(message_id)

    def send_preview_probe(self, state, persist):
        """Exercise both directions when no real media passed the preview screening."""
        items = review_state(state)["items"]
        if not preview_only() or any(item["preview"] and item["status"] == "waiting"
                                     and item.get("message_id") for item in items.values()):
            return
        probe = items.get("telegram:feedback")
        if probe is None:
            probe = {
                "status": "waiting", "preview": True, "generation": 1,
                "sha256": "", "evidence": "",
                "button_id": hashlib.sha256(os.urandom(32)).hexdigest()[:32],
                "card": "آزمون اتصال ربات فوتبالی\nبرای بررسی ثبت تصمیم، یکی از دکمه‌ها را بزن. این پیام ویدیویی برای انتشار ندارد.",
            }
            items["telegram:feedback"] = probe
            persist()
        if probe["status"] == "waiting" and not probe.get("message_id") and self.send_card(probe):
            print("[TELEGRAM] Preview connection card sent.")

    def flush_notices(self, state, deadline):
        for item in review_state(state)["items"].values():
            if time.monotonic() + 15 >= deadline:
                break
            if not item.get("notice"):
                continue
            if item.get("request_message_id"):
                result = self.call("sendMessage", chat_id=self.owner, text=item["notice"],
                                   reply_parameters={"message_id": item["request_message_id"],
                                                     "allow_sending_without_reply": True})
                if result is not None:
                    item.pop("notice")
            elif item.get("message_id"):
                result = self.call("editMessageText", chat_id=self.owner, message_id=item["message_id"],
                                   text=item["card"] + "\n\n" + item["notice"],
                                   reply_markup={"inline_keyboard": []})
                if result is not None:
                    item.pop("notice")
            elif item.get("approval", {}).get("route") == "trusted_show":
                if self.call("sendMessage", chat_id=self.owner, text=item["notice"]) is not None:
                    item.pop("notice")


def set_notice(item, message):
    if item.get("result") != message:
        item["result"] = item["notice"] = message


def prepare_card(item, title, media, digest, evidence, *, previous=None):
    source = item.get("source_url") or item["url"]
    key = source_id(item)
    width, height, duration, short = media
    preview = preview_only()
    generation = previous["generation"] + 1 if previous else 1
    button = hashlib.sha256(f"{key}:{digest}:{generation}".encode()).hexdigest()[:32]
    card = (
        ("پیشنهاد آزمایشی — انتشار غیرفعال" if preview else "پیشنهاد انتشار") + "\n"
        f"{title}\n{source}\n"
        f"منبع: {'وب‌سایت فوتبالی' if key.startswith('footballi:') else 'Instagram رسمی فوتبالی'}\n"
        f"تاریخ منبع: {item.get('source_published_at') or item.get('published_at') or 'نامشخص'}\n"
        f"{'شورت' if short else 'ویدیوی اصلی'} | {duration:.0f} ثانیه | {width}×{height}\n"
        f"دلیل پیشنهاد: {evidence}\n"
        "با تأیید، تولید فوتبالی و حق انتشار تصویر و صدای همین ویدیو در یوتیوب را گواهی می‌کنی."
    )
    return {"status": "waiting", "preview": preview, "button_id": button, "generation": generation,
            "sha256": digest, "evidence": evidence, "card": card}


def mark_published(item, video_id):
    item["status"] = "published"
    item["video_id"] = video_id
    set_notice(item, f"منتشر شد: https://www.youtube.com/watch?v={video_id}")
