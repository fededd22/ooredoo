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
OFFERS_DUMP = os.path.join(BASE_DIR, "offers_dump.json")

# مسار دائم لمتصفح Playwright
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE_DIR, ".pw-browsers"))

MY_OOREDOO_URL = "https://my.ooredoo.dz/"

# ==================== المسارات المستهدفة (مستخرجة من pp.txt) ====================
TARGET_API_PATTERNS = [
    # مسارات الباقات الأساسية
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
    # مسارات CVM
    "/cvm/getBundles",
    "/cvm/bundlePurchase",
    "/cvm/dynamic-bundle-purchase",
    "/cvm/status",
    "/cvm/cvm_optINOUT",
    # مسارات الأعمال
    "/gamification/play",
    "/gamification/status",
    "/gamification/power-snake",
    "/gamification/gta-bundle",
    "/gamification/history",
    "/power-snake/",
    # CMS (للحصول على تفاصيل الباقات)
    "/cms/v2/api/product-offerings",
    "/cms/v2/api/countries",
    # مسارات الدفع
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


# ==================== أدوات اعتراض API والبحث عن العروض ====================

def _find_offer_objects(data, depth=0):
    """البحث التكراري في JSON عن كائنات تشبه العروض/الباقات"""
    if depth > 6:
        return []
    results = []
    if isinstance(data, dict):
        keys_str = " ".join(str(k).lower() for k in data.keys())
        # البحث عن كائنات تحتوي على مفاتيح مميزة للعروض
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
    """تمرير تدريجي لتحفيز تحميل العناصر الكسولة"""
    for i in range(steps):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await asyncio.sleep(delay)
        except Exception:
            break
    # العودة إلى الأعلى
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


# ==================== المنطق الأساسي ====================

async def process_offers_via_web(msg):
    """
    اعتراض استجابات API في My Ooredoo، التقاط جميع بيانات العروض،
    محاولة النقر على عناصر 2GB/Snapchat (بما في ذلك العناصر المخفية).
    """
    cookies = parse_cookies_file(COOKIES_FILE)
    if not cookies:
        return "❌ ملف cookies.txt غير موجود أو فارغ. يرجى تصديره من my.ooredoo.dz أولاً."

    msisdn = get_msisdn_from_cookies(cookies)
    if not msisdn:
        return "❌ لم أجد msisdn في cookies.txt. تأكد من وجود cookie باسم msisdn أو x-msisdn."

    captured_by_endpoint = {}   # {endpoint: [data...]}
    captured_offers = []        # قائمة العروض المستخرجة
    click_results = []          # نتائج محاولة النقر
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

            # ===== اعتراض استجابات API المطابقة للمسارات المستهدفة =====
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

                    # استخراج كائنات العروض
                    found = _find_offer_objects(data)
                    for f in found:
                        if f not in captured_offers:
                            captured_offers.append(f)
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            # فتح الصفحة الرئيسية
            try:
                await page.goto(MY_OOREDOO_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception as e:
                return f"❌ فشل فتح My Ooredoo: {e}"

            await asyncio.sleep(8)

            # إغلاق النوافذ المنبثقة
            for label in ["تخطي", "إغلاق", "Fermer", "Skip", "Got it", "حسناً", "موافق"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=2000)
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # التحقق من تسجيل الدخول
            login_btn = page.get_by_role(
                "button", name=re.compile("تسجيل الدخول|Se connecter|Login", re.I)
            )
            if await login_btn.count() > 0 and await login_btn.first.is_visible():
                return "❌ الكوكيز منتهية الصلاحية. يرجى إعادة تصدير cookies.txt."

            await safe_edit(msg, "🔍 جاري اعتراض بيانات API + التمرير لتحميل العناصر المخفية...")

            # ===== التمرير التلقائي لتحميل العناصر الكسولة =====
            await _auto_scroll(page, steps=8, delay=1.5)

            # ===== زيارة تبويبات مهمة لتحفيز كل endpoints =====
            for tab_text in [
                "العروض", "Offres", "Bundles", "باقات", "اشتراكات",
                "Internet", "CVM", "Loyalty", "الأعمال", "Gamification",
            ]:
                try:
                    tab = page.get_by_text(re.compile(tab_text, re.I)).first
                    if await tab.count() > 0:
                        await tab.click(timeout=3000, force=True)
                        await asyncio.sleep(3)
                        await _auto_scroll(page, steps=3, delay=1.0)
                except Exception:
                    pass

            # ===== محاولة النقر على عناصر 2GB/Snapchat عبر JS =====
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

                    // البحث عن أقرب عنصر قابل للنقر
                    let clickable = el;
                    for (let i = 0; i < 5; i++) {
                        if (!clickable) break;
                        const tag = clickable.tagName;
                        if (tag === 'A' || tag === 'BUTTON' ||
                            clickable.getAttribute('role') === 'button' ||
                            clickable.onclick) {
                            break;
                        }
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

            # ===== حفظ Dump كامل للتحليل اليدوي =====
            dump = {
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

            # ===== بناء التقرير =====
            lines = [f"📱 الرقم: {mask_msisdn(msisdn)}\n"]

            if captured_by_endpoint:
                lines.append(f"🎯 تم اعتراض {len(captured_by_endpoint)} endpoint:")
                for ep, items in captured_by_endpoint.items():
                    lines.append(f"  • `{ep}` → {len(items)} استجابة")
            else:
                lines.append(
                    "⚠️ لم يتم اعتراض أي endpoint. الحساب قد لا يملك باقات "
                    "أو الصفحة تحتاج تفاعل أكثر."
                )

            if captured_offers:
                lines.append(f"\n📦 {len(captured_offers)} كائن عرض/باقة:")
                for o in captured_offers[:15]:
                    name = (
                        o.get("name") or o.get("bundleName")
                        or o.get("label") or str(o)[:80]
                    )
                    code = o.get("code") or o.get("bundleCode") or ""
                    price = (
                        o.get("price") or o.get("amount")
                        or o.get("grantValue") or ""
                    )
                    lines.append(
                        f"  • {name} "
                        f"{f'[{code}]' if code else ''} "
                        f"{f'💰{price}' if price else ''}"
                    )
                if len(captured_offers) > 15:
                    lines.append(
                        f"  ... +{len(captured_offers) - 15} آخر (راجع الملف)"
                    )
            else:
                lines.append(
                    "\n📦 لم يتم استخراج كائنات عرض. راجع "
                    "`offers_dump.json` للبيانات الخام."
                )

            if click_results:
                lines.append(f"\n🖱️ {len(click_results)} عنصر تم النقر عليه:")
                for r in click_results[:10]:
                    icon = "✅" if r.get("clicked") else "❌"
                    lines.append(f"  {icon} {r['text']}")
                if len(click_results) > 10:
                    lines.append(f"  ... +{len(click_results) - 10} آخر")

            # تحذير صريح عن Snapchat/2GB
            snapchat_found = any(
                "snap" in json.dumps(o, ensure_ascii=False).lower()
                for o in captured_offers
            )
            if not snapchat_found:
                lines.append(
                    "\nℹ️ لم أجد حقل Snapchat في البيانات المُعترضة. "
                    "عرض Snapchat المجاني عادةً مرتبط بباقة N'YOOZ المحددة."
                )

            return "\n".join(lines)

    except Exception as e:
        log.exception("فشل تنفيذ عملية الويب")
        return f"❌ حدث خطأ:\n{str(e)[:600]}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ==================== أوامر البوت ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **بوت تفعيل عروض أوريدو (اعتراض API + محاكاة ويب)**\n\n"
        "يقوم البوت بـ:\n"
        "1. الدخول إلى My Ooredoo باستخدام `cookies.txt`\n"
        "2. **اعتراض استجابات API** من المسارات المستهدفة:\n"
        "   • `/bundle/getActivePackages`\n"
        "   • `/cvm/getBundles`\n"
        "   • `/bundle/bundlePurchase/v2`\n"
        "   • `/gamification/play`\n"
        "   • `/cms/v2/api/product-offerings`\n"
        "   • وغيرها...\n"
        "3. التمرير التلقائي + زيارة التبويبات لتحفيز تحميل البيانات\n"
        "4. النقر التلقائي على عناصر 2GB/Snapchat (حتى المخفية)\n"
        "5. تصدير كل العروض المكتشفة لملف `offers_dump.json`\n\n"
        "📌 الأوامر:\n"
        "/activate — بدء المسح والتفعيل\n"
        "/status — التحقق من حالة cookies\n"
        "/dumpsize — حجم ملف العروض المُصدَّر\n\n"
        "⚠️ **ملاحظة**: عرض Snapchat المجاني ضمن باقة N'YOOZ، "
        "وعرض 2GB المجاني مكافأة من برنامج Powers of Snake.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = parse_cookies_file(COOKIES_FILE)
    msisdn = get_msisdn_from_cookies(cookies)

    dump_info = "❌ غير موجود بعد"
    if os.path.exists(OFFERS_DUMP):
        try:
            size = os.path.getsize(OFFERS_DUMP)
            with open(OFFERS_DUMP, "r", encoding="utf-8") as f:
                d = json.load(f)
            eps = len(d.get("endpoints_captured", {}))
            offers = len(d.get("offers_extracted", []))
            dump_info = (
                f"✅ {size//1024} KB | "
                f"{eps} endpoint | "
                f"{offers} عرض | "
                f"{d.get('timestamp', '')[:19]}"
            )
        except Exception:
            dump_info = "⚠️ موجود لكن غير قابل للقراءة"

    msg = (
        f"📄 cookies.txt: {'✅' if cookies else '❌'} ({len(cookies)} عنصر)\n"
        f"📱 msisdn: {mask_msisdn(msisdn) if msisdn else '❌ غير موجود'}\n"
        f"📦 offers_dump.json: {dump_info}\n"
    )
    if LAST_ERROR:
        msg += f"\n❌ آخر خطأ:\n{LAST_ERROR[-300:]}"
    await update.message.reply_text(msg)


async def dumpsize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إرسال ملخص سريع عن محتوى offers_dump.json"""
    if not os.path.exists(OFFERS_DUMP):
        await update.message.reply_text("❌ لم يتم إنشاء offers_dump.json بعد. استخدم /activate أولاً.")
        return
    try:
        with open(OFFERS_DUMP, "r", encoding="utf-8") as f:
            d = json.load(f)
        eps = d.get("endpoints_captured", {})
        lines = [
            f"📅 آخر جلب: {d.get('timestamp', '?')[:19]}",
            f"📱 الرقم: {d.get('msisdn_masked', '?')}",
            f"🎯 Endpoints: {len(eps)}",
            f"📦 عروض مستخرجة: {len(d.get('offers_extracted', []))}",
            f"🖱️ محاولات نقر: {len(d.get('click_attempts', []))}",
            "",
            "المسارات المُعترضة:",
        ]
        for ep, items in eps.items():
            lines.append(f"  • {ep} → {len(items)}")
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ فشل قراءة الملف: {e}")


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

        # إرسال ملف العروض إذا وُجد
        if os.path.exists(OFFERS_DUMP):
            try:
                with open(OFFERS_DUMP, "rb") as f:
                    await update.message.reply_document(f, filename="offers_dump.json")
            except Exception:
                pass


# ==================== التشغيل ====================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("dumpsize", dumpsize))
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
