"""
benosi.py — X News Bot (পোস্টিং/যাচাই স্তর রিডিজাইন করা)

মূল পরিবর্তন:
  ১. পোস্ট "সফল" ধরা হয় শুধু X-এর CreateTweet উত্তরে tweet id এলে (ক্লিক হলেই আর না)।
  ২. কম্পোজার সবসময় #layers (মোডাল) বা পেজ — যেটা আছে — তার ভেতর থেকে ধরা হয়;
     পেছনের লুকানো বক্সে ক্লিক করে mask-এ আটকে যাওয়ার সমস্যা শেষ।
  ৩. ক্লিক আটকালে ৩০ সেকেন্ড অপেক্ষা না করে ৮ সেকেন্ডে ধরে, কে আটকাচ্ছে সেটা লগ + স্ক্রিনশট করে,
     তারপর focus / Ctrl+Enter দিয়ে এগোয়।
  ৪. স্ক্যানের সময়ই টুইটের status URL, লাইক/ভিউ (aria-label থেকে সঠিক সংখ্যা), ছবির লিংক,
     ভিডিও আছে কিনা — সব সংগ্রহ হয়। পরে টাইমলাইন রিলোড করে টেক্সট মেলানোর দরকার নেই।
  ৫. ছবি/ভিডিও সরাসরি file input-এ (মেনু ছাড়া)।
  ৬. ক্যাপশন থেকে অদৃশ্য ক্যারেক্টার সরানো হয়, বক্সে কী লেখা হলো তা পড়ে মিলিয়ে লগ করা হয়।
  ৭. পরপর কয়েকবার পোস্ট ব্যর্থ হলে বা সেশন মরলে রান exit code 1-এ ফেল করে (GitHub-এ লাল দেখাবে)।
  ৮. শুরুতেই "কোন অ্যাকাউন্টে লগইন" লগ হয়; EXPECTED_HANDLE env দিলে না মিললে থেমে যায়।
"""
import time
import random
import re
import os
import glob
import hashlib
import requests
import subprocess
import json
import sys
import traceback
from datetime import datetime, timezone
import pytz
from playwright.sync_api import sync_playwright
import g4f

BD_TZ = pytz.timezone("Asia/Dhaka")

# ──────────────────────────────────────────────
# SOURCES (from env SOURCES)
# ──────────────────────────────────────────────
SOURCES_STR = os.environ.get("SOURCES")
if not SOURCES_STR:
    print("❌ SOURCES environment variable not set. Bot cannot run.")
    sys.exit(1)
SOURCES = [s.strip() for s in SOURCES_STR.split(",") if s.strip()]
print(f"✅ Loaded {len(SOURCES)} sources from environment.")

PROMO_KEYWORDS = [
    "subscribe ", "follow me ", "join my ", "telegram ", "substack ",
    "newsletter ", "patreon ", "buy now ", "link in bio ", "check out my ",
    "my channel ", "dm me ", "sign up ", "free trial ", "discount ",
    "coupon ", "affiliate ", "sponsored ", "ad: ", "promotion ",
]

# ── কেবল জিওপলিটিক্যাল কনটেন্টে অপ্রাসঙ্গিক কয়েকটি স্পোর্টস-টপিক ব্লক করা হচ্ছে
FORBIDDEN_KEYWORDS = [
    "ki", "ki kinping", "kinking", "kaikan", "india"
]

MEDIA_DIR = "downloaded_media"
POSTED_CACHE = "posted_cache.txt"
REPLIED_CACHE = "replied_cache.txt"
CAPTCHA_LOCK_FILE = "captcha_lock.txt"
DAILY_LIMIT_FILE = "daily_post_limit.json"
TOPIC_MEMORY_FILE = "topic_memory.json"
SESSION_CACHE_FILE = "session_state.json"   # Playwright নেটিভ storage_state ক্যাশ
os.makedirs(MEDIA_DIR, exist_ok=True)

# এই রানে যেসব টুইট পোস্ট করতে গিয়ে ব্যর্থ হয়েছে — একই রানে বারবার সেগুলোই বাছা বন্ধ
RUN_SKIP = set()


def _short(e, n=160):
    """এক্সেপশনের প্রথম লাইনটুকু — লগ ছোট রাখতে।"""
    lines = str(e).strip().splitlines()
    return (lines[0] if lines else e.__class__.__name__)[:n]


# ──────────────────────────────────────────────
# DAILY POST LIMIT (18–22 posts per day — অফিস-আওয়ার স্ট্র্যাটেজি)
# ──────────────────────────────────────────────
def get_daily_limit():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(DAILY_LIMIT_FILE):
        try:
            with open(DAILY_LIMIT_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data["target"], data["count"]
        except Exception:
            pass
    target = random.randint(18, 22)
    data = {"date": today_str, "target": target, "count": 0}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📊 New daily post target: {target}")
    return target, 0


def increment_daily_counter():
    target, count = get_daily_limit()
    count += 1
    data = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "target": target, "count": count}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📈 Daily count: {count}/{target}")
    return count >= target


# ──────────────────────────────────────────────
# SESSION MANAGEMENT (cache-first, Secret fallback)
# ──────────────────────────────────────────────
def _fix_samesite(v):
    if not v:
        return None
    return {"strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None"}.get(str(v).lower())


def normalize_cookies(raw):
    """Cookie-Editor এক্সপোর্ট (খালি list) আর storage_state (dict) — দুটোই
    Playwright-সেফ কুকি লিস্টে কনভার্ট করে।"""
    cookies = raw.get("cookies", []) if isinstance(raw, dict) else raw
    out = []
    for c in cookies:
        try:
            nc = {"name": c["name"], "value": c["value"],
                  "domain": c["domain"], "path": c.get("path", "/")}
            exp = c.get("expires", c.get("expirationDate"))
            if exp not in (None, -1) and not c.get("session", False):
                nc["expires"] = int(exp)
            nc["httpOnly"] = bool(c.get("httpOnly", False))
            nc["secure"] = bool(c.get("secure", False))
            ss = _fix_samesite(c.get("sameSite"))
            if ss:
                nc["sameSite"] = ss
            out.append(nc)
        except Exception:
            continue
    return out


def load_session():
    # ১. আগে ক্যাশড session_state.json (আগের রানের আপডেটেড state) — নেটিভ ফরম্যাট,
    #    normalize লাগে না। ২. না থাকলে Secret/session.json normalize করে।
    if os.path.exists(SESSION_CACHE_FILE):
        try:
            with open(SESSION_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("cookies"):
                print(f"✅ ক্যাশড সেশন ব্যবহার হচ্ছে: {len(cached['cookies'])} cookies")
                return cached
        except Exception as e:
            print(f"⚠️ ক্যাশড সেশন পড়তে সমস্যা, Secret-এ fallback: {e}")
    session_json_str = os.environ.get("SESSION_JSON")
    if session_json_str:
        try:
            data = json.loads(session_json_str)
            cookies = normalize_cookies(data)
            if cookies:
                print(f"✅ SESSION_JSON normalized. cookies: {len(cookies)}")
                return {"cookies": cookies, "origins": []}
        except Exception as e:
            print(f"❌ SESSION_JSON parse error: {e}")
    if os.path.exists("session.json"):
        try:
            with open("session.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = normalize_cookies(data)
            if cookies:
                print(f"✅ session.json normalized. cookies: {len(cookies)}")
                return {"cookies": cookies, "origins": []}
        except Exception as e:
            print(f"❌ session.json error: {e}")
    return None


def load_deepseek_session():
    session_json_str = os.environ.get("DEEPSEEK_SESSION_JSON")
    if session_json_str:
        try:
            data = json.loads(session_json_str)
            if "cookies" in data:
                print(f"✅ DEEPSEEK_SESSION_JSON loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ DEEPSEEK_SESSION_JSON parse error: {e}")
    if os.path.exists("deepseek_session.json"):
        try:
            with open("deepseek_session.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            if "cookies" in data:
                print(f"✅ deepseek_session.json loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ deepseek_session.json error: {e}")
    print("⚠️ No DeepSeek session found (DEEPSEEK_SESSION_JSON not set). DeepSeek rewrite may fail as logged-out.")
    return None


def validate_session():
    if os.path.exists(SESSION_CACHE_FILE):
        return True
    session = load_session()
    if session is None:
        print("❌ No session found (set SESSION_JSON or provide session.json). Bot stopped.")
        return False
    return True


def save_session_cache(context, label=""):
    try:
        context.storage_state(path=SESSION_CACHE_FILE)
        print(f"💾 সেশন ক্যাশ সেভ হলো{(' — ' + label) if label else ''}")
    except Exception as e:
        print(f"⚠️ সেশন ক্যাশ সেভ ব্যর্থ: {e}")


def clear_session_cache():
    if os.path.exists(SESSION_CACHE_FILE):
        os.remove(SESSION_CACHE_FILE)
        print("🗑️ পুরনো সেশন ক্যাশ মুছে ফেলা হলো — পরের রানে fresh Secret ব্যবহার হবে")


# ──────────────────────────────────────────────
# CAPTCHA LOCK (with screenshot) + SESSION-DEAD
# ──────────────────────────────────────────────
def is_captcha_locked():
    if not os.path.exists(CAPTCHA_LOCK_FILE):
        return False
    with open(CAPTCHA_LOCK_FILE, "r") as f:
        lock_time = float(f.read().strip())
    elapsed = time.time() - lock_time
    remaining = (12 * 3600) - elapsed
    if remaining > 0:
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        print(f"🔒 Captcha lock active. {hours}h {mins}m remaining.")
        return True
    os.remove(CAPTCHA_LOCK_FILE)
    print("✅ Captcha lock ended.")
    return False


def set_captcha_lock():
    with open(CAPTCHA_LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    print("🔒 Captcha lock set for 12h.")


def check_captcha(page):
    try:
        captcha = page.query_selector(
            'iframe[src*="captcha"], '
            'div[data-testid="captcha"], '
            '#captcha, '
            'iframe[title*="captcha"]'
        )
        if captcha and captcha.is_visible():
            print("  ⚠️ CAPTCHA element detected!")
            page.screenshot(path=f"captcha_debug_elem_{int(time.time())}.png")
            set_captcha_lock()
            return True
    except Exception:
        pass
    try:
        page_text = page.inner_text('body').lower()
        if any(phrase in page_text for phrase in [
            "verify your identity", "are you human", "unusual activity",
            "prove you're not a bot", "security challenge", "complete the challenge"
        ]):
            current_url = page.url.lower()
            if "challenge" in current_url or "captcha" in current_url or "suspended" in current_url:
                print("  ⚠️ Challenge text + suspicious URL detected!")
                page.screenshot(path=f"captcha_debug_text_{int(time.time())}.png")
                set_captcha_lock()
                return True
    except Exception:
        pass
    current_url = page.url.lower()
    if "challenge" in current_url or "captcha" in current_url:
        print("  ⚠️ Challenge/Captcha URL detected!")
        page.screenshot(path=f"captcha_debug_url_{int(time.time())}.png")
        set_captcha_lock()
        return True
    return False


def check_session_dead(page):
    """সেশন মরেছে কিনা — (ক) URL লগইনে redirect হলে, অথবা (খ) সাইডবার নেই কিন্তু লগইন-লিংক আছে
    (guest layout)। দুটোর কোনোটাই অস্পষ্ট অবস্থায় 'মরেছে' ধরে না — যাতে ভুল ফেলস-অ্যালার্ম না হয়।"""
    url = page.url.lower()
    if "/login" in url or "/i/flow/login" in url:
        print("❌ Session dead — login page এ redirect.")
        page.screenshot(path=f"captcha_debug_{int(time.time())}.png")
        return True
    try:
        has_nav = page.locator(
            '[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="SideNav_NewTweet_Button"]'
        ).count() > 0
        has_login_wall = page.locator('a[href="/login"], [data-testid="loginButton"]').count() > 0
        if (not has_nav) and has_login_wall:
            print("❌ Session dead — সাইডবার নেই, লগইন-ওয়াল দেখা যাচ্ছে (guest layout)।")
            page.screenshot(path=f"captcha_debug_guest_{int(time.time())}.png")
            return True
    except Exception:
        pass
    return False


def verify_login_and_identity(page, retries=2):
    """শুরুতেই যাচাই: আসলে লগইন আছে কিনা + কোন @handle-এ। EXPECTED_HANDLE env থাকলে মিলিয়ে দেখে।"""
    expected = os.environ.get("EXPECTED_HANDLE", "").strip().lstrip("@").lower()
    for attempt in range(1, retries + 2):
        try:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector('[data-testid="SideNav_AccountSwitcher_Button"]', timeout=25000)
            txt = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first.inner_text(timeout=5000)
            m = re.search(r'@(\w+)', txt or "")
            handle = m.group(1) if m else None
            print(f"👤 লগইন করা অ্যাকাউন্ট: @{handle if handle else '(handle পড়া যায়নি)'}")
            if expected and handle and handle.lower() != expected:
                print(f"❌ প্রত্যাশিত @{expected}, কিন্তু লগইন আছে @{handle} — ভুল অ্যাকাউন্টে পোস্ট এড়াতে থামছি।")
                return False
            if expected and not handle:
                print("⚠️ handle পড়া যায়নি, তাই EXPECTED_HANDLE মেলানো গেল না — চালিয়ে যাচ্ছি।")
            return True
        except Exception as e:
            print(f"⚠️ লগইন যাচাই ব্যর্থ (চেষ্টা {attempt}): {_short(e)}")
            if check_session_dead(page):
                break
            time.sleep(5)
    try:
        page.screenshot(path=f"captcha_debug_login_{int(time.time())}.png")
    except Exception:
        pass
    print("❌ লগইন নিশ্চিত করা যায়নি (সেশন মৃত/কুকি ভুল/ব্লক) — পোস্ট না করে থামছি।")
    return False


# ──────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────
def text_hash(text):
    t = text.lower().strip()
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'@\w+', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()[:250]
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def load_cache(filepath):
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_to_cache(text, filepath):
    h = text_hash(text)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(h + "\n")


def is_duplicate(text, cache):
    return text_hash(text) in cache


def trim_cache(filepath, limit=500):
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > limit:
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])


# ──────────────────────────────────────────────
# FILTERS
# ──────────────────────────────────────────────
def is_promotional(text):
    return any(kw in text.lower() for kw in PROMO_KEYWORDS)


def is_too_short(text, min_chars=40):
    return len(text.strip()) < min_chars


def is_forbidden_topic(text):
    """FORBIDDEN_KEYWORDS-এ উল্লেখিত শব্দগুলো থাকলে টুইট পোস্ট হবে না।
    ছোট শব্দ (≤৩ অক্ষর, যেমন "ki") শুধু পূর্ণ শব্দ হিসেবে মেলে — আগে সাবস্ট্রিং মেলাত বলে
    "BREAKING", "killed", "Pakistan", "taking" সহ বহু দরকারি টুইট ভুলে বাদ পড়ত।
    লম্বা শব্দ (india, kaikan...) আগের মতোই সাবস্ট্রিং।"""
    low = text.lower()
    for kw in FORBIDDEN_KEYWORDS:
        if len(kw) <= 3:
            if re.search(r'(?<!\w)' + re.escape(kw) + r'(?!\w)', low):
                return True
        elif kw in low:
            return True
    return False


def is_pinned_tweet(tweet_element):
    try:
        social_context = tweet_element.query_selector('[data-testid="socialContext"]')
        if social_context and "pinned" in social_context.inner_text().lower():
            return True
        outer = tweet_element.inner_html().lower()
        if "pinned" in outer and "pinnedtweet" in outer.replace(" ", ""):
            return True
    except Exception:
        pass
    return False


def is_thread_continuation(tweet_element):
    try:
        outer = tweet_element.inner_html()
        if 'data-testid="tweet_reply_context"' in outer:
            return True
        if tweet_element.query_selector('[data-testid="tweet_reply_context"]'):
            return True
    except Exception:
        pass
    return False


def is_retweet(tweet_element):
    try:
        ctx = tweet_element.query_selector('[data-testid="socialContext"]')
        if ctx:
            t = ctx.inner_text().lower()
            if "retweet" in t or "retweeted" in t or "reposted" in t:
                return True
    except Exception:
        pass
    return False


def get_tweet_age_minutes(tweet_element):
    try:
        time_el = tweet_element.query_selector('time')
        if time_el:
            dt_str = time_el.get_attribute("datetime")
            if dt_str:
                tweet_time = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return int((now - tweet_time).total_seconds() / 60)
    except Exception:
        pass
    return 9999


# ──────────────────────────────────────────────
# টুইট থেকে তথ্য সংগ্রহ (স্ক্যানের সময়ই সব নেওয়া হয় — পরে রিলোড/টেক্সট-ম্যাচ লাগে না)
# ──────────────────────────────────────────────
def parse_count(text):
    if not text:
        return 0
    text = text.strip().upper().replace(",", "")
    try:
        if "K" in text:
            return int(float(text.replace("K", "")) * 1000)
        elif "M" in text:
            return int(float(text.replace("M", "")) * 1_000_000)
        return int(text)
    except Exception:
        return 0


def get_tweet_view_count(tweet):
    """পুরনো আন্দাজি পদ্ধতি — শুধু aria-label না পেলে ফলব্যাক।"""
    try:
        view_btn = tweet.query_selector('a[href*="/analytics"], [data-testid="analyticsButton"]')
        if view_btn:
            label = view_btn.get_attribute("aria-label") or view_btn.inner_text()
            return parse_count(label)
        stats = tweet.query_selector_all('div[data-testid$="count"]')
        return sum(parse_count(s.inner_text()) for s in stats) * 50
    except Exception:
        return 0


def get_tweet_stats(tweet):
    """অ্যাকশন-বারের aria-label থেকে সঠিক সংখ্যা, যেমন:
    '264 replies, 1795 reposts, 8210 likes, 439 bookmarks, 277427 views'"""
    stats = {"likes": 0, "views": 0, "reposts": 0, "replies": 0}
    label = ""
    try:
        label = tweet.evaluate(
            r"""el => { const g = el.querySelector('[role="group"][aria-label]');
                        return g ? (g.getAttribute('aria-label') || '') : ''; }"""
        ) or ""
    except Exception:
        label = ""
    patterns = (
        ("likes", r'([\d,\.]+)\s+likes?'),
        ("views", r'([\d,\.]+)\s+views?'),
        ("reposts", r'([\d,\.]+)\s+reposts?'),
        ("replies", r'([\d,\.]+)\s+repl(?:y|ies)'),
    )
    found = False
    for key, pat in patterns:
        m = re.search(pat, label, re.I)
        if m:
            try:
                stats[key] = int(m.group(1).replace(",", "").split(".")[0])
                found = True
            except Exception:
                pass
    if not found:   # aria-label না মিললে পুরনো পদ্ধতি
        try:
            like_btn = tweet.query_selector('button[data-testid="like"]')
            stats["likes"] = parse_count(like_btn.inner_text()) if like_btn else 0
        except Exception:
            pass
        stats["views"] = get_tweet_view_count(tweet)
    return stats


def get_tweet_status_url(tweet):
    try:
        url = tweet.evaluate(
            r"""el => { const t = el.querySelector('time');
                        const a = t ? t.closest('a') : null;
                        return a ? a.href : null; }"""
        )
        if url and "/status/" in url:
            return url
    except Exception:
        pass
    return None


def get_tweet_media_info(tweet):
    """(ছবির URL তালিকা, ভিডিও আছে কিনা)"""
    try:
        info = tweet.evaluate(
            r"""el => ({
                imgs: Array.from(el.querySelectorAll('img[src*="pbs.twimg.com/media"]')).map(i => i.src),
                video: !!(el.querySelector('video') || el.querySelector('[data-testid="videoPlayer"]'))
            })"""
        )
        imgs = list(dict.fromkeys(info.get("imgs") or []))
        return imgs, bool(info.get("video"))
    except Exception:
        return [], False


# ──────────────────────────────────────────────
# SCORING (fallback)
# ──────────────────────────────────────────────
def has_news_keywords(text):
    keywords = [
        "breaking", "war", "attack", "missile", "troops", "nato",
        "russia", "china", "ukraine", "iran", "israel", "gaza",
        "brics", "sanctions", "military", "killed", "turkey", "ceasefire",
        "explosion", "strike", "conflict", "coup", "election",
        "president", "minister", "nuclear", "uae", "saudi", "drone", "border",
        "crisis", "emergency", "peace", "deal", "treaty",
    ]
    return any(kw in text.lower() for kw in keywords)


def score_tweet(text, likes, views=0, age_minutes=9999):
    score = 0.0
    score += min(likes, 50000) * 0.001
    score += min(views, 500000) * 0.0001
    if has_news_keywords(text):
        score += 20
    if "breaking" in text.lower():
        score += 10
    if age_minutes <= 30:
        score += 25
    elif age_minutes <= 60:
        score += 15
    elif age_minutes <= 120:
        score += 5
    elif age_minutes > 240:          # 4-hour cutoff
        score -= 20
    if is_promotional(text):
        score -= 100
    if is_too_short(text):
        score -= 50
    return score


# ──────────────────────────────────────────────
# TOPIC MEMORY (short-term duplicate topic prevention)
# ──────────────────────────────────────────────
STOPWORDS = {
    "the", "is", "at", "which", "on", "a", "an", "and", "or", "but",
    "in", "with", "to", "for", "of", "by", "from", "as", "into",
    "about", "like", "after", "before", "between", "under", "over",
    "out", "up", "down", "off", "no", "not", "its", "it's", "that",
    "this", "was", "are", "were", "been", "be", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may",
    "can", "said", "says", "new", "just", "now", "very", "also",
    "more", "some", "breaking", "iran", "us", "usa", "u.s.", "back",
    "still", "already", "yet", "all", "both", "each", "every", "other",
    "many", "much", "such", "only", "then", "than", "too", "here",
    "there", "when", "where", "why", "how"
}


def extract_keywords(text):
    words = re.findall(r'[a-zA-Z]+', text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def load_topic_memory():
    if not os.path.exists(TOPIC_MEMORY_FILE):
        return []
    try:
        with open(TOPIC_MEMORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_topic_memory(memory):
    cutoff = time.time() - 6 * 3600   # ৬ ঘণ্টা – পুরো রান জুড়ে একই টপিক ব্লক
    memory = [m for m in memory if m["time"] > cutoff]
    with open(TOPIC_MEMORY_FILE, "w") as f:
        json.dump(memory, f)


def is_similar_topic(text, memory, min_overlap=3):
    keywords = extract_keywords(text)
    for mem in memory:
        if len(keywords & set(mem["keywords"])) >= min_overlap:
            return True
    return False


def add_to_topic_memory(text):
    memory = load_topic_memory()
    memory.append({"time": time.time(), "keywords": list(extract_keywords(text))})
    save_topic_memory(memory)


# ──────────────────────────────────────────────
# MEDIA (স্ক্যানে পাওয়া তথ্য থেকে সরাসরি ডাউনলোড — টাইমলাইন রিলোড লাগে না)
# ──────────────────────────────────────────────
def download_media(url, filename):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 200:
            path = os.path.join(MEDIA_DIR, filename)
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception as e:
        print(f"  ❌ Image download failed: {e}")
    return None


def download_images(urls, limit=4):
    paths = []
    for i, src in enumerate(urls[:limit]):
        src = re.sub(r'name=\w+', 'name=large', src)
        path = download_media(src, f"img_{int(time.time())}_{i}.jpg")
        if path:
            paths.append(path)
            print(f"  📥 Image {i + 1} downloaded.")
    return paths


def download_videos_from_tweet(tweet_url, max_attempts=3):
    if not tweet_url:
        return []
    base_name = os.path.join(MEDIA_DIR, f"video_{int(time.time())}")
    for attempt in range(1, max_attempts + 1):
        try:
            cmd = [
                "yt-dlp",
                "--format", "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--output", base_name + " %(playlist_index)s.%(ext)s",
                "--quiet", "--no-warnings",
                "--socket-timeout", "30",
                tweet_url,
            ]
            result = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
            if result.returncode == 0:
                video_files = sorted(glob.glob(base_name + "*.mp4"))
                valid_files = []
                too_large = False
                for fname in video_files:
                    size = os.path.getsize(fname)
                    if size == 0:
                        os.remove(fname)
                        continue
                    if size > 100 * 1024 * 1024:
                        print(f"  ⚠️ {os.path.basename(fname)} too large (100MB+), skip tweet.")
                        too_large = True
                        continue
                    valid_files.append(fname)
                if too_large:
                    for vf in valid_files:
                        try:
                            os.remove(vf)
                        except Exception:
                            pass
                    return "TOO_LARGE"
                if valid_files:
                    print(f"  📥 Downloaded {len(valid_files)} video(s): {[os.path.basename(v) for v in valid_files]}")
                    return valid_files
                print(f"  ❌ yt-dlp ran but no video files found (attempt {attempt}/{max_attempts})")
            else:
                print(f"  ❌ yt-dlp failed (attempt {attempt}/{max_attempts}): {result.stderr[:200]}")
                if attempt < max_attempts:
                    time.sleep(random.uniform(4, 8))
        except subprocess.TimeoutExpired:
            print(f"  ❌ yt-dlp timeout (attempt {attempt}/{max_attempts}).")
            if attempt < max_attempts:
                time.sleep(random.uniform(4, 8))
        except FileNotFoundError:
            print("  ❌ yt-dlp not installed.")
            return []
        except Exception as e:
            print(f"  ❌ Video download error (attempt {attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                time.sleep(random.uniform(4, 8))
    return []


def prepare_media(cand):
    """নির্বাচিত টুইটের মিডিয়া ডাউনলোড। ফেরত: ফাইল-পাথের তালিকা, অথবা "TOO_LARGE"।
    X-এর নিয়ম: ভিডিও থাকলে সাথে ছবি মেশানো যায় না — তাই ভিডিও পেলে ছবি বাদ।"""
    media_paths = []
    if cand.get("has_video") and cand.get("url"):
        print(f"  🎬 Video detected, yt-dlp: {cand['url']}")
        vids = download_videos_from_tweet(cand["url"])
        if vids == "TOO_LARGE":
            return "TOO_LARGE"
        if vids:
            media_paths = vids[:1]   # একটা ভিডিওই যথেষ্ট ও নিরাপদ
    elif cand.get("has_video"):
        print("  ⚠️ ভিডিও আছে কিন্তু status URL পাওয়া যায়নি — ভিডিও বাদ।")
    if not media_paths and cand.get("imgs"):
        media_paths = download_images(cand["imgs"], limit=4)
    return media_paths


# ──────────────────────────────────────────────
# FALLBACK IMAGE (media-less পোস্টের জন্য) — run শুরুতে একবার ডাউনলোড, পুরো রান জুড়ে reuse
# ──────────────────────────────────────────────
def download_fallback_image():
    url = os.environ.get("FALLBACK_IMAGE_URL")
    if not url:
        print("ℹ️ FALLBACK_IMAGE_URL সেট করা নেই — media-less পোস্টে fallback ছবি ব্যবহার হবে না।")
        return None
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200 and r.content:
            ext = ".jpg"
            content_type = r.headers.get("Content-Type", "").lower()
            if "gif" in content_type:
                ext = ".gif"
            elif "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif url.lower().split("?")[0].endswith(".gif"):
                ext = ".gif"
            if ext == ".gif" and len(r.content) > 15 * 1024 * 1024:
                print(f"⚠️ Fallback GIF {len(r.content) / 1024 / 1024:.1f}MB — X-এর ~15MB লিমিট ছাড়িয়ে গেছে, ব্যবহার হবে না।")
                return None
            path = os.path.join(MEDIA_DIR, f"fallback_image{ext}")
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"✅ Fallback image downloaded: {path} ({len(r.content)} bytes)")
            return path
        else:
            print(f"⚠️ Fallback image download failed: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ Fallback image download failed: {e}")
    return None


# ──────────────────────────────────────────────
# DEEPSEEK REWRITE (Instant mode + Search + DeepThink — den.py-এর প্রমাণিত লজিক)
# ──────────────────────────────────────────────
def _deepseek_ensure_toggle_on(page, label_text: str) -> None:
    try:
        toggles = page.query_selector_all("div[aria-pressed]")
        for t in toggles:
            span = t.query_selector("span")
            if span and span.inner_text().strip() == label_text:
                pressed = t.get_attribute("aria-pressed")
                cls = t.get_attribute("class") or ""
                is_on = (pressed == "true") and ("ds-toggle-button--selected" in cls)
                if not is_on:
                    t.click()
                    page.wait_for_timeout(random.uniform(400, 700))
                return
    except Exception as e:
        print(f"  ⚠️ DeepSeek toggle '{label_text}' error: {e}")


def _deepseek_is_focused(page, el) -> bool:
    try:
        return bool(page.evaluate(
            """(node) => {
                let p = node;
                for (let i = 0; i < 6 && p; i++) {
                    if (p.classList && p.classList.contains('focused')) return true;
                    p = p.parentElement;
                }
                return false;
            }""",
            el,
        ))
    except Exception:
        return False


def _deepseek_find_textarea(page, timeout=8000):
    for sel in [
        'textarea[name="search"]',
        'textarea[placeholder="Message DeepSeek"]',
        'textarea.ds-scroll-area',
        'textarea',
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def deepseek_rewrite(context, prompt: str) -> str | None:
    ds_session = load_deepseek_session()
    if not ds_session:
        print("  ❌ DeepSeek session not available — cannot rewrite.")
        return None
    ds_context = context.browser.new_context(storage_state=ds_session)
    page = ds_context.new_page()
    try:
        print("  🌐 DeepSeek page loading...")
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # Instant mode-এই থাকি (Expert সিলেক্ট করা হয় না) — den.py-এর মতো
        _deepseek_ensure_toggle_on(page, "Search")
        _deepseek_ensure_toggle_on(page, "DeepThink")
        textarea = _deepseek_find_textarea(page)
        if not textarea:
            print("  ❌ DeepSeek textarea not found.")
            page.screenshot(path=f"deepseek_debug_{int(time.time())}.png")
            return None
        textarea.click()
        page.wait_for_timeout(500)
        if not _deepseek_is_focused(page, textarea):
            textarea.click()
            page.wait_for_timeout(500)
            if not _deepseek_is_focused(page, textarea):
                print("  ⚠️ DeepSeek: ক্লিকের পরও ইনপুট বক্স focused হয়নি, তবু টাইপ করার চেষ্টা চলছে...")
        textarea.fill(prompt)
        page.wait_for_timeout(random.uniform(500, 800))
        try:
            current_value = textarea.input_value()
        except Exception:
            current_value = ""
        if not current_value.strip():
            print("  ⚠️ DeepSeek: fill()-এর পর বক্স খালি (stale handle সন্দেহ), textarea নতুন করে খুঁজে রিট্রাই করছি...")
            textarea = _deepseek_find_textarea(page, timeout=5000)
            if textarea:
                textarea.click()
                page.wait_for_timeout(500)
                textarea.fill(prompt)
                page.wait_for_timeout(random.uniform(500, 800))
                try:
                    current_value = textarea.input_value()
                except Exception:
                    current_value = ""
            if not textarea or not current_value.strip():
                print("  ❌ DeepSeek: রিট্রাইতেও বক্স খালি — প্রম্পট সাবমিট না করেই থামছে।")
                return None
        sent = False
        try:
            btn = page.wait_for_selector(
                'div[role="button"].ds-button--primary.ds-button--circle', timeout=5000
            )
            if btn and btn.is_visible() and btn.is_enabled():
                btn.click()
                sent = True
        except Exception:
            pass
        if not sent:
            page.keyboard.press("Enter")
        print("  ⏳ Waiting for DeepSeek response (Search + DeepThink, ~90s)...")
        page.wait_for_timeout(90000)
        response_text = ""
        last_text = ""
        stable_count = 0
        for _ in range(30):
            page.wait_for_timeout(2000)
            try:
                blocks = page.query_selector_all(
                    "div.ds-markdown.ds-assistant-message-main-content"
                )
                if blocks:
                    last_block = blocks[-1]
                    # সাইটেশন widget-গুলো টেক্সট তোলার আগেই DOM থেকে সরানো হয় —
                    # নাহলে hidden spacer span (opacity:0 কিন্তু layout দখল করে) থাকায়
                    # Chromium-এর innerText অযাচিত লাইন-ব্রেক ঢুকিয়ে দেয়। সংখ্যাওয়ালা
                    # badge গুলো .ds-markdown-cite-এ, আর SVG-লোগো/লিংক-টাইপগুলো
                    # DeepSeek-এর নিজস্ব hash class-এ (বিল্ডে বদলায়) — তাই class-এর
                    # বদলে inline style + ভেতরে svg এই structural প্যাটার্ন ধরা হয়।
                    last_block.evaluate(
                        """(el) => {
                            el.querySelectorAll('.ds-markdown-cite').forEach(n => n.remove());
                            el.querySelectorAll('span[style*="cursor: pointer"]').forEach(n => {
                                if (n.querySelector('svg')) n.remove();
                            });
                        }"""
                    )
                    txt = last_block.inner_text().strip()
                    txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
                    if txt:
                        if txt == last_text:
                            stable_count += 1
                        else:
                            stable_count = 0
                            last_text = txt
                        if stable_count >= 2:
                            response_text = txt
                            break
            except Exception:
                pass
        if not response_text and last_text:
            response_text = last_text
        REFUSAL_PHRASES = ["beyond my current scope", "i cannot", "i'm unable"]
        if response_text and any(p in response_text.lower() for p in REFUSAL_PHRASES):
            print(f"  🚫 DeepSeek refused: {response_text[:80]}...")
            return None
        if response_text:
            print(f"  ✅ DeepSeek response: {response_text[:100]}...")
            return response_text
        else:
            print("  ⚠️ DeepSeek no response.")
    except Exception as e:
        print(f"  ⚠️ DeepSeek error: {e}")
    finally:
        page.close()
        ds_context.close()
    return None


# ──────────────────────────────────────────────
# g4f AI call (tweet selection only)
# ──────────────────────────────────────────────
def clean_text(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'#+', '', text)
    text = text.replace('\"', '"').replace("\'", "'")
    return text.strip()


def ai_call(prompt):
    try:
        response = g4f.ChatCompletion.create(
            model=g4f.models.default,
            messages=[{"role": "user", "content": prompt}],
        )
        if response:
            return clean_text(str(response).strip())
        return None
    except Exception as e:
        print(f"  ❌ AI error: {e}")
        return None


# ──────────────────────────────────────────────
# AI SELECTION (g4f, no disqualifiers, source + text only)
# ──────────────────────────────────────────────
def ai_select_best_tweet(tweet_list):
    try:
        shortlist = []
        for t in tweet_list:
            shortlist.append({
                "source": t["source"],
                "text": t["text"][:4000]
            })
        prompt = f"""You are a sharp geopolitical news editor for X/Twitter.
Below are tweets from breaking news sources. Pick the ONE tweet that is the most newsworthy, urgent, and likely to get high engagement.
Consider:
Global geopolitical significance and urgency.
High public interest and potential engagement.
No reaction tweets.
STRICT EXCLUSION: Do NOT select any chinese political tweet.
Tweets:
{json.dumps(shortlist, indent=2, ensure_ascii=False)}
Return ONLY the index (0-based) of the best tweet. Nothing else.
Example: 2"""
        result = ai_call(prompt)
        if result and result.strip().isdigit():
            idx = int(result.strip())
            if 0 <= idx < len(tweet_list):
                return tweet_list[idx]
    except Exception as e:
        print(f"  ⚠️ AI post selection error: {e}")
    return None


# ──────────────────────────────────────────────
# CAPTION: তৈরি + পরিষ্কার (অদৃশ্য ক্যারেক্টার সরানো) + ২৮০ (X-ওজন) সীমায় আনা
# ──────────────────────────────────────────────
_INVISIBLE_RE = re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]')


def sanitize_caption(text):
    """DeepSeek/ওয়েব থেকে আসা টেক্সটে লুকানো zero-width/direction ক্যারেক্টার থাকতে পারে —
    এগুলো X-এর কম্পোজারে সমস্যা করতে পারে। সরিয়ে নিই।"""
    found = len(_INVISIBLE_RE.findall(text))
    if found:
        print(f"  🧽 ক্যাপশন থেকে {found}টি অদৃশ্য ক্যারেক্টার সরানো হলো।")
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u00a0', ' ')
    text = _INVISIBLE_RE.sub('', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def x_weighted_len(text):
    """X-এর অক্ষর-গণনার মোটামুটি হিসাব: URL = ২৩, CJK/ইমোজি = ২, বাকি = ১।"""
    text = re.sub(r'https?://\S+', 'x' * 23, text)
    total = 0
    for ch in text:
        o = ord(ch)
        if (0x1100 <= o <= 0x11FF or 0x2600 <= o <= 0x27BF or 0x2E80 <= o <= 0xD7FF
                or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6 or o >= 0x1F000):
            total += 2
        else:
            total += 1
    return total


def fit_caption(text, limit=280):
    text = text.strip()
    if x_weighted_len(text) <= limit:
        return text
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) > 1:
        head = parts[0]
        rest = " ".join(parts[1:])
        sentences = re.split(r'(?<=[.!?])\s+', rest)
        out = head
        first = True
        for s in sentences:
            cand = out + ("\n\n" if first else " ") + s
            if x_weighted_len(cand) <= limit:
                out = cand
                first = False
            else:
                break
        if x_weighted_len(out) <= limit:
            print("  ✂️ ক্যাপশন ছোট করা হলো (শেষের বাক্যগুলো বাদ)।")
            return out
        text = head
    out = ""
    for w in text.split(" "):
        cand = (out + " " + w).strip()
        if x_weighted_len(cand + "…") > limit:
            break
        out = cand
    print("  ✂️ ক্যাপশন ২৮০ সীমায় কাটা হলো।")
    return out.rstrip(".,;: ") + "…"


def build_final_caption(original_text, context=None):
    prompt = f"""IMPORTANT: All output must be in simple words.
Think step by step: Internally create 3 distinct drafts, each with a main sentence and 2, 3 detailed sentences everything under 280 total characters with key facts, in simple words. Then critically compare them—check for conciseness, factual accuracy, and strict character limit. Select the best one or merge the strongest parts into a single final version. After that, output only the final two lines in the format below, with no extra text.
Search web, rewrite this into a main sentence and 2, 3 detailed sentences everything under 280 total characters with key facts only, in simple words. No extra words. Try to include any relevant direct quotes if available.
If search does not confirm the tweet, ignore all other rules and just rewrite the tweet in simple words. Do not fact-check, do not add extra info, and do not follow the title/sentence format.
CRITICAL FORMAT RULES:
Output exactly two lines separated by one blank line.
First line: Main sentence.
Leave a blank line.
Third line: 2 or 3 detailed sentences.
Example of correct output:
Catastrophic 7.8 magnitude earthquake hits central Turkey, over 1,500 dead

Rescue teams work early Monday in freezing weather. They pull survivors from collapsed buildings as the death toll rises and thousands remain injured.

Tweet:
{original_text}"""
    if context:
        result = deepseek_rewrite(context, prompt)
    else:
        result = None
    if result:
        return result.strip()
    print("  ⚠️ DeepSeek failed, posting original tweet text as fallback...")
    return clean_text(original_text)


# ──────────────────────────────────────────────
# HUMAN-LIKE MOUSE MOVEMENT & TYPING
# ──────────────────────────────────────────────
def human_mouse_move(page, target_x, target_y, steps=15):
    start_x, start_y = random.randint(100, 300), random.randint(100, 300)
    cp_x = (start_x + target_x) / 2 + random.randint(-80, 80)
    cp_y = (start_y + target_y) / 2 + random.randint(-80, 80)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * cp_x + t ** 2 * target_x
        y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * cp_y + t ** 2 * target_y
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.015))


def _move_to(page, locator):
    try:
        box = locator.bounding_box()
        if box:
            human_mouse_move(page, box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    except Exception:
        pass


def human_type_text(page, text):
    """ফোকাস করা বক্সে অক্ষর অক্ষর করে টাইপ (এলিমেন্টে আলাদা ক্লিক লাগে না)।"""
    time.sleep(random.uniform(0.3, 0.8))
    for char in text:
        page.keyboard.type(char, delay=random.randint(40, 120))
        if random.random() < 0.05:
            time.sleep(random.uniform(0.3, 0.9))
    time.sleep(random.uniform(0.5, 1.2))


# ──────────────────────────────────────────────
# POSTING — রিডিজাইন: scope-ভিত্তিক কম্পোজার + CreateTweet উত্তরে যাচাই
# ──────────────────────────────────────────────
class PreClickError(Exception):
    """Post বাটনে ক্লিকের আগের ধাপের ব্যর্থতা — নিরাপদে অন্য পদ্ধতিতে আবার চেষ্টা করা যায়
    (কিছু পোস্ট হয়নি নিশ্চিত)। ক্লিকের পরের অনিশ্চয়তায় কখনো রিট্রাই করা হয় না — ডাবল পোস্ট এড়াতে।"""


POPUP_BUTTONS = ["Not now", "Maybe later", "Skip for now", "Dismiss"]


def _debug_shot(page, tag):
    # ফাইলের নাম attach_fail_*.png — workflow এটা আপলোড করে
    path = f"attach_fail_{tag}_{int(time.time())}.png"
    try:
        page.screenshot(path=path)
        print(f"  📸 স্ক্রিনশট: {path}")
    except Exception as e:
        print(f"  ⚠️ স্ক্রিনশট ব্যর্থ: {_short(e)}")


def describe_overlays(page):
    """#layers-এ এখন কী আছে — mask কয়টা, কোনো ডায়ালগ থাকলে তার টেক্সট (ডিবাগের জন্য)।"""
    try:
        return page.evaluate(r"""() => {
            const L = document.querySelector('#layers');
            if (!L) return 'no #layers';
            const dlgs = Array.from(L.querySelectorAll('[role="dialog"], [role="alertdialog"]'));
            const masks = L.querySelectorAll('[data-testid="mask"]').length;
            const t = dlgs.map(d => (d.innerText || '').replace(/\s+/g, ' ').slice(0, 140));
            return 'masks=' + masks + ' dialogs=' + dlgs.length + ' ' + JSON.stringify(t);
        }""")
    except Exception as e:
        return f"describe_overlays failed: {_short(e)}"


def describe_blocker(page, locator):
    """এলিমেন্টের মাঝখানে আসলে কোন এলিমেন্ট আছে (ক্লিক কে আটকাচ্ছে) — ওপরের দিকের চেইনসহ।"""
    try:
        box = locator.bounding_box()
        if not box:
            return "bounding box নেই"
        cx = box['x'] + box['width'] / 2
        cy = box['y'] + box['height'] / 2
        return page.evaluate(r"""([x, y]) => {
            const el = document.elementFromPoint(x, y);
            if (!el) return 'elementFromPoint=null';
            const chain = [];
            let n = el;
            for (let i = 0; i < 6 && n; i++) {
                const tid = n.getAttribute ? n.getAttribute('data-testid') : null;
                chain.push((n.tagName || '') + (tid ? '[' + tid + ']' : '') + (n.id ? '#' + n.id : ''));
                n = n.parentElement;
            }
            return chain.join(' < ');
        }""", [cx, cy])
    except Exception as e:
        return f"describe_blocker failed: {_short(e)}"


def dismiss_popups(page):
    """'Not now' / 'Maybe later' ধরনের পপআপ থাকলে বন্ধ করে (কম্পোজারের পথ খোলা রাখতে)।"""
    for name in POPUP_BUTTONS:
        try:
            loc = page.locator('#layers').get_by_role("button", name=name, exact=True)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000)
                print(f"  🧹 পপআপ বন্ধ করা হলো: '{name}'")
                page.wait_for_timeout(500)
        except Exception:
            pass


def read_toast(page):
    try:
        return page.locator('[data-testid="toast"]').first.inner_text(timeout=1500)
    except Exception:
        return ""


def get_composer_scope(page, wait_modal_ms=0):
    """কম্পোজার কোথায়: #layers-এর মোডালে থাকলে সেটাই (পেছনের inline বক্স নয়), নইলে পেজে।
    এই scope ধরেই সব selector চলে — এতে mask-এর পেছনের বক্সে ক্লিক করার ভুল আর হয় না।"""
    modal_sel = '#layers [data-testid="tweetTextarea_0"]'
    if wait_modal_ms:
        try:
            page.wait_for_selector(modal_sel, timeout=wait_modal_ms)
        except Exception:
            pass
    if page.locator(modal_sel).count() > 0:
        return page.locator('#layers'), "modal"
    return page.locator('body'), "inline"


def _norm_ws(s):
    return re.sub(r'\s+', '', s or "")


def attach_media(page, scope, media_paths):
    """ছবি/ভিডিও সংযুক্তি। প্রথমে লুকানো file input-এ সরাসরি (কোনো ক্লিক নেই, mask আটকাতে পারে না);
    না হলে ছবি-বাটন চেপে file chooser। ফেরত: true/false।"""
    attached = False
    try:
        inputs = scope.locator('input[type="file"][data-testid="fileInput"], input[type="file"]')
        if inputs.count() > 0:
            inputs.first.set_input_files(media_paths, timeout=15000)
            attached = True
            print(f"  🎞 {len(media_paths)} media ফাইল queued (সরাসরি input)।")
    except Exception as e:
        print(f"  ⚠️ সরাসরি input ব্যর্থ: {_short(e)}")
    if not attached:
        try:
            btn = scope.locator('button[aria-label="Add photos or video"]').first
            with page.expect_file_chooser(timeout=10000) as fc_info:
                btn.click(timeout=6000)
            fc_info.value.set_files(media_paths)
            attached = True
            print(f"  🎞 {len(media_paths)} media ফাইল queued (file chooser)।")
        except Exception as e:
            print(f"  ⚠️ file chooser পদ্ধতিও ব্যর্থ: {_short(e)}")
    if not attached:
        return False

    is_video = any(mp.lower().endswith('.mp4') for mp in media_paths)
    try:
        scope.locator('[data-testid="attachments"]').first.wait_for(
            state="visible", timeout=30000 if is_video else 15000)
        print("  ✅ Attachment container পাওয়া গেছে।")
    except Exception:
        print("  ⚠️ Attachment container দেখা যায়নি।")
        _debug_shot(page, "post_attach_missing")
    page.wait_for_timeout(6000 if is_video else random.randint(2500, 4000))
    return True


def click_post_and_verify(page, post_btn, textarea):
    """Post চেপে X-এর CreateTweet উত্তর ধরে — tweet id এলেই কেবল সফল।
    এই ফাংশন কখনো exception ছোড়ে না (ক্লিকের পরে রিট্রাই করা বিপজ্জনক)।"""
    def is_create(r):
        try:
            return "CreateTweet" in r.url and r.request.method == "POST"
        except Exception:
            return False

    method_used = "click"
    try:
        with page.expect_response(is_create, timeout=45000) as info:
            try:
                post_btn.click(timeout=8000)
            except Exception as e:
                blocker = describe_blocker(page, post_btn)
                print(f"  ⚠️ Post ক্লিক আটকেছে ({_short(e)}) | আটকাচ্ছে: {blocker}")
                print(f"     overlays: {describe_overlays(page)}")
                _debug_shot(page, "post_postclick_blocked")
                try:
                    textarea.focus(timeout=3000)
                except Exception:
                    pass
                page.keyboard.press("Control+Enter")
                method_used = "ctrl+enter"
        resp = info.value
    except Exception as e:
        err = f"CreateTweet-এর উত্তর আসেনি ({_short(e)})"
        print(f"  ❌ {err}")
        try:
            _debug_shot(page, "post_noresponse")
        except Exception:
            pass
        return {"ok": False, "error": err, "uncertain": True}

    try:
        status = resp.status
        data = None
        body = ""
        try:
            data = resp.json()
        except Exception:
            try:
                body = resp.text()[:300]
            except Exception:
                body = ""
        tweet_id = None
        err_msgs = []
        if isinstance(data, dict):
            try:
                res = data["data"]["create_tweet"]["tweet_results"]["result"]
                tweet_id = res.get("rest_id") or (res.get("tweet") or {}).get("rest_id")
            except Exception:
                pass
            for er in data.get("errors") or []:
                try:
                    err_msgs.append(f"{er.get('code')}: {er.get('message')}")
                except Exception:
                    pass
        if tweet_id:
            print(f"  ✅ X নিশ্চিত করেছে — tweet id {tweet_id} (https://x.com/i/status/{tweet_id}) [{method_used}]")
            try:
                page.wait_for_timeout(random.randint(2000, 4000))
            except Exception:
                pass
            return {"ok": True, "tweet_id": tweet_id}
        err = f"HTTP {status} | " + (" ; ".join(err_msgs) if err_msgs else f"body={body!r}")
        toast = read_toast(page)
        if toast:
            err += f" | toast={toast!r}"
        print(f"  ❌ X পোস্ট নেয়নি: {err}")
        _debug_shot(page, "post_rejected")
        return {"ok": False, "error": err}
    except Exception as e:
        err = f"উত্তর পড়তে সমস্যা ({_short(e)})"
        print(f"  ❌ {err}")
        return {"ok": False, "error": err, "uncertain": True}


def do_compose(page, text, media_paths, mode):
    """একটা পদ্ধতিতে পোস্টের পুরো ধাপ। ক্লিকের আগে সমস্যা হলে PreClickError।
    mode: 'compose_page' = x.com/compose/post, 'home_inline' = হোমের নিজস্ব বক্স।"""
    url = "https://x.com/compose/post" if mode == "compose_page" else "https://x.com/home"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(random.randint(3500, 6000))
    if check_session_dead(page):
        raise Exception("SESSION_DEAD")
    if check_captcha(page):
        raise Exception("CAPTCHA_DETECTED")
    dismiss_popups(page)

    # ── কম্পোজার খুঁজি ──
    try:
        page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=25000)
    except Exception:
        _debug_shot(page, "post_notextarea")
        raise PreClickError("লেখার বক্স (tweetTextarea_0) পাওয়া যায়নি")
    scope, kind = get_composer_scope(page, wait_modal_ms=3000 if mode == "compose_page" else 0)
    print(f"  🧭 কম্পোজার: {kind} ({mode}) | overlays: {describe_overlays(page)}")
    textarea = scope.locator('[data-testid="tweetTextarea_0"]').first

    # ── বক্সে ক্লিক (আটকালে কে আটকাচ্ছে লগ করে focus() দিয়ে এগোই) ──
    _move_to(page, textarea)
    try:
        textarea.click(timeout=8000)
    except Exception as e:
        blocker = describe_blocker(page, textarea)
        print(f"  ⚠️ বক্সে ক্লিক আটকেছে ({_short(e)}) | আটকাচ্ছে: {blocker}")
        _debug_shot(page, "post_click_blocked")
        try:
            textarea.focus(timeout=5000)
        except Exception:
            raise PreClickError("বক্সে ফোকাসও করা গেল না")

    # ── টাইপ + বক্সে আসলে কী লেখা হলো তা মিলিয়ে দেখা ──
    human_type_text(page, text)
    try:
        typed = textarea.inner_text(timeout=3000)
    except Exception:
        typed = ""
    if not _norm_ws(typed):
        _debug_shot(page, "post_typed_empty")
        raise PreClickError("টাইপের পর বক্স খালি")
    if _norm_ws(typed) != _norm_ws(text):
        print(f"  ⚠️ বক্সের লেখা ক্যাপশনের সাথে মেলেনি | বক্স={len(typed)} ক্যাপশন={len(text)}")
        print(f"     বক্সে: {typed[:140]!r}")
    page.wait_for_timeout(random.randint(800, 1500))

    # ── মিডিয়া (ব্যর্থ হলে টেক্সট-অনলি এগোই, আগের আচরণের মতো) ──
    if media_paths:
        if not attach_media(page, scope, media_paths):
            print("  ⚠️ মিডিয়া অ্যাটাচ হয়নি — টেক্সট-অনলি পোস্ট করা হবে।")
            _debug_shot(page, "post_attach_failed")

    # ── Post বাটন: visible + enabled না হওয়া পর্যন্ত অপেক্ষা (ভিডিও আপলোডে বেশি সময়) ──
    post_btn = scope.locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]').first
    try:
        post_btn.wait_for(state="visible", timeout=10000)
    except Exception:
        _debug_shot(page, "post_nobutton")
        raise PreClickError("Post বাটন পাওয়া যায়নি")
    has_video = any(mp.lower().endswith('.mp4') for mp in (media_paths or []))
    deadline = time.time() + (150 if has_video else 25)
    while True:
        try:
            if post_btn.is_enabled():
                break
        except Exception:
            pass
        if time.time() > deadline:
            _debug_shot(page, "post_btn_disabled")
            raise PreClickError("Post বাটন enabled হয়নি (আপলোড চলছে/অক্ষর-সীমা পেরিয়েছে?)")
        time.sleep(1.5)

    _move_to(page, post_btn)
    page.wait_for_timeout(random.randint(500, 1200))
    return click_post_and_verify(page, post_btn, textarea)


def open_compose_and_post(page, text, media_paths):
    """ফেরত: {"ok": bool, "tweet_id"?, "error"?, "uncertain"?}। SESSION_DEAD/CAPTCHA_DETECTED raise করে।"""
    methods = [("compose_page", "কম্পোজ-পেজ"), ("home_inline", "হোম-ইনলাইন")]
    last = {"ok": False, "error": "কোনো পদ্ধতিই চালু হয়নি"}
    for n, (mode, label) in enumerate(methods, 1):
        try:
            print(f"  🔄 পদ্ধতি {n} ({label})...")
            return do_compose(page, text, media_paths, mode)   # ক্লিকের পরের ফল চূড়ান্ত — রিট্রাই নেই
        except PreClickError as e:
            print(f"  ❌ পদ্ধতি {n} ক্লিকের আগেই ব্যর্থ: {e}")
            last = {"ok": False, "error": str(e)}
        except Exception as e:
            if "CAPTCHA_DETECTED" in str(e) or "SESSION_DEAD" in str(e):
                raise
            print(f"  ❌ পদ্ধতি {n} ব্যর্থ: {_short(e)}")
            _debug_shot(page, f"post_m{n}_exc")
            last = {"ok": False, "error": _short(e)}
    print("  💥 সব পদ্ধতি ব্যর্থ।")
    return last


def simulate_scroll(page):
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(2000, 3000))
        for _ in range(random.randint(2, 5)):
            page.mouse.wheel(0, random.randint(300, 800))
            time.sleep(random.uniform(0.5, 1.5))
        print("  📜 Scrolled timeline naturally.")
    except Exception as e:
        print(f"  ⚠️ Scroll error: {_short(e)}")


# ──────────────────────────────────────────────
# STOCHASTIC FAIR TOP-N SELECTION
# ──────────────────────────────────────────────
def _weighted_pick_one(cands):
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    weights = [max(c['score'], 1.0) for c in cands]
    return random.choices(cands, weights=weights, k=1)[0]


def _weighted_sample_without_replacement(cands, k):
    pool = list(cands)
    chosen = []
    for _ in range(min(k, len(pool))):
        weights = [max(c['score'], 1.0) for c in pool]
        pick = random.choices(pool, weights=weights, k=1)[0]
        chosen.append(pick)
        pool.remove(pick)
    return chosen


def select_shortlist_for_ai(candidates, top_n=15):   # increased to 15 for more sources
    by_source = {}
    for c in candidates:
        by_source.setdefault(c['source'], []).append(c)
    per_source_picks = []
    for source, cands in by_source.items():
        pick = _weighted_pick_one(cands)
        if pick:
            per_source_picks.append(pick)
    if len(per_source_picks) <= top_n:
        shortlist = list(per_source_picks)
        if len(shortlist) < top_n:
            chosen_ids = {id(c) for c in shortlist}
            remaining = [c for c in candidates if id(c) not in chosen_ids]
            need = top_n - len(shortlist)
            shortlist.extend(_weighted_sample_without_replacement(remaining, need))
    else:
        shortlist = _weighted_sample_without_replacement(per_source_picks, top_n)
    random.shuffle(shortlist)
    return shortlist


# ──────────────────────────────────────────────
# POST-ONLY FUNCTION
# ফেরত: True = পোস্ট হয়েছে (X নিশ্চিত) | "dead" = সেশন মৃত | "post_failed" = পোস্ট ব্যর্থ
#       "net_down" = পরপর ৩টা সোর্স লোড হয়নি (নেটওয়ার্ক/exit node সমস্যা)
#       False = পোস্টযোগ্য কিছু পাওয়া যায়নি/ক্যাপচা
# ──────────────────────────────────────────────
def perform_post_only(page, posted_cache, fallback_image_path=None):
    context = page.context
    candidates = []
    load_fails = 0      # পরপর কতগুলো সোর্স লোডই হয়নি (ফোন অফলাইন/exit node মৃত হলে এটা বাড়ে)
    for source in random.sample(SOURCES, len(SOURCES)):
        print(f"\n📡 @{source} checking...")
        try:
            page.goto(f"https://x.com/{source}", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(1500, 3500))
        except Exception as e:
            load_fails += 1
            print(f"  ⚠️ @{source} লোড হয়নি ({load_fails}/3): {_short(e)}")
            if load_fails >= 3:
                print("🌐 পরপর ৩টা সোর্স লোড হয়নি — নেটওয়ার্ক/exit node সমস্যা ধরে এই রাউন্ড বাতিল।")
                return "net_down"
            continue
        load_fails = 0
        if check_captcha(page):
            return False
        if check_session_dead(page):
            return "dead"
        tweets = page.query_selector_all('article[data-testid="tweet"]')
        if not tweets:
            continue
        for i, tweet in enumerate(tweets[:8]):   # ৬ → ৮: সাপ্লাই ইনশুরেন্স
            try:
                if is_pinned_tweet(tweet) or is_retweet(tweet) or is_thread_continuation(tweet):
                    continue
                text_el = tweet.query_selector('div[data-testid="tweetText"]')
                txt = text_el.inner_text() if text_el else ""
                if not txt or is_duplicate(txt, posted_cache) or is_promotional(txt) or is_too_short(txt) or is_forbidden_topic(txt):
                    continue
                if text_hash(txt) in RUN_SKIP:
                    continue
                age = get_tweet_age_minutes(tweet)
                if age > 240:          # 4-hour cutoff
                    continue
                stats = get_tweet_stats(tweet)
                likes, views = stats["likes"], stats["views"]
                imgs, has_video = get_tweet_media_info(tweet)
                url = get_tweet_status_url(tweet)
                sc = score_tweet(txt, likes, views, age)
                flags = ("🎥" if has_video else "") + (f"🖼{len(imgs)}" if imgs else "")
                print(f"  ✅ {i + 1} | Age:{age}m Likes:{likes} Views:{views} Score:{sc:.1f} {flags}| {txt[:60]}...")
                candidates.append({
                    'index': i, 'text': txt, 'source': source, 'likes': likes,
                    'views': views, 'age_min': age, 'score': sc,
                    'url': url, 'imgs': imgs, 'has_video': has_video,
                })
            except Exception:
                pass
    if not candidates:
        print("\n⚠️ No new posts found.")
        return False

    # ────── Topic memory filtering (6-hour window, min 3 common keywords) ──────
    topic_memory = load_topic_memory()
    candidates = [c for c in candidates if not is_similar_topic(c["text"], topic_memory, min_overlap=3)]
    if not candidates:
        print("\n⚠️ All candidates are on recently posted topics — skipping this round.")
        return False

    # ────── নির্বাচন + মিডিয়া (ভিডিও খুব বড় হলে পরের সেরা প্রার্থী, সর্বোচ্চ ৩ বার) ──────
    pool = list(candidates)
    best_tweet = None
    media_paths = []
    for attempt in range(3):
        if attempt == 0:
            shortlist = select_shortlist_for_ai(pool, top_n=15)
            best_tweet = ai_select_best_tweet(shortlist)
        else:
            best_tweet = None
        if best_tweet is None:
            best_tweet = max(pool, key=lambda x: x['score'])
        print(f"\n🏆 Selected: @{best_tweet['source']} | {best_tweet['text'][:100]}...")
        media = prepare_media(best_tweet)
        if media == "TOO_LARGE":
            print("  ⏭ Video too large, trying next best candidate...")
            pool = [c for c in pool if c is not best_tweet]
            if not pool:
                print("  ❌ No more candidates.")
                return False
            continue
        media_paths = media
        break
    else:
        print("  ❌ তিনবারই ভিডিও বড় — এই রাউন্ড বাদ।")
        return False

    original_text = best_tweet['text']
    print(f"  🎥 Video: {best_tweet['has_video']}, 🖼 Media: {len(media_paths)} files")

    # ── কোনো নেটিভ মিডিয়া না পাওয়া গেলে fallback ছবি ব্যবহার ──
    if not media_paths and fallback_image_path and os.path.exists(fallback_image_path):
        print("  🖼️ No native media found — using fallback image.")
        media_paths = [fallback_image_path]

    print("  🤖 Generating caption...")
    final_caption = build_final_caption(original_text, context=context)
    final_caption = sanitize_caption(final_caption)
    final_caption = fit_caption(final_caption, limit=280)
    print(f"  ✅ Caption ({x_weighted_len(final_caption)}/280): {final_caption}")

    print("\n📤 Posting...")
    try:
        result = open_compose_and_post(page, final_caption, media_paths)
    except Exception as e:
        if "SESSION_DEAD" in str(e):
            return "dead"
        if "CAPTCHA_DETECTED" in str(e):
            return False
        raise
    finally:
        for path in media_paths:
            if path == fallback_image_path:
                continue  # fallback ছবিটা পুরো run জুড়ে reuse হবে
            try:
                os.remove(path)
            except Exception:
                pass

    if result.get("ok"):
        save_to_cache(original_text, POSTED_CACHE)
        add_to_topic_memory(original_text)
        trim_cache(POSTED_CACHE)
        print("✅ Post successful! (X নিশ্চিত করেছে)")
        save_session_cache(context, label="post")
        limit_reached = increment_daily_counter()
        simulate_scroll(page)
        if limit_reached:
            print("🎯 Daily post limit reached. Stopping further posts today.")
        return True

    # ── ব্যর্থ: কাউন্টার/টপিক মেমরি বাড়ে না; এই রানে একই টুইট আর নয় ──
    RUN_SKIP.add(text_hash(original_text))
    if result.get("uncertain"):
        # উত্তর আসেনি — পোস্ট হয়ে থাকতে পারে; ডুপ্লিকেট এড়াতে ক্যাশে রাখি (কাউন্টারে গুনি না)
        save_to_cache(original_text, POSTED_CACHE)
        print("⚠️ ফল অনিশ্চিত — সম্ভাব্য ডুপ্লিকেট এড়াতে টুইটটা ক্যাশে রাখা হলো, কাউন্টারে গোনা হয়নি।")
    print(f"❌ Post failed: {result.get('error')}")
    return "post_failed"


# ──────────────────────────────────────────────
# HUMAN DELAY (office-hour strategy: পোস্ট-সাইকেল শেষে র‍্যান্ডম বিরতি)
# ──────────────────────────────────────────────
def human_delay():
    lo = int(os.environ.get("DELAY_MIN_MINUTES", "5"))
    hi = int(os.environ.get("DELAY_MAX_MINUTES", "15"))
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi) * 60


# ──────────────────────────────────────────────
# MAIN LOOP (রান-উইন্ডো workflow-এর MAX_RUN_MINUTES env থেকে আসে)
# ফেরত: exit code (0 = ঠিক, 1 = ব্যর্থ/সেশন মৃত/পরপর পোস্ট-ব্যর্থতা)
# ──────────────────────────────────────────────
WEBDRIVER_HIDE_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Google Inc. (Intel)';
        if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return getParameter.call(this, parameter);
    };
    const originalCreateOscillator = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function() {
        const osc = originalCreateOscillator.apply(this, arguments);
        const originalStart = osc.start;
        osc.start = function() {
            setTimeout(() => originalStart.apply(this, arguments), Math.random() * 2);
        };
        return osc;
    };
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            originalQuery(parameters)
    );
"""


def run_bot_loop():
    if not validate_session():
        return 1
    if is_captcha_locked():
        return 0
    target, current = get_daily_limit()
    print(f"📊 Daily limit: {current}/{target}")
    if current >= target:
        print("🎯 Today's post limit already reached. Exiting.")
        return 0

    # ── রান-ডিউরেশনের নব workflow-এ (MAX_RUN_MINUTES env); কোড শুধু মানটা পড়ে নেয় ──
    MAX_RUN_MINUTES = int(os.environ.get("MAX_RUN_MINUTES", "235"))
    MAX_DURATION = MAX_RUN_MINUTES * 60
    MAX_FAILS = int(os.environ.get("MAX_CONSECUTIVE_FAILS", "4"))
    print(f"⏱ Run window: {MAX_RUN_MINUTES} minutes (workflow-set). পরপর {MAX_FAILS}বার ব্যর্থ হলে রান ফেল হবে।")
    start_time = time.time()
    exit_code = 0

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=egl",
            ]
        )
        session_data = load_session()
        context = browser.new_context(
            storage_state=session_data,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()
        page.add_init_script(WEBDRIVER_HIDE_JS)

        # ── শুরুতেই যাচাই: আসলে লগইন আছে? কোন অ্যাকাউন্টে? ──
        if not verify_login_and_identity(page):
            clear_session_cache()
            browser.close()
            return 1

        # ── run শুরুতে একবারই fallback ছবি ডাউনলোড, পুরো রান জুড়ে reuse হবে ──
        fallback_image_path = download_fallback_image()

        print(f"\n🤖 News Bot started (Post-Only Mode) — {datetime.now(BD_TZ).strftime('%Y-%m-%d %H:%M:%S')} (BD time)")
        iteration = 0
        posts_since_siesta = 0
        consecutive_fails = 0
        session_died = False
        SIESTA_EVERY = 8             # প্রতি ৮ পোস্টে একবার চা-বিরতি

        while True:
            target, current = get_daily_limit()
            if current >= target:
                print("🎯 Daily limit reached. Stopping.")
                break
            elapsed = time.time() - start_time
            if elapsed > MAX_DURATION:
                print(f"⏰ MAX_RUN_MINUTES ({MAX_RUN_MINUTES}m, workflow-set) reached. Exiting loop gracefully.", flush=True)
                break
            if is_captcha_locked():
                print("🔒 Captcha lock active. Exiting loop.", flush=True)
                break
            iteration += 1
            now = datetime.now(BD_TZ)
            print(f"\n🔄 Post iteration {iteration} — {now.strftime('%H:%M:%S')} (BD time)", flush=True)
            posted_cache = load_cache(POSTED_CACHE)

            try:
                success = perform_post_only(page, posted_cache, fallback_image_path)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"💥 অপ্রত্যাশিত ত্রুটি: {_short(e)}", flush=True)
                traceback.print_exc()
                try:
                    page.screenshot(path=f"captcha_debug_crash_{int(time.time())}.png")
                except Exception:
                    pass
                success = "post_failed"

            if success == "dead":
                print("🔐 Session dead — stopping run. নতুন কুকি আপলোড করুন।", flush=True)
                session_died = True
                exit_code = 1
                break

            if success is True:
                consecutive_fails = 0
                posts_since_siesta += 1
                delay = human_delay()
            elif success in ("post_failed", "net_down"):
                consecutive_fails += 1
                what = "নেটওয়ার্ক/exit node সমস্যা" if success == "net_down" else "পোস্ট-ব্যর্থতা"
                print(f"⚠️ পরপর {what}: {consecutive_fails}/{MAX_FAILS}", flush=True)
                if consecutive_fails >= MAX_FAILS:
                    if success == "net_down":
                        print("🛑 নেটওয়ার্ক বারবার অচল (ফোন অফলাইন/Tailscale বন্ধ/ইন্টারনেট নেই?) — "
                              "অকারণ ঘুরে না থেকে রান ফেল করা হলো।", flush=True)
                    else:
                        print("🛑 পরপর অনেকবার পোস্ট ব্যর্থ — ফেক চলা এড়াতে রান ফেল করা হলো। "
                              "attach_fail_* স্ক্রিনশট ও লগের 'X পোস্ট নেয়নি' লাইন দেখুন।", flush=True)
                    exit_code = 1
                    break
                delay = random.randint(2, 4) * 60   # ব্যর্থতার পর ছোট বিরতি
            else:
                print("⚠️ এই রাউন্ডে পোস্ট হয়নি (পোস্টযোগ্য কিছু নেই/ক্যাপচা), বিরতির পর আবার।", flush=True)
                delay = human_delay()

            print(f"⏳ Next post in {delay // 60} minutes...", flush=True)
            time.sleep(delay)
            if posts_since_siesta >= SIESTA_EVERY:
                siesta = random.randint(10, 15) * 60
                print(f"\n☕ Siesta for {siesta // 60} minutes...", flush=True)
                time.sleep(siesta)
                posts_since_siesta = 0

        if session_died:
            clear_session_cache()
        else:
            save_session_cache(context, label="run end")
        browser.close()
        print("\n🔒 Browser closed. Loop ended.", flush=True)
    return exit_code


if __name__ == "__main__":
    delay = random.randint(60, 180)
    print(f"⏱ {delay}s initial delay...")
    time.sleep(delay)
    sys.exit(run_bot_loop() or 0)
