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

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7261013113:AAFbRbjTcPFCuezmqKgG9sNxUEO8WsR_IvI")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
NUMBERS_FILE = os.path.join(BASE_DIR, "numbers.txt")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

# 浏览器持久化路径
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE_DIR, ".pw-browsers"))

MY_OOREDOO_URL = "https://my.ooredoo.dz/"

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("ooredoo-bot")

LAST_ERROR = ""
job_lock = asyncio.Lock()


# ==================== 工具函数 ====================

def parse_cookies_file(path: str):
    """读取 Netscape 格式 cookies.txt 为 Playwright 可用的列表"""
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
        log.warning(f"读取 cookies.txt 失败: {e}")
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
    return msisdn[:3] + "******" + msisdn[-2:] if msisdn and len(msisdn) > 6 else "******"


async def safe_edit(msg, text: str):
    try:
        await msg.edit_text(text[:4000])
    except Exception:
        try:
            await msg.reply_text(text[:4000])
        except Exception:
            pass


# ==================== 浏览器管理 ====================

async def launch_browser(p, msg=None):
    args = dict(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
    try:
        return await p.chromium.launch(**args)
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
    if msg:
        await safe_edit(msg, "⬇️ 首次运行，正在安装 Chromium，请稍候...")
    import subprocess
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "chromium-headless-shell"],
                   timeout=900)
    return await p.chromium.launch(**args)


# ==================== 核心：网页爬取与点击 ====================

async def process_offers_via_web(msg):
    """
    通过 Playwright 登录 My Ooredoo，抓取页面所有可点击的 offer 卡片，
    识别包含 "2Go" / "2GB" / "Snapchat" 的卡片并点击激活。
    """
    cookies = parse_cookies_file(COOKIES_FILE)
    if not cookies:
        return "❌ cookies.txt 不存在或为空，请先从 my.ooredoo.dz 导出。"

    msisdn = get_msisdn_from_cookies(cookies)
    if not msisdn:
        return "❌ cookies.txt 中未找到 msisdn，无法识别用户号码。"

    browser = None
    try:
        async with async_playwright() as p:
            await safe_edit(msg, "🌐 正在启动浏览器并加载 My Ooredoo...")
            browser = await launch_browser(p, msg)

            ctx = await browser.new_context(
                viewport={"width": 412, "height": 915},
                user_agent=("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
                locale="ar-DZ",
            )
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()

            # 打开首页
            try:
                await page.goto(MY_OOREDOO_URL, timeout=90000, wait_until="domcontentloaded")
            except Exception as e:
                return f"❌ 无法打开 My Ooredoo: {e}"

            # 等待页面动态内容加载（Spa / React）
            await asyncio.sleep(8)

            # 关闭可能的弹窗 / 引导遮罩
            for label in ["تخطي", "إغلاق", "Fermer", "Skip", "Got it", "حسناً"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=2000)
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # 确保已登录（检查是否有登录按钮，若有说明 cookies 失效）
            login_btn = page.get_by_role("button", name=re.compile("تسجيل الدخول|Se connecter|Login", re.I))
            if await login_btn.count() > 0 and await login_btn.first.is_visible():
                return "❌ Cookies 已失效，请重新导出 cookies.txt（页面要求登录）。"

            await safe_edit(msg, "🔍 正在扫描页面上的所有 offer...")

            # 收集所有可能包含 offer 的元素
            # 策略：查找所有 a / button / div[role=button]，提取文本，判断是否包含关键词
            raw_candidates = await page.evaluate("""() => {
                const out = [];
                const els = document.querySelectorAll('a, button, [role="button"], .offer-card, .bundle-card, [class*="offer"], [class*="bundle"]');
                const seen = new Set();
                for (const el of els) {
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 200) continue; // 过滤掉大段文本
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

            # 关键词匹配
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
                return ("⚠️ 在页面上未找到包含 "2GB" 或 "Snapchat" 的可点击 offer。\n"
                        "可能原因：\n"
                        "1. 这些 offer 只对特定用户可见（不满足资格）\n"
                        "2. 页面需要滚动/切换 Tab 才显示\n"
                        "3. 页面结构与预期不同\n\n"
                        f"当前页面标题：{await page.title()}")

            # 点击每个匹配的 offer
            results = []
            for t in targets:
                label = t["text"]
                try:
                    # 使用 get_by_text 精确定位（exact=False 以容忍前后空格）
                    locator = page.get_by_text(label, exact=False).first
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    await locator.click(timeout=10000)
                    await asyncio.sleep(3)

                    # 处理可能的确认弹窗
                    for confirm_label in ["تأكيد", "تفعيل", "موافق", "Confirmer", "Activer", "OK"]:
                        try:
                            cbtn = page.get_by_role("button", name=re.compile(confirm_label, re.I)).first
                            if await cbtn.is_visible(timeout=1500):
                                await cbtn.click(timeout=3000)
                                await asyncio.sleep(2)
                        except Exception:
                            pass

                    results.append(f"✅ 已点击: {label[:60]}  →  已尝试激活")
                    await asyncio.sleep(2)
                except Exception as e:
                    results.append(f"❌ 点击失败: {label[:60]}  →  {str(e)[:100]}")

            await browser.close()
            browser = None

            header = f"📱 号码: {mask_msisdn(msisdn)}\n\n"
            return header + "\n\n".join(results)

    except Exception as e:
        log.exception("web job failed")
        return f"❌ 执行出错:\n{str(e)[:600]}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ==================== Bot 命令 ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **بوت تفعيل عروض أوريدو (محاكاة الويب)**\n\n"
        "يقوم البوت بـ:\n"
        "1. الدخول إلى My Ooredoo باستخدام `cookies.txt`\n"
        "2. تحليل جميع العروض المعروضة على الصفحة\n"
        "3. البحث عن **2GB المجاني** و **Snapchat المجاني** والنقر عليهما\n\n"
        "📌 الأوامر:\n"
        "/activate — بدء المسح والتفعيل\n"
        "/status — التحقق من حالة cookies\n\n"
        "⚠️ **ملاحظة**: Snapchat المجاني عادةً مرتبط بباقة N'YOOZ، "
        "إذا لم يظهر في الصفحة فهذا يعني أن حسابك غير مؤهل حالياً.",
        parse_mode="Markdown"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = parse_cookies_file(COOKIES_FILE)
    msisdn = get_msisdn_from_cookies(cookies)
    msg = (
        f"📄 cookies.txt: {'✅' if cookies else '❌'} ({len(cookies)} entries)\n"
        f"📱 msisdn: {mask_msisdn(msisdn) if msisdn else '❌ غير موجود'}\n"
    )
    if LAST_ERROR:
        msg += f"\n❌ آخر خطأ:\n{LAST_ERROR[-300:]}"
    await update.message.reply_text(msg)


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if job_lock.locked():
        await update.message.reply_text("⏳ 操作进行中，请稍候...")
        return
    async with job_lock:
        msg = await update.message.reply_text("🚀 开始扫描 My Ooredoo 页面...")
        try:
            result = await process_offers_via_web(msg)
        except Exception as e:
            log.exception("activate failed")
            result = f"❌ 错误: {e}"
        await safe_edit(msg, result)


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
            log.info("🤖 Bot 已启动，发送 /activate 开始")
            app.run_polling(close_loop=False)
            return
        except Exception:
            LAST_ERROR = traceback.format_exc()
            log.error(f"Bot 停止 (尝试 {attempt}/3):\n{LAST_ERROR}")
            if attempt == 3:
                raise
            time.sleep(5 * attempt)


if __name__ == "__main__":
    main()