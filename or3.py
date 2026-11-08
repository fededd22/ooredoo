import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from playwright.async_api import async_playwright

# محاولة استيراد Appium (اختياري - قد لا يكون مثبتاً)
try:
    from appium import webdriver as appium_webdriver
    from appium.options.android import UiAutomator2Options
    from appium.webdriver.common.appiumby import AppiumBy
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    APPIUM_AVAILABLE = True
except ImportError:
    APPIUM_AVAILABLE = False
    log_temp = logging.getLogger("ooredoo-bot")
    log_temp.warning("Appium غير مثبت - سيتم تعطيل محاكاة Android Native")


# ==================== الإعدادات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7261013113:AAFbRbjTcPFCuezmqKgG9sNxUEO8WsR_IvI")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
OFFERS_DUMP = os.path.join(BASE_DIR, "offers_dump.json")
APPIUM_DUMP = os.path.join(BASE_DIR, "appium_dump.json")

# مسار دائم لمتصفح Playwright
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE_DIR, ".pw-browsers"))

MY_OOREDOO_URL = "https://my.ooredoo.dz/"

# إعدادات Appium / Android
APP_PACKAGE = "com.algeria.selfcare.app.android"
APP_ACTIVITY = ".MainActivity"  # قد يحتاج تعديل بعد dumpsys
APPIUM_HOST = os.getenv("APPIUM_HOST", "127.0.0.1")
APPIUM_PORT = int(os.getenv("APPIUM_PORT", "4723"))
DEVICE_UDID = os.getenv("DEVICE_UDID", None)  # إن كانت فارغة، Appium يختار تلقائياً
CHROME_DEBUG_PORT = int(os.getenv("CHROME_DEBUG_PORT", "9222"))

# ==================== المسارات المستهدفة (مستخرجة من pp.txt) ====================
TARGET_API_PATTERNS = [
    "/bundle/getActivePackages",
    "/bundle/bundlePurchase",
    "/bundle/slave/bundlePurchase",
    "/bundle/others/",
    "/bundle/social-bundle/",
    "/bundle/boost/",
    "/bundle/tod/",
    "/bundle/free-calls/",
    "/bundle/bundleConversion",
    "/bundle/getBundleConvertEligibility",
    "/cvm/getBundles",
    "/cvm/bundlePurchase",
    "/cvm/dynamic-bundle-purchase",
    "/cvm/status",
    "/cvm/cvm_optINOUT",
    "/gamification/play",
    "/gamification/status",
    "/gamification/power-snake",
    "/gamification/gta-bundle",
    "/gamification/history",
    "/power-snake/",
    "/cms/v2/api/product-offerings",
    "/cms/v2/api/countries",
    "/ipg/init",
    "/plan/laSwitch/",
    "/plan/data-sharing/",
]

# ==================== السجلات ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("ooredoo-bot")

LAST_ERROR = ""
job_lock = asyncio.Lock()


# ==================== أدوات مساعدة ====================

def parse_cookies_file(path: str):
    """قراءة cookies.txt بصيغة Netscape وتحويلها لقائمة يقبلها Playwright"""
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
            raw = re.sub(r"\D", "", c["value"] or "")
            if raw.startswith("0") and len(raw) == 10:
                raw = "213" + raw[1:]
            if re.match(r"^213[567]\d{8}$", raw):
                return raw
    return None


def mask_msisdn(msisdn):
    if msisdn and len(msisdn) > 6:
        return msisdn[:3] + "******" + msisdn[-2:]
    return "******"


async def safe_edit(msg, text: str):
    try:
        await msg.edit_text(text[:4000])
    except Exception:
        try:
            await msg.reply_text(text[:4000])
        except Exception:
            pass


# ==================== إدارة المتصفح ====================

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
        await safe_edit(msg, "⬇️ تشغيل أول مرة - جاري تثبيت Chromium، يرجى الانتظار...")

    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium", "chromium-headless-shell"],
        timeout=900,
    )
    return await p.chromium.launch(**args)


# ==================== أدوات اعتراض API ====================

def _find_offer_objects(data, depth=0):
    if depth > 6:
        return []
    results = []
    if isinstance(data, dict):
        keys_str = " ".join(str(k).lower() for k in data.keys())
        if any(k in keys_str for k in ("bundle", "offer", "plan", "pack", "forfait", "promo")):
            if any(k in data for k in ("bundleCode", "offerCode", "code", "name", "bundleName", "label")):
                results.append(data)
        for v in data.values():
            results.extend(_find_offer_objects(v, depth + 1))
    elif isinstance(data, list):
        for item in data:
            results.extend(_find_offer_objects(item, depth + 1))
    return results


async def _auto_scroll(page, steps=6, delay=1.5):
    for _ in range(steps):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await asyncio.sleep(delay)
        except Exception:
            break
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


# ==================== تقرير مساعد ====================

def _build_report(msisdn, captured_by_endpoint, captured_offers, click_results, mode_label):
    lines = [f"📱 الرقم: {mask_msisdn(msisdn)}", f"🧪 الطريقة: {mode_label}\n"]

    if captured_by_endpoint:
        lines.append(f"🎯 تم اعتراض {len(captured_by_endpoint)} endpoint:")
        for ep, items in captured_by_endpoint.items():
            lines.append(f"  • `{ep}` → {len(items)} استجابة")
    else:
        lines.append("⚠️ لم يتم اعتراض أي endpoint.")

    if captured_offers:
        lines.append(f"\n📦 {len(captured_offers)} كائن عرض/باقة:")
        for o in captured_offers[:15]:
            name = o.get("name") or o.get("bundleName") or o.get("label") or str(o)[:80]
            code = o.get("code") or o.get("bundleCode") or ""
            price = o.get("price") or o.get("amount") or o.get("grantValue") or ""
            lines.append(f"  • {name} {f'[{code}]' if code else ''} {f'💰{price}' if price else ''}")
        if len(captured_offers) > 15:
            lines.append(f"  ... +{len(captured_offers)-15} آخر (راجع الملف)")

    if click_results:
        lines.append(f"\n🖱️ {len(click_results)} عنصر تم النقر عليه:")
        for r in click_results[:10]:
            icon = "✅" if r.get("clicked") else "❌"
            lines.append(f"  {icon} {r['text']}")
        if len(click_results) > 10:
            lines.append(f"  ... +{len(click_results)-10} آخر")

    snapchat_found = any(
        "snap" in json.dumps(o, ensure_ascii=False).lower() for o in captured_offers
    )
    if not snapchat_found:
        lines.append("\nℹ️ لم أجد حقل Snapchat في البيانات المُعترضة.")

    return "\n".join(lines)


# ==================== (1) وضع الويب: Playwright ====================

async def process_offers_via_web(msg):
    cookies = parse_cookies_file(COOKIES_FILE)
    if not cookies:
        return "❌ ملف cookies.txt غير موجود أو فارغ."

    msisdn = get_msisdn_from_cookies(cookies)
    if not msisdn:
        return "❌ لم أجد msisdn في cookies.txt."

    captured_by_endpoint = {}
    captured_offers = []
    click_results = []
    browser = None

    try:
        async with async_playwright() as p:
            await safe_edit(msg, "🌐 [Web] تشغيل المتصفح...")
            browser = await launch_browser(p, msg)

            ctx = await browser.new_context(
                viewport={"width": 412, "height": 915},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                locale="ar-DZ",
            )
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()

            async def on_response(response):
                try:
                    url = response.url
                    if "apis.ooredoo.dz" not in url:
                        return
                    matched_endpoint = None
                    for pattern in TARGET_API_PATTERNS:
                        if pattern in url:
                            matched_endpoint = pattern
                            break
                    if not matched_endpoint:
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    data = await response.json()
                    captured_by_endpoint.setdefault(matched_endpoint, []).append({
                        "url": url,
                        "status": response.status,
                        "data": data,
                    })
                    for f in _find_offer_objects(data):
                        if f not in captured_offers:
                            captured_offers.append(f)
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            try:
                await page.goto(MY_OOREDOO_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception as e:
                return f"❌ فشل فتح My Ooredoo: {e}"

            await asyncio.sleep(8)

            for label in ["تخطي", "إغلاق", "Fermer", "Skip", "Got it", "حسناً", "موافق"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=2000)
                        await asyncio.sleep(1)
                except Exception:
                    pass

            login_btn = page.get_by_role(
                "button", name=re.compile("تسجيل الدخول|Se connecter|Login", re.I)
            )
            if await login_btn.count() > 0 and await login_btn.first.is_visible():
                return "❌ الكوكيز منتهية الصلاحية."

            await safe_edit(msg, "🔍 [Web] اعتراض API + تمرير...")
            await _auto_scroll(page, steps=8, delay=1.5)

            for tab_text in [
                "العروض", "Offres", "Bundles", "باقات", "اشتراكات",
                "Internet", "CVM", "Loyalty", "Gamification",
            ]:
                try:
                    tab = page.get_by_text(re.compile(tab_text, re.I)).first
                    if await tab.count() > 0:
                        await tab.click(timeout=3000, force=True)
                        await asyncio.sleep(3)
                        await _auto_scroll(page, steps=3, delay=1.0)
                except Exception:
                    pass

            click_results = await page.evaluate("""() => {
                const results = [];
                const keywords = [
                    /2\\s*[gG](?:[oO]|[bB])(?!\\w)/,
                    /2\\s*جيجا/,
                    /2\\s*جيغا/,
                    /snapchat|سناب/i,
                ];
                const all = document.querySelectorAll('*');
                const seen = new Set();
                for (const el of all) {
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 150) continue;
                    let matched = false;
                    for (const kw of keywords) { if (kw.test(txt)) { matched = true; break; } }
                    if (!matched) continue;
                    let clickable = el;
                    for (let i = 0; i < 5; i++) {
                        if (!clickable) break;
                        const tag = clickable.tagName;
                        if (tag === 'A' || tag === 'BUTTON' ||
                            clickable.getAttribute('role') === 'button' ||
                            clickable.onclick) break;
                        clickable = clickable.parentElement;
                    }
                    if (!clickable) continue;
                    const key = txt.slice(0, 80);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    try {
                        clickable.scrollIntoView({block: 'center'});
                        clickable.click();
                        results.push({text: key, clicked: true});
                    } catch(e) {
                        results.push({text: key, clicked: false, error: String(e)});
                    }
                }
                return results;
            }""")

            await asyncio.sleep(5)
            await browser.close()
            browser = None

            dump = {
                "mode": "web_playwright",
                "msisdn_masked": mask_msisdn(msisdn),
                "timestamp": datetime.now().isoformat(),
                "endpoints_captured": {
                    ep: [{"url": x["url"], "status": x["status"]} for x in items]
                    for ep, items in captured_by_endpoint.items()
                },
                "endpoints_full_data": captured_by_endpoint,
                "offers_extracted": captured_offers,
                "click_attempts": click_results,
            }
            try:
                with open(OFFERS_DUMP, "w", encoding="utf-8") as f:
                    json.dump(dump, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            return _build_report(msisdn, captured_by_endpoint, captured_offers, click_results, "Web (Playwright)")

    except Exception as e:
        log.exception("فشل وضع الويب")
        return f"❌ خطأ في وضع الويب:\n{str(e)[:600]}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ==================== (2) وضع Android Native: Appium ====================

def _run_appium_session_sync(msg_text_ref):
    """
    تشغيل جلسة Appium في thread منفصل (لأنه sync).
    ترجع قاموساً بنتائج الجلسة.
    """
    if not APPIUM_AVAILABLE:
        return {"error": "Appium غير مثبت. نفّذ: pip install Appium-Python-Client"}

    result = {
        "screens_visited": [],
        "texts_found": [],
        "click_attempts": [],
        "error": None,
    }

    options = UiAutomator2Options()
    options.platform_name = "Android"
    if DEVICE_UDID:
        options.device_name = DEVICE_UDID
        options.udid = DEVICE_UDID
    options.app_package = APP_PACKAGE
    options.app_activity = APP_ACTIVITY
    options.no_reset = False
    options.full_reset = False
    options.auto_grant_permissions = True
    options.new_command_timeout = 300

    driver = None
    try:
        driver = appium_webdriver.Remote(
            f"http://{APPIUM_HOST}:{APPIUM_PORT}", options=options
        )
        wait = WebDriverWait(driver, 20)
        time.sleep(5)
        result["screens_visited"].append("launch")

        # إغلاق النوافذ المنبثقة
        popup_texts = ["Skip", "Fermer", "تخطي", "إغلاق", "Plus tard", "حسناً", "موافق"]
        for text in popup_texts:
            try:
                btn = driver.find_element(AppiumBy.XPATH, f"//*[@text='{text}']")
                if btn.is_displayed():
                    btn.click()
                    time.sleep(2)
                    result["screens_visited"].append(f"popup_closed:{text}")
            except Exception:
                pass

        # زيارة التبويبات المحتملة
        possible_tabs = [
            "Offres", "Bundles", "Forfaits", "Internet", "باقات", "عروض",
            "My Offers", "CVM", "Loyalty", "Gamification", "Snapchat", "2GB",
        ]
        for tab_name in possible_tabs:
            try:
                el = driver.find_element(AppiumBy.XPATH, f"//*[contains(@text, '{tab_name}')]")
                if el.is_displayed():
                    el.click()
                    result["screens_visited"].append(f"tab:{tab_name}")
                    time.sleep(4)
            except Exception:
                continue

        # استخراج النصوص
        page_source = driver.page_source
        keywords = ["2GB", "2 Go", "Snapchat", "DA", "Prix", "Offre", "Bundle", "باقة", "عرض"]
        found = set()
        for line in page_source.split("\n"):
            for kw in keywords:
                if kw.lower() in line.lower() and len(line.strip()) < 200:
                    found.add(line.strip())
                    break
        result["texts_found"] = list(found)[:100]

        # محاولة النقر على 2GB/Snapchat
        for kw in ["2GB", "2 Go", "Snapchat"]:
            try:
                el = driver.find_element(AppiumBy.XPATH, f"//*[contains(@text, '{kw}')]")
                if el.is_displayed():
                    el.click()
                    result["click_attempts"].append({"text": kw, "clicked": True})
                    time.sleep(3)
            except Exception as e:
                result["click_attempts"].append({"text": kw, "clicked": False, "error": str(e)})

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log.exception("فشل جلسة Appium")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


async def process_offers_via_appium(msg):
    if not APPIUM_AVAILABLE:
        return (
            "❌ Appium غير مثبت على الخادم.\n"
            "نفّذ: `pip install Appium-Python-Client`\n"
            "وتأكد من تشغيل Appium Server على المنفذ 4723."
        )

    await safe_edit(msg, "🤖 [Android] بدء جلسة Appium...")

    # نستخدم run_in_executor لأن Appium sync
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run_appium_session_sync, None)
    except Exception as e:
        log.exception("فشل run_in_executor")
        return f"❌ خطأ Appium: {e}"

    # حفظ dump
    try:
        with open(APPIUM_DUMP, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if result.get("error"):
        return f"❌ خطأ Appium:\n{result['error'][:600]}"

    lines = ["📱 الطريقة: Android Native (Appium)\n"]
    sv = result.get("screens_visited", [])
    lines.append(f"🗺️ الشاشات التي تم زيارتها: {len(sv)}")
    for s in sv[:10]:
        lines.append(f"  • {s}")

    tf = result.get("texts_found", [])
    if tf:
        lines.append(f"\n📄 نصوص محتملة للعروض ({len(tf)}):")
        for t in tf[:15]:
            lines.append(f"  • {t[:120]}")
    else:
        lines.append("\n⚠️ لم يتم العثور على نصوص عروض واضحة.")

    ca = result.get("click_attempts", [])
    if ca:
        lines.append(f"\n🖱️ محاولات نقر:")
        for c in ca:
            icon = "✅" if c.get("clicked") else "❌"
            lines.append(f"  {icon} {c.get('text')}")

    return "\n".join(lines)


# ==================== (3) الوضع المزدوج (تلقائي) ====================

async def process_dual_mode(msg):
    """
    يشغّل الويب أولاً، وإذا لم يُعترض أي endpoint، يجرّب Appium.
    """
    web_result = await process_offers_via_web(msg)

    # نفحص إذا كان الـ dump يحتوي endpoints
    has_data = False
    try:
        if os.path.exists(OFFERS_DUMP):
            with open(OFFERS_DUMP, "r", encoding="utf-8") as f:
                d = json.load(f)
            has_data = len(d.get("endpoints_captured", {})) > 0
    except Exception:
        pass

    if has_data:
        return web_result

    # لم تُعترض بيانات من الويب → نجرب Appium
    await safe_edit(msg, "⚠️ الويب لم يعترض أي API. تجربة Android Native (Appium)...")
    appium_result = await process_offers_via_appium(msg)
    return f"{web_result}\n\n{'='*30}\n\n{appium_result}"


# ==================== أوامر البوت ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    appium_status = "✅ مثبت" if APPIUM_AVAILABLE else "❌ غير مثبت"
    await update.message.reply_text(
        "🤖 **بوت تفعيل عروض أوريدو**\n\n"
        "**الأوامر:**\n"
        "/activate — الوضع المزدوج (Web ثم Android تلقائياً)\n"
        "/activate_web — وضع الويب فقط (Playwright)\n"
        "/activate_android — وضع Android Native (Appium)\n"
        "/status — حالة الكوكيز والملفات\n"
        "/dumpsize — ملخص آخر جلب\n\n"
        f"🖥️ **حالة Appium**: {appium_status}\n"
        f"📦 **الحزمة**: `{APP_PACKAGE}`\n"
        f"🎬 **النشاط**: `{APP_ACTIVITY}`\n\n"
        "⚠️ **لـ Android Native**: يجب تشغيل Appium Server + Emulator\n"
        "  `appium --port 4723`\n\n"
        "ℹ️ Snapchat/2GB عادةً مرتبطة بـ N'YOOZ + برنامج Powers of Snake.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = parse_cookies_file(COOKIES_FILE)
    msisdn = get_msisdn_from_cookies(cookies)

    def _dump_info(path):
        if not os.path.exists(path):
            return "❌ غير موجود"
        try:
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            mode = d.get("mode", "?")
            eps = len(d.get("endpoints_captured", {}))
            offers = len(d.get("offers_extracted", []))
            ts = d.get("timestamp", "")[:19]
            return f"✅ {size//1024} KB | {mode} | {eps} ep | {offers} offers | {ts}"
        except Exception:
            return "⚠️ موجود لكن غير قابل للقراءة"

    msg = (
        f"📄 cookies.txt: {'✅' if cookies else '❌'} ({len(cookies)} عنصر)\n"
        f"📱 msisdn: {mask_msisdn(msisdn) if msisdn else '❌'}\n"
        f"🖥️ Appium: {'✅' if APPIUM_AVAILABLE else '❌'}\n"
        f"🌐 offers_dump.json: {_dump_info(OFFERS_DUMP)}\n"
        f"🤖 appium_dump.json: {_dump_info(APPIUM_DUMP)}\n"
    )
    if LAST_ERROR:
        msg += f"\n❌ آخر خطأ:\n{LAST_ERROR[-300:]}"
    await update.message.reply_text(msg)


async def dumpsize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for path, label in [(OFFERS_DUMP, "🌐 Web"), (APPIUM_DUMP, "🤖 Appium")]:
        if not os.path.exists(path):
            await update.message.reply_text(f"{label}: ❌ غير موجود")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            lines = [f"{label} — {d.get('timestamp', '?')[:19]}"]
            if "endpoints_captured" in d:
                lines.append(f"🎯 Endpoints: {len(d['endpoints_captured'])}")
                for ep, items in list(d["endpoints_captured"].items())[:20]:
                    lines.append(f"  • {ep} → {len(items)}")
            if "screens_visited" in d:
                lines.append(f"🗺️ Screens: {len(d['screens_visited'])}")
            if "texts_found" in d:
                lines.append(f"📄 Texts: {len(d['texts_found'])}")
            if "offers_extracted" in d:
                lines.append(f"📦 Offers: {len(d['offers_extracted'])}")
            await update.message.reply_text("\n".join(lines)[:4000])
        except Exception as e:
            await update.message.reply_text(f"{label}: ❌ {e}")


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الوضع المزدوج - Web ثم Android"""
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية أخرى قيد التنفيذ...")
        return
    async with job_lock:
        msg = await update.message.reply_text("🚀 بدء الوضع المزدوج...")
        try:
            result = await process_dual_mode(msg)
        except Exception as e:
            log.exception("فشل activate")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)
        for path, name in [(OFFERS_DUMP, "offers_dump.json"), (APPIUM_DUMP, "appium_dump.json")]:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        await update.message.reply_document(f, filename=name)
                except Exception:
                    pass


async def activate_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية أخرى قيد التنفيذ...")
        return
    async with job_lock:
        msg = await update.message.reply_text("🌐 بدء وضع الويب...")
        try:
            result = await process_offers_via_web(msg)
        except Exception as e:
            log.exception("فشل activate_web")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)
        if os.path.exists(OFFERS_DUMP):
            try:
                with open(OFFERS_DUMP, "rb") as f:
                    await update.message.reply_document(f, filename="offers_dump.json")
            except Exception:
                pass


async def activate_android(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية أخرى قيد التنفيذ...")
        return
    async with job_lock:
        msg = await update.message.reply_text("🤖 بدء وضع Android Native (Appium)...")
        try:
            result = await process_offers_via_appium(msg)
        except Exception as e:
            log.exception("فشل activate_android")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)
        if os.path.exists(APPIUM_DUMP):
            try:
                with open(APPIUM_DUMP, "rb") as f:
                    await update.message.reply_document(f, filename="appium_dump.json")
            except Exception:
                pass


# ==================== التشغيل ====================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("dumpsize", dumpsize))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("activate_web", activate_web))
    app.add_handler(CommandHandler("activate_android", activate_android))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, activate))
    return app


def main():
    global LAST_ERROR
    print(f"Python {sys.version.split()[0]}")
    print(f"Appium available: {APPIUM_AVAILABLE}")

    for attempt in range(1, 4):
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            app = build_app()
            log.info("🤖 البوت جاهز - أرسل /activate للبدء")
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