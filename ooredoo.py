import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
import urllib3

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from playwright.async_api import async_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== الإعدادات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7261013113:AAFbRbjTcPFCuezmqKgG9sNxUEO8WsR_IvI")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
NUMBERS_FILE = os.path.join(BASE_DIR, "numbers.txt")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
STATE_FILE = os.path.join(BASE_DIR, "pw_state.json")

# مسار دائم لمتصفح Playwright
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.join(BASE_DIR, ".pw-browsers"),
)

# ==================== ثوابت Ooredoo ====================
OOREDOO_HOME = "https://my.ooredoo.dz/"
OOREDOO_BFF = "https://apis.ooredoo.dz/api/ooredoo-bff"
BUNDLE_CODE = "BTL_24H_DATA_Free_2Go"

OOREDOO_X_VERSION = "1.5.17"
OOREDOO_X_SIG = "5c7641dbb82cf11066df68afc91541c967640106a95192740b43d51a5ce6777a"

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("ooredoo-bot")

LAST_ERROR = ""
job_lock = asyncio.Lock()

http_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=1)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)


# ==================== أدوات مساعدة ====================

def mask_msisdn(msisdn):
    if not msisdn or len(msisdn) < 4:
        return "05********"
    return msisdn[:2] + "********"


def normalize_msisdn(raw: str):
    raw = re.sub(r"\D", "", raw or "")
    if raw.startswith("0") and len(raw) == 10:
        raw = "213" + raw[1:]
    if re.match(r"^213[567]\d{8}$", raw):
        return raw
    return None


def now_ms():
    return str(int(time.time() * 1000))


def new_instance_id():
    u = uuid.uuid4().hex
    return f"{u[:8]}-{u[8:12]}-11{u[12:16]}-{u[16:20]}-{u[20:32]}{now_ms()}"


def new_plat_sig(iid):
    return base64.b64encode(json.dumps(
        {"platform": "android", "is-physical-device": True, "device-id": iid},
        separators=(",", ":")).encode()).decode()


def new_fingerprint():
    return uuid.uuid4().hex + uuid.uuid4().hex[:32]


# ==================== Cookies ====================

def parse_cookies_file(path: str):
    """يقرأ cookies.txt بتنسيق Netscape ويرجّع list of dicts جاهزة لـ Playwright."""
    cookies = []
    if not os.path.exists(path):
        return cookies
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, path_, secure, expires, name, value = parts[:7]
                # Playwright يحتاج domain يبدأ بنقطة أو بدونها لكن يفضّل النقطة
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path_ or "/",
                    "expires": float(expires) if expires.isdigit() else -1,
                    "httpOnly": False,
                    "secure": secure.upper() == "TRUE",
                    "sameSite": "Lax",
                })
    except Exception as e:
        log.warning(f"فشل قراءة cookies.txt: {e}")
    return cookies


def get_msisdn_from_cookies(cookies):
    for c in cookies:
        if c["name"].lower() in ("msisdn", "x-msisdn"):
            n = normalize_msisdn(c["value"])
            if n:
                return n
    return None


# ==================== متصفح Playwright ====================

_install_lock = None
INSTALL_LOG = ""


async def launch_browser(p, msg=None):
    args = dict(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    )
    try:
        return await p.chromium.launch(**args)
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
    if msg:
        await safe_edit(msg, "⬇️ جاري تثبيت Chromium لأول مرة (قد يستغرق دقائق)...")
    ok = await asyncio.to_thread(_ensure_chromium)
    if not ok:
        raise RuntimeError("فشل تثبيت Chromium. تحقق من السجل.")
    return await p.chromium.launch(**args)


def _browsers_dir() -> str:
    return os.environ["PLAYWRIGHT_BROWSERS_PATH"]


def _find(pattern: str) -> bool:
    import glob
    paths = glob.glob(os.path.join(_browsers_dir(), pattern), recursive=True)
    return any(os.path.isfile(x) for x in paths)


def _ensure_chromium() -> bool:
    global INSTALL_LOG
    if _find("chromium_headless_shell-*/**/chrome-headless-shell") or _find("chromium-*/**/chrome"):
        return True
    INSTALL_LOG = ""
    py = [sys.executable, "-m", "playwright"]
    try:
        import subprocess
        r = subprocess.run(py + ["install", "chromium", "chromium-headless-shell"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=900)
        INSTALL_LOG = (r.stdout or "")[-2000:]
    except Exception as e:
        INSTALL_LOG = str(e)
        return False
    return _find("chromium_headless_shell-*/**/chrome-headless-shell") or _find("chromium-*/**/chrome")


# ==================== استخراج التوكن من المتصفح ====================

async def extract_token_via_browser(page, cookies):
    """
    يفتح الصفحة الرئيسية لـ My Ooredoo، يعترض أي طلب يحمل Authorization
    ويرجّع (token, msisdn).
    """
    captured = {"token": None, "msisdn": None}

    async def on_request(request):
        try:
            auth = request.headers.get("authorization") or request.headers.get("Authorization")
            if auth and auth.lower().startswith("bearer ") and not captured["token"]:
                captured["token"] = auth[7:].strip()
            msisdn_hdr = request.headers.get("x-msisdn")
            if msisdn_hdr and not captured["msisdn"]:
                captured["msisdn"] = normalize_msisdn(msisdn_hdr)
        except Exception:
            pass

    async def on_response(response):
        try:
            # بعض الـ endpoints ترجع التوكن في الـ JSON مباشرة
            if "/token" in response.url or "/auth" in response.url:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    data = await response.json()
                    if isinstance(data, dict):
                        tok = data.get("access_token") or data.get("token")
                        if tok and not captured["token"]:
                            captured["token"] = tok
        except Exception:
            pass

    page.on("request", lambda r: asyncio.create_task(on_request(r)))
    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    # فتح الصفحة الرئيسية - يتم حقن cookies مسبقًا
    try:
        await page.goto(OOREDOO_HOME, timeout=90000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto home: {e}")

    # انتظر حتى ظهور التوكن أو انتهاء المهلة
    for _ in range(30):  # حتى 30 ثانية
        if captured["token"]:
            break
        await asyncio.sleep(1)

    # إذا لم نلتقط شيئًا، جرّب استخراج التوكن من localStorage/sessionStorage
    if not captured["token"]:
        try:
            js_tokens = await page.evaluate("""() => {
                const out = {};
                for (const s of [localStorage, sessionStorage]) {
                    try {
                        for (let i = 0; i < s.length; i++) {
                            const k = s.key(i);
                            const v = s.getItem(k);
                            if (v && v.toLowerCase().includes('token')) out[k] = v;
                        }
                    } catch(e){}
                }
                return out;
            }""")
            for v in js_tokens.values():
                m = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", v or "")
                if m:
                    captured["token"] = m.group(0)
                    break
        except Exception:
            pass

    # إذا لم نجد msisdn من الطلبات، خذه من الـ cookies
    if not captured["msisdn"]:
        captured["msisdn"] = get_msisdn_from_cookies(cookies)

    return captured["token"], captured["msisdn"]


# ==================== تفعيل الهدية عبر API ====================

def build_auth_headers(msisdn, token):
    iid = new_instance_id()
    psig = new_plat_sig(iid)
    return {
        "User-Agent": "Dart/3.11 (dart:io)",
        "Accept-Encoding": "gzip",
        "x-msisdn": msisdn,
        "x-device-fingerprint": new_fingerprint(),
        "x-platform-origin": "web",
        "authorization": f"Bearer {token}",
        "x-correlation-id": uuid.uuid4().hex + now_ms(),
        "platform": "web",
        "x-version": OOREDOO_X_VERSION,
        "x-signature": OOREDOO_X_SIG,
        "x-platform-data-signature": psig,
        "x-timestamp": now_ms(),
        "x-instance-id": iid,
        "origin": "https://my.ooredoo.dz",
        "referer": "https://my.ooredoo.dz/",
        "content-type": "application/json; charset=utf-8",
        "accept": "*/*",
    }


def purchase_bundle(msisdn, token):
    headers = build_auth_headers(msisdn, token)
    body = json.dumps({"bundleCode": BUNDLE_CODE})
    try:
        r = http_session.post(
            f"{OOREDOO_BFF}/cvm/dynamic-bundle-purchase",
            headers=headers, data=body, timeout=20, verify=False,
        )
        if r.status_code == 200:
            next_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
            return True, f"🎉 تم التفعيل (2GB / 24h)\n⏰ التجديد: {next_time}"
        return False, f"⏳ فشل التفعيل (code={r.status_code})\n{r.text[:300]}"
    except Exception as e:
        return False, f"❌ خطأ اتصال: {e}"


def check_balance(msisdn, token):
    headers = build_auth_headers(msisdn, token)
    try:
        r = http_session.get(
            f"{OOREDOO_BFF}/api/v1/subscribers/main-balance/{msisdn}",
            headers=headers, timeout=15, verify=False,
        )
        if r.status_code == 200:
            try:
                return r.json().get("mainBalance", "غير متاح")
            except Exception:
                return "غير متاح"
        return f"خطأ (code={r.status_code})"
    except Exception as e:
        return f"خطأ: {e}"


# ==================== العملية الرئيسية (Playwright) ====================

async def run_ooredoo_job(msg, numbers=None, do_balance=False):
    """
    يشغّل متصفح Playwright، يسجل الدخول بالكوكيز، يستخرج التوكن،
    ثم يفعّل الهدية لكل رقم في القائمة.
    """
    cookies = parse_cookies_file(COOKIES_FILE)
    if not cookies:
        return "❌ لا يوجد cookies.txt أو أنه فارغ. صدّر الكوكيز من my.ooredoo.dz."

    base_msisdn = get_msisdn_from_cookies(cookies)
    if not base_msisdn:
        return "❌ لم أجد msisdn داخل cookies.txt. تأكد من وجود cookie باسم msisdn."

    if numbers is None:
        numbers = [base_msisdn]
        if os.path.exists(NUMBERS_FILE):
            with open(NUMBERS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    n = normalize_msisdn(line.strip())
                    if n and n not in numbers:
                        numbers.append(n)

    token = None
    browser = None
    try:
        async with async_playwright() as p:
            await safe_edit(msg, "🌐 فتح المتصفح...")
            browser = await launch_browser(p, msg)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                locale="ar-DZ",
            )

            # حقن الكوكيز
            try:
                await ctx.add_cookies(cookies)
            except Exception as e:
                log.warning(f"add_cookies: {e}")
                return f"❌ فشل تحميل cookies.txt في المتصفح: {e}"

            page = await ctx.new_page()
            await safe_edit(msg, "🔐 الدخول إلى My Ooredoo بالجلسة المحفوظة...")
            token, session_msisdn = await extract_token_via_browser(page, cookies)

            if session_msisdn and session_msisdn not in numbers:
                numbers.insert(0, session_msisdn)

            await browser.close()
            browser = None
    except Exception as e:
        log.exception("playwright job")
        return f"❌ خطأ أثناء الدخول عبر Playwright:\n{e}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    if not token:
        return "❌ لم أستطع استخراج access_token من الجلسة. الكوكيز منتهية أو غير كافية، أعد تصديرها."

    # تنفيذ العمليات
    results = []
    for msisdn in numbers:
        if do_balance:
            bal = await asyncio.to_thread(check_balance, msisdn, token)
            results.append(f"📱 {mask_msisdn(msisdn)}\n💰 {bal}")
        else:
            ok, m = await asyncio.to_thread(purchase_bundle, msisdn, token)
            results.append(f"📱 {mask_msisdn(msisdn)}\n{m}")

    header = "💰 نتائج الرصيد:" if do_balance else "📊 نتائج التفعيل:"
    return header + "\n\n" + "\n\n".join(results)


# ==================== أوامر البوت ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت تفعيل عرض أوريدو المجاني (2GB / 24h) عبر Playwright\n\n"
        "📌 الأوامر:\n"
        "/activate — تفعيل الهدية\n"
        "/balance — عرض الرصيد\n"
        "/status — حالة الإعدادات\n"
        "/help — المساعدة\n\n"
        "⚠️ المطلوب: cookies.txt من my.ooredoo.dz + (اختياري) numbers.txt"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = parse_cookies_file(COOKIES_FILE)
    msisdn = get_msisdn_from_cookies(cookies)
    msg = (
        f"📄 cookies.txt: {'✅' if cookies else '❌'} ({len(cookies)} cookie)\n"
        f"📄 numbers.txt: {'✅' if os.path.exists(NUMBERS_FILE) else '❌'}\n"
        f"📱 msisdn من الكوكيز: {mask_msisdn(msisdn) if msisdn else '❌ غير موجود'}\n"
    )
    if LAST_ERROR:
        msg += f"\n❌ آخر خطأ:\n{LAST_ERROR[-400:]}"
    await update.message.reply_text(msg)


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية جارية، انتظر...")
        return
    async with job_lock:
        msg = await update.message.reply_text("🚀 بدء العملية...")
        try:
            result = await run_ooredoo_job(msg, do_balance=False)
        except Exception as e:
            log.exception("activate")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية جارية، انتظر...")
        return
    async with job_lock:
        msg = await update.message.reply_text("💰 جاري جلب الرصيد...")
        try:
            result = await run_ooredoo_job(msg, do_balance=True)
        except Exception as e:
            log.exception("balance")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)


async def safe_edit(msg, text: str):
    try:
        await msg.edit_text(text[:4000])
    except Exception:
        try:
            await msg.reply_text(text[:4000])
        except Exception:
            pass


# ==================== التشغيل ====================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, activate))
    return app


def main():
    global LAST_ERROR
    print(f"Python {sys.version.split()[0]}")
    # تثبيت Chromium في الخلفية
    try:
        asyncio.get_event_loop().run_until_complete(asyncio.to_thread(_ensure_chromium))
    except Exception:
        pass

    for attempt in range(1, 4):
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            app = build_app()
            log.info("🤖 البوت جاهز — أرسل /activate")
            app.run_polling(close_loop=False)
            return
        except Exception:
            LAST_ERROR = traceback.format_exc()
            log.error(f"توقف البوت (المحاولة {attempt}/3):\n{LAST_ERROR}")
            if attempt == 3:
                raise
            time.sleep(5 * attempt)


if __name__ == "__main__":
    main()