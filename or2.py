import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from playwright.async_api import async_playwright

# ==================== الإعدادات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7261013113:AAFbRbjTcPFCuezmqKgG9sNxUEO8WsR_IvI")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
NUMBERS_FILE = os.path.join(BASE_DIR, "numbers.txt")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

# مسار دائم لمتصفح Playwright
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE_DIR, ".pw-browsers"))

MY_OOREDOO_URL = "https://my.ooredoo.dz/"

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
    """استخراج رقم الهاتف من الكوكيز"""
    for c in cookies:
        if c["name"].lower() in ("msisdn", "x-msisdn"):
            raw = re.sub(r"\D", "", c["value"] or "")
            if raw.startswith("0") and len(raw) == 10:
                raw = "213" + raw[1:]
            if re.match(r"^213[567]\d{8}$", raw):
                return raw
    return None


def mask_msisdn(msisdn):
    """إخفاء جزء من الرقم للعرض"""
    if msisdn and len(msisdn) > 6:
        return msisdn[:3] + "******" + msisdn[-2:]
    return "******"


async def safe_edit(msg, text: str):
    """تعديل رسالة بأمان مع تجاهل الأخطاء"""
    try:
        await msg.edit_text(text[:4000])
    except Exception:
        try:
            await msg.reply_text(text[:4000])
        except Exception:
            pass


# ==================== إدارة المتصفح ====================

async def launch_browser(p, msg=None):
    """تشغيل المتصفح مع تثبيت تلقائي عند الحاجة"""
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

    import subprocess
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium", "chromium-headless-shell"],
        timeout=900,
    )
    return await p.chromium.launch(**args)


# ==================== المنطق الأساسي: زحف الصفحة والنقر ====================

async def process_offers_via_web(msg):
    """
    الدخول إلى My Ooredoo باستخدام Playwright،
    تحليل جميع بطاقات العروض في الصفحة،
    البحث عن "2GB" و "Snapchat" والنقر عليها لتفعيلها.
    """
    cookies = parse_cookies_file(COOKIES_FILE)
    if not cookies:
        return "❌ ملف cookies.txt غير موجود أو فارغ. يرجى تصديره من my.ooredoo.dz أولاً."

    msisdn = get_msisdn_from_cookies(cookies)
    if not msisdn:
        return "❌ لم أجد msisdn في cookies.txt. تأكد من وجود cookie باسم msisdn أو x-msisdn."

    browser = None
    try:
        async with async_playwright() as p:
            await safe_edit(msg, "🌐 جاري تشغيل المتصفح وتحميل My Ooredoo...")
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

            # فتح الصفحة الرئيسية
            try:
                await page.goto(MY_OOREDOO_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception as e:
                return f"❌ فشل فتح My Ooredoo: {e}"

            # انتظار تحميل المحتوى الديناميكي (SPA/React)
            await asyncio.sleep(8)

            # إغلاق أي نوافذ منبثقة أو أدلة ترحيبية
            for label in ["تخطي", "إغلاق", "Fermer", "Skip", "Got it", "حسناً", "موافق"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=2000)
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # التحقق من حالة تسجيل الدخول
            login_btn = page.get_by_role(
                "button", name=re.compile("تسجيل الدخول|Se connecter|Login", re.I)
            )
            if await login_btn.count() > 0 and await login_btn.first.is_visible():
                return "❌ الكوكيز منتهية الصلاحية. يرجى إعادة تصدير cookies.txt (الصفحة تطلب تسجيل الدخول)."

            await safe_edit(msg, "🔍 جاري مسح جميع العروض في الصفحة...")

            # جمع كل العناصر القابلة للنقر المحتملة
            raw_candidates = await page.evaluate("""() => {
                const out = [];
                const els = document.querySelectorAll(
                    'a, button, [role="button"], .offer-card, .bundle-card, ' +
                    '[class*="offer"], [class*="bundle"], [class*="card"]'
                );
                const seen = new Set();
                for (const el of els) {
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 200) continue;
                    if (seen.has(txt)) continue;
                    seen.add(txt);
                    out.push({
                        text: txt,
                        tag: el.tagName,
                        cls: el.className || '',
                        id: el.id || '',
                    });
                }
                return out;
            }""")

            # مطابقة الكلمات المفتاحية
            keywords = {
                "2gb": re.compile(r"2\s*[gG](?:[oO]|[bB])|2\s*جيجا|2\s*جيغا", re.I),
                "snapchat": re.compile(r"snapchat|سناب", re.I),
            }

            targets = []
            for cand in raw_candidates:
                text = cand["text"]
                matched = []
                for k, pat in keywords.items():
                    if pat.search(text):
                        matched.append(k)
                if matched:
                    targets.append({"text": text[:100], "keys": matched, "raw": cand})

            if not targets:
                return (
                    "⚠️ لم أجد أي عرض قابل للنقر يحتوي على '2GB' أو 'Snapchat' في الصفحة.\n"
                    "الأسباب المحتملة:\n"
                    "1. هذه العروض تظهر فقط لمستخدمين مؤهلين (شروط الأهلية غير متوفرة)\n"
                    "2. يجب التمرير أو تغيير التبويب لإظهار العروض\n"
                    "3. بنية الصفحة مختلفة عن المتوقع\n\n"
                    f"عنوان الصفحة الحالي: {await page.title()}"
                )

            await safe_edit(
                msg,
                f"🎯 وجدت {len(targets)} عرض مطابق. جاري النقر على كل عرض..."
            )

            # النقر على كل عرض مطابق
            results = []
            for t in targets:
                label = t["text"]
                try:
                    locator = page.get_by_text(label, exact=False).first
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    await locator.click(timeout=10000)
                    await asyncio.sleep(3)

                    # معالجة نوافذ التأكيد المحتملة
                    for confirm_label in ["تأكيد", "تفعيل", "موافق", "Confirmer", "Activer", "OK"]:
                        try:
                            cbtn = page.get_by_role(
                                "button", name=re.compile(confirm_label, re.I)
                            ).first
                            if await cbtn.is_visible(timeout=1500):
                                await cbtn.click(timeout=3000)
                                await asyncio.sleep(2)
                        except Exception:
                            pass

                    results.append(f"✅ تم النقر: {label[:60]}  →  محاولة التفعيل")
                    await asyncio.sleep(2)
                except Exception as e:
                    results.append(f"❌ فشل النقر: {label[:60]}  →  {str(e)[:100]}")

            await browser.close()
            browser = None

            header = f"📱 الرقم: {mask_msisdn(msisdn)}\n\n"
            return header + "\n\n".join(results)

    except Exception as e:
        log.exception("فشل تنفيذ عملية الويب")
        return f"❌ حدث خطأ أثناء التنفيذ:\n{str(e)[:600]}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ==================== أوامر البوت ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **بوت تفعيل عروض أوريدو (محاكاة الويب)**\n\n"
        "يقوم البوت بـ:\n"
        "1. الدخول إلى My Ooredoo باستخدام `cookies.txt`\n"
        "2. تحليل جميع العروض المعروضة على الصفحة\n"
        "3. البحث عن **عرض 2GB المجاني** و **Snapchat المجاني** والنقر عليهما\n\n"
        "📌 الأوامر:\n"
        "/activate — بدء المسح والتفعيل\n"
        "/status — التحقق من حالة cookies\n\n"
        "⚠️ **ملاحظة**: عرض Snapchat المجاني عادةً مرتبط بباقة N'YOOZ، "
        "إذا لم يظهر في الصفحة فهذا يعني أن حسابك غير مؤهل حالياً.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = parse_cookies_file(COOKIES_FILE)
    msisdn = get_msisdn_from_cookies(cookies)
    msg = (
        f"📄 cookies.txt: {'✅' if cookies else '❌'} ({len(cookies)} عنصر)\n"
        f"📱 msisdn: {mask_msisdn(msisdn) if msisdn else '❌ غير موجود'}\n"
    )
    if LAST_ERROR:
        msg += f"\n❌ آخر خطأ:\n{LAST_ERROR[-300:]}"
    await update.message.reply_text(msg)


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ عملية أخرى قيد التنفيذ، يرجى الانتظار...")
        return

    async with job_lock:
        msg = await update.message.reply_text("🚀 بدء مسح صفحة My Ooredoo...")
        try:
            result = await process_offers_via_web(msg)
        except Exception as e:
            log.exception("فشل أمر التفعيل")
            result = f"❌ خطأ: {e}"
        await safe_edit(msg, result)


# ==================== التشغيل ====================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, activate))
    return app


def main():
    global LAST_ERROR
    print(f"Python {sys.version.split()[0]}")

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