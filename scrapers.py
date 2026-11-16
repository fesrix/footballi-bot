# -*- coding: utf-8 -*-
import os
import re
import json
import time
import shutil
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from config import (
    PROTO,
    HEADERS,
    ENTRY_URLS,
    FOREIGN_BLACKLIST,
    IRANIAN_DOMESTIC_CLUBS,
    IRANIAN_SAFE_SHOWS,
    IRANIAN_FOOTBALL_ENTITIES,
    APIFY_COOLDOWN_HOURS,
)

MAX_VIDEO_ID_AGE_GAP = 650
MIN_ABSOLUTE_VIDEO_ID = 1073500


def normalize_persian(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = text.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        text = text.replace(persian_digits[i], str(i)).replace(arabic_digits[i], str(i))
    return re.sub(r"\s+", " ", text).strip()


def extract_numeric_id(url: str) -> int:
    m = re.search(r"/video/(\d+)", str(url))
    return int(m.group(1)) if m else 0


def is_video_too_old(title: str, page_text_sample: str = "", video_id: int = 0, max_site_id: int = 0) -> bool:
    if video_id > 0:
        if video_id < MIN_ABSOLUTE_VIDEO_ID:
            return True
        if max_site_id > MIN_ABSOLUTE_VIDEO_ID and (max_site_id - video_id) > MAX_VIDEO_ID_AGE_GAP:
            return True

    norm_t = normalize_persian(title).lower()
    norm_page = normalize_persian(page_text_sample).lower()

    old_years = ["1398", "1399", "1400", "1401", "1402", "1403", "1404", "2020", "2021", "2022", "2023", "2024", "2025"]
    for y in old_years:
        if y in norm_t:
            return True

    if any(old_unit in norm_page for old_unit in ["هفته پیش", "ماه پیش", "سال پیش"]):
        return True

    m_days = re.search(r"(\d+)\s*روز پیش", norm_page)
    if m_days and int(m_days.group(1)) > 2:
        return True

    return False


def is_match_broadcast_footage(norm_title: str) -> bool:
    match_words = [
        "خلاصه بازی", "خلاصه دیدار", "خلاصه مسابقه", "هایلایت بازی", "بازی کامل",
        "گل اول", "گل دوم", "گل سوم", "گل چهارم", "گل پنجم", "گل های بازی",
        "گلهای بازی", "گل های برتر", "گلهای برتر", "پاس گل های برتر", "پاس گلهای برتر",
        "تمام گل های", "ضربات پنالتی بازی", "شبیه سازی گل",
    ]
    return any(normalize_persian(w).lower() in norm_title for w in match_words)


def is_foreign_or_blacklisted(title: str, desc: str = "") -> bool:
    """فیلتر پشتیبان و خط قرمز قطعی برای خلاصه بازی‌های خارجی و آسیایی"""
    clean_title = re.sub(r"\|.*$", "", title).strip()
    norm_title = normalize_persian(clean_title).lower()

    if is_video_too_old(clean_title):
        return True

    if is_match_broadcast_footage(norm_title):
        for bad_word in FOREIGN_BLACKLIST:
            if normalize_persian(bad_word).lower() in norm_title:
                return True
        has_iranian_club = any(
            normalize_persian(club).lower() in norm_title
            for club in IRANIAN_DOMESTIC_CLUBS + ["لیگ برتر ایران", "لیگ آزادگان", "جام حذفی", "فوتبال ایران"]
        )
        return not has_iranian_club

    for bad_word in FOREIGN_BLACKLIST:
        if normalize_persian(bad_word).lower() in norm_title:
            has_iranian_ref = any(
                normalize_persian(ent).lower() in norm_title
                for ent in IRANIAN_FOOTBALL_ENTITIES + ["لیگ برتر", "تیم ملی", "فوتبال ایران"]
            )
            if not has_iranian_ref:
                return True

    return False


def is_strictly_domestic_short(title: str, desc: str = "") -> bool:
    """بررسی سریع برای شورتس‌های اینستاگرام"""
    norm_full = normalize_persian(f"{title} {desc}").lower()
    if is_match_broadcast_footage(norm_full):
        for bad_word in FOREIGN_BLACKLIST:
            if normalize_persian(bad_word).lower() in norm_full:
                return False
    return not is_foreign_or_blacklisted(title, desc)


def ai_judge_videos(candidates: list) -> list:
    """
    قضاوت دسته‌جمعی تمام عنوان‌ها توسط هوش مصنوعی (بدون وابستگی به لیست‌های ثابت و ایستا):
    کل لیست در ۱ درخواست ارسال می‌شود تا با هزینه نزدیک به صفر، مصاحبه‌ها و خلاصه‌بازی‌های ایران تایید
    و کلیپ‌های خارجی، اروپایی، آسیایی (AFC) و گیمینگ به دقت رد شوند.
    """
    if not candidates:
        return []

    safe_candidates = []
    for c in candidates:
        norm_t = normalize_persian(c["title"]).lower()
        # خط قرمز سخت‌افزاری: خلاصه بازی/گل خارجی یا آسیایی حتی برای هوش مصنوعی هم فرستاده نمی‌شود
        if is_match_broadcast_footage(norm_t) and is_foreign_or_blacklisted(c["title"]):
            print(f"🚫 [SKIP-FOREIGN-MATCH] Blocked copyrighted match footage: {c['title']}")
            continue
        safe_candidates.append(c)

    if not safe_candidates:
        return []

    arvan_key = os.environ.get("ARVAN_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    payload = [{"index": i, "title": c["title"]} for i, c in enumerate(safe_candidates)]
    prompt = f"""
You are the Head of Copyright & Content Curation for 'Footballi' (فوتبالی), an official Iranian domestic football YouTube channel.
Review each Persian video title and decide whether to APPROVE (true) or REJECT (false).

APPROVE (true) ALL of these:
1. Domestic Iranian football match highlights, goals, assists, or weekly compilations (لیگ برتر ایران, لیگ آزادگان, جام حذفی, استقلال, پرسپولیس, سپاهان, تراکتور, فولاد, ملوان, etc.).
2. ALL interviews, quotes, press conferences, and news about Iranian players, coaches, referees, club managers, veterans (پیشکسوتان), Iranian legionnaires abroad (like علی قلی‌زاده, طارمی, آزمون, صیادمنش), or Iranian stadiums (like ورزشگاه آزادی).
   - IMPORTANT: Approve Iranian interviews even if they casually mention a foreign club or player as an example or comparison (e.g., "مثل دیبالا شماره 21 میپوشم" or talking about an Iranian player's European club).
3. All Footballi studio/talk shows (ویدیوکست, پُشتِ خَط, تُپُلیگ, فوتبال برتر, شب‌های فوتبالی, کارشناسی داوری).

REJECT (false) ALL of these:
1. Any foreign football match highlights, goals, skills, or compilations (European leagues, La Liga, Serie A, Premier League, Bundesliga, MLS, Arab leagues, Champions League, international matches like Spain vs England, Brazil vs Japan, Haiti vs USA, etc.).
2. Any AFC Champions League, Asian Cup, or FIFA World Cup match broadcast footage (even if Iran's national team or an Iranian club is playing in it, e.g. خلاصه بازی ایران و عراق, ایران و فلسطین, نوجوانان ایران و سریلانکا).
3. Video games (FC27, FIFA, PES, GTA), generic viral foreign football clips, or foreign football newspaper reviews.

Return ONLY valid JSON mapping each index string ("0", "1", ...) to {{"approve": true/false, "reason": "short reason"}}.

Videos:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    decisions = {}

    # ۱. تلاش با جمینای رایگان گوگل (هزینه صفر)
    if gemini_key:
        try:
            g_url = f"{PROTO}generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            resp = requests.post(
                g_url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=25,
            )
            if resp.status_code == 200:
                txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{.*\}", txt, re.DOTALL)
                if m:
                    decisions = json.loads(m.group(0))
                    print(f"[AI-CURATOR] Evaluated {len(safe_candidates)} candidates via Free Gemini API.")
        except Exception:
            pass

    # ۲. فال‌بک به مدل فوق‌ارزان Arvan
    if not decisions and arvan_key:
        for model_name in ["Gemini-2.5-Flash-lite", "GPT-4.1-Nano"]:
            try:
                r = requests.post(
                    f"{PROTO}ai.najafivip.ir/v1/chat/completions",
                    headers={"Authorization": f"Bearer {arvan_key}", "Content-Type": "application/json"},
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    },
                    timeout=25,
                )
                if r.status_code == 200:
                    txt = r.json()["choices"][0]["message"]["content"]
                    m = re.search(r"\{.*\}", txt, re.DOTALL)
                    if m:
                        decisions = json.loads(m.group(0))
                        print(f"[AI-CURATOR] Evaluated {len(safe_candidates)} candidates via {model_name}.")
                        break
            except Exception:
                continue

    approved = []
    for i, c in enumerate(safe_candidates):
        dec = decisions.get(str(i))
        if isinstance(dec, dict):
            is_ok = bool(dec.get("approve", False))
            reason = dec.get("reason", "")
            if is_ok:
                print(f"✅ [AI-APPROVED] {c['title']} ({reason})")
                approved.append(c)
            else:
                print(f"🚫 [AI-REJECTED] {c['title']} ({reason})")
        elif isinstance(dec, bool):
            if dec:
                print(f"✅ [AI-APPROVED] {c['title']}")
                approved.append(c)
            else:
                print(f"🚫 [AI-REJECTED] {c['title']}")
        else:
            # فال‌بک در صورت خطای شبکه هوش مصنوعی
            if not is_foreign_or_blacklisted(c["title"], c.get("description", "")):
                approved.append(c)
            else:
                print(f"🚫 [FALLBACK-SKIP] {c['title']}")

    return approved


def find_system_chrome() -> str:
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium-browser"),
        shutil.which("chromium"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def browser_discover_links(max_pages: int = 6) -> list:
    discovered_urls = []
    seen = set()
    chrome_path = find_system_chrome()

    if not chrome_path:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    print(f"[AI-BROWSER] Launching pre-installed System Chrome ({chrome_path}) to render footballi.net...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chrome_path,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 900},
                locale="fa-IR",
            )
            page = context.new_page()

            def handle_response(response):
                try:
                    if "footballi.net" in response.url and "json" in response.headers.get("content-type", ""):
                        text = response.text()
                        for m in re.finditer(r"/video/(\d+)(?:/[^\s\"\'<>\\]*)?", text):
                            full_u = urljoin("https://footballi.net", m.group(0))
                            vid_id = m.group(1)
                            if vid_id not in seen:
                                seen.add(vid_id)
                                discovered_urls.append(full_u)
                except Exception:
                    pass

            page.on("response", handle_response)

            for url in ENTRY_URLS[:max_pages]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    for _ in range(2):
                        page.evaluate("window.scrollBy(0, 1000)")
                        page.wait_for_timeout(500)

                    anchors = page.eval_on_selector_all(
                        "a[href*='/video/']",
                        "elements => elements.map(el => ({href: el.href, text: (el.innerText || el.title || '').trim()}))",
                    )
                    for item in anchors:
                        href = item.get("href", "")
                        m = re.search(r"https?://(?:www\.)?footballi\.net/video/(\d+)(?:/[^\s\"\'<>?#]*)?", href)
                        if m:
                            vid_id = m.group(1)
                            if vid_id not in seen:
                                seen.add(vid_id)
                                discovered_urls.append(href.split("?")[0])
                except Exception as e:
                    print(f"[BROWSER-WARN] Timeout/skip on {url}: {e}")

            browser.close()
    except Exception as e:
        print(f"[BROWSER-WARN] Headless Chrome scan note: {e}")

    print(f"[AI-BROWSER] Browser rendered & discovered {len(discovered_urls)} unique video links.")
    return discovered_urls


def extract_video_page_details(page_url: str, max_site_id: int = 0, session: requests.Session = None) -> dict:
    video_id = extract_numeric_id(page_url)
    if video_id <= 0:
        return None

    if is_video_too_old("", "", video_id=video_id, max_site_id=max_site_id):
        return None

    req = session or requests
    try:
        resp = req.get(page_url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = re.sub(r"\s*[-|]\s*فوتبالی.*$", "", title).strip()

    for tag in soup.find_all(["aside", "footer", "nav"]):
        tag.decompose()
    main_text_sample = soup.get_text(" ", strip=True)[:600]
    if is_video_too_old(title, main_text_sample, video_id=video_id, max_site_id=max_site_id):
        return None

    poster_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        poster_url = og_img["content"].strip()

    description = ""
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        description = og_desc["content"].strip()

    mp4_url = ""
    for meta_prop in ["og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"]:
        m_tag = soup.find("meta", property=meta_prop) or soup.find("meta", attrs={"name": meta_prop})
        if m_tag and m_tag.get("content") and ".mp4" in m_tag["content"]:
            mp4_url = m_tag["content"].strip()
            break

    if not mp4_url:
        video_tag = soup.find("video")
        if video_tag:
            if video_tag.get("src") and ".mp4" in video_tag["src"]:
                mp4_url = video_tag["src"].strip()
            else:
                src_tag = video_tag.find("source")
                if src_tag and src_tag.get("src") and ".mp4" in src_tag["src"]:
                    mp4_url = src_tag["src"].strip()
            if not poster_url and video_tag.get("poster"):
                poster_url = video_tag["poster"].strip()

    if not mp4_url:
        idx = html.find(str(video_id))
        while idx != -1:
            window = html[max(0, idx - 1500) : min(len(html), idx + 2500)]
            mp4_matches = re.findall(r"https?://[^\s\"\'<>\\]+\.mp4", window)
            if mp4_matches:
                mp4_url = mp4_matches[0]
                break
            idx = html.find(str(video_id), idx + len(str(video_id)))

    if not mp4_url:
        all_mp4s = re.findall(r"https?://[^\s\"\'<>\\]+\.mp4", html)
        if all_mp4s:
            mp4_url = all_mp4s[0]

    if not mp4_url or not title:
        return None

    return {
        "video_id": video_id,
        "url": page_url,
        "title": title,
        "description": description or title,
        "mp4_url": mp4_url,
        "poster_url": poster_url,
    }


def crawl_footballi_videos(state: dict, max_new_candidates: int = 20) -> list:
    uploaded_urls = set(state.get("uploaded_urls", []))
    seen_mp4s = set(state.get("seen_mp4s", []))
    queued_urls = {item.get("url") for item in state.get("pending_queue", []) if item.get("url")}
    queued_mp4s = {item.get("mp4_url") for item in state.get("pending_queue", []) if item.get("mp4_url")}

    known_ids = set()
    for u in uploaded_urls | queued_urls:
        vid_id = extract_numeric_id(u)
        if vid_id:
            known_ids.add(vid_id)

    raw_discovered_links = []
    for link in browser_discover_links():
        raw_discovered_links.append(link)

    session = requests.Session()
    for hub_url in ENTRY_URLS:
        try:
            r = session.get(hub_url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                for m in re.finditer(r"https?://(?:www\.)?footballi\.net/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?", r.text):
                    raw_discovered_links.append(m.group(0))
                for m in re.finditer(r"\"/video/(\d+)(?:/[^\s\"\'<>?#\\]*)?\"", r.text):
                    raw_discovered_links.append(urljoin("https://footballi.net", m.group(0).strip('"')))
        except Exception:
            continue

    all_ids = [extract_numeric_id(u) for u in raw_discovered_links if extract_numeric_id(u) > 0]
    max_site_id = max(all_ids) if all_ids else MIN_ABSOLUTE_VIDEO_ID
    state["max_seen_video_id"] = max(int(state.get("max_seen_video_id", 0)), max_site_id)
    max_site_id = state["max_seen_video_id"]

    candidate_links = []
    seen_candidate_ids = set()

    for u in sorted(raw_discovered_links, key=extract_numeric_id, reverse=True):
        vid_id = extract_numeric_id(u)
        if vid_id <= 0 or vid_id in known_ids or vid_id in seen_candidate_ids:
            continue
        if is_video_too_old("", "", video_id=vid_id, max_site_id=max_site_id):
            continue
        seen_candidate_ids.add(vid_id)
        candidate_links.append(u)

    print(
        f"[CRAWLER] Latest site video ID: {max_site_id} | "
        f"Found {len(candidate_links)} fresh (<48h) candidate video pages to inspect."
    )

    raw_extracted = []
    for link in candidate_links[: max_new_candidates * 2]:
        details = extract_video_page_details(link, max_site_id=max_site_id, session=session)
        if not details:
            continue

        if details["mp4_url"] in seen_mp4s or details["mp4_url"] in queued_mp4s:
            continue

        raw_extracted.append({
            "video_id": details["video_id"],
            "url": details["url"],
            "title": details["title"],
            "description": details["description"],
            "mp4_url": details["mp4_url"],
            "poster_url": details["poster_url"],
            "source": "footballi_web",
            "is_short": False,
        })

    # ارسال کل ویدیوهای استخراج‌شده برای تصمیم‌گیری هوشمند به AI
    verified = ai_judge_videos(raw_extracted)
    results = []
    for item in verified[:max_new_candidates]:
        seen_mp4s.add(item["mp4_url"])
        results.append(item)

    print(f"[CRAWLER] AI Curator approved {len(results)} brand-new (<48h) domestic Iranian videos!")
    return results


def fetch_instagram_reels(state: dict) -> list:
    """
    دریافت ریلزهای باکیفیت HD از پیج اینستاگرام @footballiapp با Apify و قضاوت هوش مصنوعی
    """
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()
    if not apify_token:
        print("[APIFY] APIFY_TOKEN not set; skipping Instagram Reels check.")
        return []

    last_check = float(state.get("last_apify_check", 0))
    elapsed_hours = (time.time() - last_check) / 3600.0
    if elapsed_hours < APIFY_COOLDOWN_HOURS:
        rem = APIFY_COOLDOWN_HOURS - elapsed_hours
        print(f"[APIFY] Cooldown active ({rem:.1f}h remaining before next Instagram check).")
        return []

    try:
        from apify_client import ApifyClient
        client = ApifyClient(apify_token)
        run_input = {
            "username": ["footballiapp"],
            "resultsLimit": 6,
        }
        print("[APIFY] Fetching latest HD Reels from @footballiapp Instagram...")
        run = client.actor("apify/instagram-reel-scraper").call(run_input=run_input)
        state["last_apify_check"] = time.time()

        uploaded_urls = set(state.get("uploaded_urls", []))
        seen_mp4s = set(state.get("seen_mp4s", []))
        queued_urls = {item.get("url") for item in state.get("pending_queue", []) if item.get("url")}

        raw_reels = []
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            reel_url = item.get("url") or f"https://www.instagram.com/p/{item.get('shortCode')}/"
            video_url = item.get("videoUrl")
            caption = (item.get("caption") or "").strip()
            if not video_url or reel_url in uploaded_urls or reel_url in queued_urls or video_url in seen_mp4s:
                continue

            clean_lines = [
                re.sub(r"^[^\w\u0600-\u06FF]+", "", ln).strip()
                for ln in caption.split("\n")
                if ln.strip() and len(re.findall(r"[\u0600-\u06FFa-zA-Z]", ln)) >= 4
            ]
            first_line = clean_lines[0][:95] if clean_lines else "ویدیوی کوتاه فوتبالی"

            raw_reels.append({
                "video_id": 0,
                "url": reel_url,
                "title": first_line,
                "description": caption,
                "mp4_url": video_url,
                "poster_url": item.get("displayUrl", ""),
                "source": "instagram",
                "is_short": True,
            })

        verified_reels = ai_judge_videos(raw_reels)
        print(f"[APIFY] AI Curator approved {len(verified_reels)} fresh domestic HD Reels from Instagram!")
        return verified_reels
    except Exception as e:
        print(f"[APIFY-WARN] Instagram check skipped: {e}")
        return []
