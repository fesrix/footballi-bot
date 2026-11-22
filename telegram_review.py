"""Private Telegram approval cards for exact Footballi media files."""

import hashlib
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import requests

import originals_policy as originals


def preview_only():
    return os.getenv("FOOTBALLI_TELEGRAM_PREVIEW_ONLY", "").lower() == "true"


def review_state(state):
    return state.setdefault("telegram_review", {"version": 1, "offset": 0, "items": {}})


def source_id(candidate):
    return originals.source_key(candidate.get("source_url") or candidate.get("url"))


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
                            allowed_updates=["callback_query"])
        if not isinstance(updates, list) or not updates:
            return
        callbacks = []
        for update in sorted(updates, key=lambda value: value["update_id"]):
            callback = update.get("callback_query")
            if callback:
                callbacks.append((callback["id"], self.decide(review, callback)))
        review["offset"] = max(update["update_id"] for update in updates) + 1
        review["updated_at"] = time.time()
        persist()
        answer_until = time.monotonic() + 8
        for callback_id, result in callbacks:
            if time.monotonic() >= answer_until:
                break
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=result)

    def decide(self, review, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if (callback.get("from", {}).get("id") != self.owner or chat.get("id") != self.owner
                or chat.get("type") != "private"):
            return "این حساب اجازهٔ بررسی ندارد."
        match = re.fullmatch(r"([ar]):([a-f0-9]{32})", callback.get("data") or "")
        if not match:
            return "دکمه معتبر نیست."
        action, button_id = match.groups()
        item = next((item for item in review["items"].values() if item["button_id"] == button_id), None)
        if not item or item["preview"] != preview_only():
            return "این پیشنهاد منقضی شده است."
        if item["status"] != "waiting":
            return "تصمیم این پیشنهاد قبلاً ثبت شده است."
        if item.get("message_id") not in (None, message.get("message_id")):
            return "این پیام معتبر نیست."
        item["message_id"] = message["message_id"]
        item["status"] = "approved" if action == "a" else "rejected"
        item["decided_at"] = datetime.now(timezone.utc).isoformat()
        if action == "a":
            item["approval"] = {
                "sha256": item["sha256"],
                "production_evidence": (
                    "An authorized Footballi reviewer confirmed via the private approval card that "
                    "Footballi produced this exact source video and cleared its visual and audio rights "
                    "for YouTube. " + item["evidence"]
                ),
                "visual_rights_confirmed": True,
                "audio_rights_confirmed": True,
            }
        label = "رد شد." if action == "r" else (
            "تأیید آزمایشی ثبت شد. انتشار غیرفعال است." if item["preview"]
            else "تأیید ثبت شد. در انتظار بررسی نهایی فایل است."
        )
        set_notice(item, label)
        return label

    def send_card(self, item):
        button = item["button_id"]
        reply = self.call("sendMessage", chat_id=self.owner, text=item["card"],
                          reply_markup={"inline_keyboard": [[
                              {"text": "تأیید انتشار", "callback_data": f"a:{button}"},
                              {"text": "رد", "callback_data": f"r:{button}"},
                          ]]})
        if isinstance(reply, dict) and isinstance(reply.get("message_id"), int):
            item["message_id"] = reply["message_id"]
            return True
        return False

    def send_preview_probe(self, state, persist):
        """Exercise both directions when no real media passed the preview screening."""
        items = review_state(state)["items"]
        if not preview_only() or any(item["preview"] and item["status"] == "waiting"
                                     and item.get("message_id") for item in items.values()):
            return
        probe = items.get("telegram:connection")
        if probe is None:
            probe = {
                "status": "waiting", "preview": True, "generation": 1,
                "sha256": "", "evidence": "",
                "button_id": hashlib.sha256(os.urandom(32)).hexdigest()[:32],
                "card": "آزمون اتصال ربات فوتبالی\nبرای بررسی ثبت تصمیم، یکی از دکمه‌ها را بزن. این پیام ویدیویی برای انتشار ندارد.",
            }
            items["telegram:connection"] = probe
            persist()
        if probe["status"] == "waiting" and not probe.get("message_id") and self.send_card(probe):
            print("[TELEGRAM] Preview connection card sent.")

    def flush_notices(self, state, deadline):
        for item in review_state(state)["items"].values():
            if time.monotonic() + 15 >= deadline:
                break
            if item.get("notice") and item.get("message_id"):
                result = self.call("editMessageText", chat_id=self.owner, message_id=item["message_id"],
                                   text=item["card"] + "\n\n" + item["notice"],
                                   reply_markup={"inline_keyboard": []})
                if result is not None:
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
