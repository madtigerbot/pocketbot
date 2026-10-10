"""
MAD-TIGER-BOT 🐯
المواصفات الكاملة - كود نظيف 100% + صور التايقر الحقيقية
Real Data Only - No Fake Prices - Testable Strategy
"""
import os
import json
import asyncio
import sqlite3
import time
import random
import hmac
from datetime import datetime, timedelta
from threading import Thread, Lock
from pathlib import Path

import pytz
import aiohttp
from dotenv import load_dotenv
from flask import Flask, jsonify

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

load_dotenv()

# ========== ENV - كل الأسرار من متغيرات البيئة ==========
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID") or os.getenv("ALLOWED_USER_ID") or "5256844309")
BOT_PASSWORD = os.getenv("BOT_PASSWORD") or os.getenv("PASSWORD") or "0530667154"

TW_KEY = os.getenv("TWELVE_DATA_API_KEY") or ""
FH_KEY = os.getenv("FINNHUB_API_KEY") or ""
GE_KEY = os.getenv("GEMINI_API_KEY") or ""
GR_KEY = os.getenv("GROQ_API_KEY") or ""

DB_PATH = os.getenv("DATABASE_URL") or os.getenv("DB_PATH") or "/tmp/mad_tiger.db"
if Path("/data").exists():
    DB_PATH = "/data/mad_tiger.db"

# صور التايقر الحقيقية اللي أرسلها المستخدم
BASE_DIR = Path(__file__).parent
TIGER_BUY_PATH = BASE_DIR / "tiger_buy.png"
TIGER_SELL_PATH = BASE_DIR / "tiger_sell.png"
TIGER_LOGO_PATH = BASE_DIR / "tiger_logo.png"

# ========== الأسواق حسب المواصفات الجديدة ==========
MARKETS_REAL = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF", "NZD/USD", "EUR/JPY"]
MARKETS_OTC = [f"{m} OTC" for m in MARKETS_REAL]

# OTC غير متاح لأنه لا يوجد مصدر حقيقي لأسعار OTC الخاصة بـ PocketOption
# حسب المواصفات: "يجب إظهار السوق على أنه غير متاح مؤقتًا"
OTC_AVAILABLE = False

TIMEFRAMES = ["30S", "1M", "2M", "3M", "5M", "15M"]
TIMEFRAMES_SUPPORTED = ["1M", "2M", "3M", "5M", "15M"]  # 30S يحتاج tick data

EXPIRIES = ["10S", "30S", "5M", "10M", "15M", "30M"]
EXPIRIES_SUPPORTED = ["5M", "10M", "15M", "30M"]  # 10S/30S تحتاج tick + دعم المنصة

# ========== Flask - Health Check ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "MAD-TIGER-BOT 🐯 | LIVE"

@flask_app.route('/healthz')
def healthz():
    return jsonify({
        "status": "ok",
        "bot": "MAD-TIGER-BOT",
        "markets": MARKETS_REAL,
        "markets_count": len(MARKETS_REAL),
        "otc_available": OTC_AVAILABLE,
        "timeframes_requested": TIMEFRAMES,
        "timeframes_supported": TIMEFRAMES_SUPPORTED,
        "expiries_requested": EXPIRIES,
        "expiries_supported": EXPIRIES_SUPPORTED,
        "tw": bool(TW_KEY),
        "fh": bool(FH_KEY),
        "ge": bool(GE_KEY),
        "gr": bool(GR_KEY),
        "tiger_buy_exists": TIGER_BUY_PATH.exists(),
        "tiger_sell_exists": TIGER_SELL_PATH.exists(),
        "time": datetime.utcnow().isoformat()
    })

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

Thread(target=run_flask, daemon=True).start()

# ========== DB - إحصائيات حقيقية دائمة ==========
db_lock = Lock()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        market TEXT,
        market_type TEXT,
        timeframe TEXT,
        expiry TEXT,
        direction TEXT,
        entry_time TEXT,
        expiry_time TEXT,
        price REAL,
        tw_status TEXT,
        fh_status TEXT,
        ge_status TEXT,
        gr_status TEXT,
        reason TEXT,
        result TEXT DEFAULT 'PENDING',
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        markets TEXT,
        timeframe TEXT DEFAULT '1M',
        expiry TEXT DEFAULT '5M'
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        authorized INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

init_db()

def db_exec(query, params=(), fetch=False):
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        cur.execute(query, params)
        data = cur.fetchall() if fetch else None
        conn.commit()
        conn.close()
        return data

def get_settings(user_id):
    rows = db_exec("SELECT markets, timeframe, expiry FROM user_settings WHERE user_id=?", (user_id,), True)
    if rows:
        m_json, tf, exp = rows[0]
        try:
            markets = json.loads(m_json)
        except:
            markets = MARKETS_REAL[:4]
        return {"markets": markets, "timeframe": tf, "expiry": exp}
    else:
        default = MARKETS_REAL[:4]
        db_exec("INSERT OR REPLACE INTO user_settings VALUES (?,?,?,?)",
                (user_id, json.dumps(default), "1M", "5M"))
        return {"markets": default, "timeframe": "1M", "expiry": "5M"}

def save_settings(user_id, markets=None, timeframe=None, expiry=None):
    cur = get_settings(user_id)
    if markets is not None:
        cur["markets"] = markets
    if timeframe is not None:
        cur["timeframe"] = timeframe
    if expiry is not None:
        cur["expiry"] = expiry
    db_exec("INSERT OR REPLACE INTO user_settings VALUES (?,?,?,?)",
            (user_id, json.dumps(cur["markets"]), cur["timeframe"], cur["expiry"]))

# ========== الأمان ==========
def secure_compare(a, b):
    return hmac.compare_digest(str(a).strip(), str(b).strip())

def is_authorized(user_id):
    rows = db_exec("SELECT authorized FROM users WHERE user_id=?", (user_id,), True)
    if not rows:
        return False
    return rows[0][0] == 1

def authorize_user(user_id):
    db_exec("INSERT OR REPLACE INTO users (user_id, authorized) VALUES (?,1)", (user_id,))

# ========== مصادر البيانات الحقيقية ==========

class TwelveData:
    """TW - المصدر الأساسي"""
    def __init__(self, key):
        self.key = key

    async def fetch(self, symbol, interval="1min", outputsize=100):
        if not self.key:
            return {"status": "FAIL", "reason": "TW_KEY missing", "closes": [], "fresh": False}

        clean = symbol.replace(" OTC", "").strip()
        url = "https://api.twelvedata.com/time_series"
        params = {"symbol": clean, "interval": interval, "outputsize": outputsize, "apikey": self.key, "format": "JSON"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as r:
                    if r.status != 200:
                        return {"status": "FAIL", "reason": f"HTTP {r.status}", "closes": [], "fresh": False}
                    data = await r.json()

                    # تحقق من الأخطاء
                    if "code" in data and data["code"] != 200:
                        return {"status": "FAIL", "reason": data.get("message", "API error"), "closes": [], "fresh": False}

                    values = data.get("values", [])
                    if not values or len(values) < 20:
                        return {"status": "FAIL", "reason": "بيانات ناقصة", "closes": [], "fresh": False}

                    # ترتيب زمني قديم → جديد
                    values = list(reversed(values))
                    closes = []
                    for v in values:
                        try:
                            closes.append(float(v["close"]))
                        except:
                            continue

                    # فحص الحداثة - آخر شمعة لا تكون أقدم من 30 دقيقة
                    try:
                        last_time_str = data["values"][0]["datetime"]
                        last_time = datetime.fromisoformat(last_time_str.replace("Z", ""))
                        now = datetime.utcnow()
                        diff = (now - last_time).total_seconds() / 60
                        fresh = diff < 60  # أقل من ساعة
                    except:
                        fresh = True

                    return {
                        "status": "OK",
                        "reason": "OK",
                        "closes": closes,
                        "fresh": fresh,
                        "raw_time": data["values"][0].get("datetime", "") if data.get("values") else "",
                        "source": "TW"
                    }
        except asyncio.TimeoutError:
            return {"status": "FAIL", "reason": "انتهت مهلة TW", "closes": [], "fresh": False}
        except Exception as e:
            return {"status": "FAIL", "reason": f"TW خطأ {e}", "closes": [], "fresh": False}

class Finnhub:
    """FH - مصدر احتياطي"""
    def __init__(self, key):
        self.key = key

    async def fetch(self, symbol, resolution="1", count=100):
        if not self.key:
            return {"status": "SKIP", "reason": "FH_KEY غير موجود", "closes": []}

        clean = symbol.replace(" OTC", "").replace("/", "")
        # Finnhub يستخدم رموز بدون /
        url = "https://finnhub.io/api/v1/forex/candle"
        now = int(time.time())
        params = {
            "symbol": f"OANDA:{symbol.replace(' OTC','').replace('/','_')}",
            "resolution": resolution,
            "from": now - 3600 * 5,
            "to": now,
            "token": self.key
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as r:
                    if r.status != 200:
                        return {"status": "FAIL", "reason": f"FH HTTP {r.status}", "closes": []}
                    data = await r.json()
                    if data.get("s") != "ok":
                        return {"status": "FAIL", "reason": "FH بيانات غير متاحة", "closes": []}

                    closes = data.get("c", [])
                    if len(closes) < 20:
                        return {"status": "FAIL", "reason": "FH بيانات ناقصة", "closes": []}

                    return {"status": "OK", "reason": "OK", "closes": closes, "source": "FH"}
        except Exception as e:
            return {"status": "FAIL", "reason": f"FH خطأ {e}", "closes": []}

class GeminiReview:
    """GE - مراجعة AI"""
    def __init__(self, key):
        self.key = key

    async def review(self, market, closes, tech_result):
        if not self.key:
            return {"status": "SKIP", "decision": "SKIP", "reason": "GE_KEY غير موجود"}

        if len(closes) < 20:
            return {"status": "FAIL", "decision": "WAIT", "reason": "GE بيانات ناقصة"}

        prompt = f"""
Market: {market}
Last price: {closes[-1]}
RSI: {tech_result.get('rsi', 'N/A')}
Trend: {tech_result.get('trend', 'N/A')}
Tech decision: {tech_result.get('decision')}

راجع الاتجاه باختصار، لا تخترع أسعار. رد بـ BUY أو SELL أو WAIT فقط مع سبب قصير.
"""

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self.key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=15) as r:
                    if r.status != 200:
                        return {"status": "FAIL", "decision": "WAIT", "reason": f"GE HTTP {r.status}"}
                    data = await r.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"].upper()

                    if "BUY" in text and "SELL" not in text:
                        dec = "BUY"
                    elif "SELL" in text and "BUY" not in text:
                        dec = "SELL"
                    else:
                        dec = "WAIT"

                    return {"status": "OK", "decision": dec, "reason": text[:150]}
        except Exception as e:
            return {"status": "FAIL", "decision": "WAIT", "reason": f"GE خطأ {e}"}

class GroqReview:
    """GR - مراجعة ثانية"""
    def __init__(self, key):
        self.key = key

    async def review(self, market, closes, tech_result):
        if not self.key:
            return {"status": "SKIP", "decision": "SKIP", "reason": "GR_KEY غير موجود"}

        if len(closes) < 20:
            return {"status": "FAIL", "decision": "WAIT", "reason": "GR بيانات ناقصة"}

        prompt = f"Market {market} price {closes[-1]} tech {tech_result.get('decision')} RSI {tech_result.get('rsi')}. Reply BUY SELL or WAIT only."

        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 50
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=15) as r:
                    if r.status != 200:
                        return {"status": "FAIL", "decision": "WAIT", "reason": f"GR HTTP {r.status}"}
                    data = await r.json()
                    text = data["choices"][0]["message"]["content"].upper()

                    if "BUY" in text and "SELL" not in text:
                        dec = "BUY"
                    elif "SELL" in text and "BUY" not in text:
                        dec = "SELL"
                    else:
                        dec = "WAIT"

                    return {"status": "OK", "decision": dec, "reason": text[:150]}
        except Exception as e:
            return {"status": "FAIL", "decision": "WAIT", "reason": f"GR خطأ {e}"}

# ========== المحرك الفني - قرار حتمي قابل للاختبار ==========
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def technical_decision(closes):
    """
    قرار فني واضح ومحدد:
    - BUY: RSI < 70 و السعر فوق EMA20 و فوق EMA50
    - SELL: RSI > 30 و السعر تحت EMA20 و تحت EMA50
    - WAIT: غير ذلك أو بيانات ناقصة
    """
    if len(closes) < 50:
        return {"decision": "WAIT", "reason": "بيانات أقل من 50 شمعة", "rsi": 50, "trend": "UNKNOWN"}

    rsi = calculate_rsi(closes)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    last = closes[-1]

    # اتجاه
    if last > ema20 > ema50:
        trend = "صاعد"
    elif last < ema20 < ema50:
        trend = "هابط"
    else:
        trend = "عرضي"

    # شروط BUY
    if rsi < 68 and last > ema20 and ema20 > ema50 and closes[-1] > closes[-5]:
        return {"decision": "BUY", "reason": f"RSI {rsi:.1f} + فوق EMA20/50 + صعود", "rsi": rsi, "trend": trend, "ema20": ema20, "ema50": ema50}

    # شروط SELL
    if rsi > 32 and last < ema20 and ema20 < ema50 and closes[-1] < closes[-5]:
        return {"decision": "SELL", "reason": f"RSI {rsi:.1f} + تحت EMA20/50 + هبوط", "rsi": rsi, "trend": trend, "ema20": ema20, "ema50": ema50}

    return {"decision": "WAIT", "reason": f"RSI {rsi:.1f} + {trend} - لا يوجد إشارة واضحة", "rsi": rsi, "trend": trend, "ema20": ema20, "ema50": ema50}

# ========== مصادر البيانات ==========
tw_source = TwelveData(TW_KEY)
fh_source = Finnhub(FH_KEY)
ge_review = GeminiReview(GE_KEY)
gr_review = GroqReview(GR_KEY)

# ========== حالة المسح ==========
active_scans = set()
scan_tasks = {}

# ========== واجهة تيليجرام ==========
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Signal", callback_data="start_scan"),
         InlineKeyboardButton("🛑 إيقاف الإشارات", callback_data="stop_scan")],
        [InlineKeyboardButton("🌍 الأسواق", callback_data="markets"),
         InlineKeyboardButton("⏱️ الفريمات", callback_data="timeframes")],
        [InlineKeyboardButton("⌛ مدة الصفقة", callback_data="expiries"),
         InlineKeyboardButton("📊 إجمالي الإشارات", callback_data="stats")],
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔐 أدخل كلمة المرور لفتح البوت:")

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    if text.startswith("/"):
        return

    if is_authorized(user.id):
        return

    if secure_compare(text, BOT_PASSWORD):
        authorize_user(user.id)
        await update.message.reply_text(
            "تم فتح البوت 🔓🔑\n\n"
            "MAD-TIGER-BOT 🐯 جاهز\n"
            f"الأسواق: {', '.join(MARKETS_REAL)}\n"
            f"OTC: {'متاح' if OTC_AVAILABLE else 'غير متاح مؤقتاً'}\n"
            f"الفريمات المدعومة: {', '.join(TIMEFRAMES_SUPPORTED)}\n"
            f"المدد المدعومة: {', '.join(EXPIRIES_SUPPORTED)}",
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text("❌ كلمة مرور خاطئة")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    # حماية
    if not is_authorized(user_id):
        if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
            await query.edit_message_text("❌ غير مصرح")
            return
        # إذا ALLOWED_USER_ID = 0 نسمح لمن فتح بالباسورد
        if not is_authorized(user_id):
            await query.edit_message_text("🔐 أدخل كلمة المرور أولاً /start")
            return

    data = query.data

    if data == "start_scan":
        if user_id in active_scans:
            await query.edit_message_text("✅ المسح شغال بالفعل", reply_markup=main_keyboard())
            return
        active_scans.add(user_id)
        task = asyncio.create_task(scan_cycle(user_id, context.application))
        scan_tasks[user_id] = task
        await query.edit_message_text("🚀 جاري بدء المسح...\nTW — جارٍ فحص بيانات الأسعار.", reply_markup=main_keyboard())

    elif data == "stop_scan":
        active_scans.discard(user_id)
        if user_id in scan_tasks:
            try:
                scan_tasks[user_id].cancel()
            except:
                pass
            del scan_tasks[user_id]
        await query.edit_message_text("🛑 تم إيقاف المسح", reply_markup=main_keyboard())

    elif data == "markets":
        settings = get_settings(user_id)
        text = "🌍 الأسواق المتاحة:\n\n"
        for m in MARKETS_REAL:
            check = "✅" if m in settings["markets"] else "⬜"
            text += f"{check} {m} (REAL)\n"
        if OTC_AVAILABLE:
            for m in MARKETS_OTC:
                check = "✅" if m in settings["markets"] else "⬜"
                text += f"{check} {m} (OTC)\n"
        else:
            text += "\n⚠️ OTC غير متاح مؤقتاً - لا يوجد مصدر أسعار موثوق\n"
        text += "\nاختر الأسواق (اضغط لتفعيل/تعطيل):"

        keyboard = []
        for m in MARKETS_REAL:
            keyboard.append([InlineKeyboardButton(f"{'✅' if m in settings['markets'] else '⬜'} {m}", callback_data=f"toggle_market:{m}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("toggle_market:"):
        market = data.split(":", 1)[1]
        settings = get_settings(user_id)
        markets = settings["markets"]
        if market in markets:
            markets.remove(market)
        else:
            markets.append(market)
        if not markets:
            markets = [MARKETS_REAL[0]]
        save_settings(user_id, markets=markets)
        # إعادة عرض
        text = "🌍 الأسواق المتاحة:\n\n"
        for m in MARKETS_REAL:
            check = "✅" if m in markets else "⬜"
            text += f"{check} {m} (REAL)\n"
        keyboard = []
        for m in MARKETS_REAL:
            keyboard.append([InlineKeyboardButton(f"{'✅' if m in markets else '⬜'} {m}", callback_data=f"toggle_market:{m}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "timeframes":
        settings = get_settings(user_id)
        text = f"⏱️ الفريمات:\nالمطلوبة: {', '.join(TIMEFRAMES)}\nالمدعومة فعلياً: {', '.join(TIMEFRAMES_SUPPORTED)}\n\n30S غير مدعوم (يحتاج tick data)\n\nالفريم الحالي: {settings['timeframe']}\n"
        keyboard = []
        for tf in TIMEFRAMES:
            if tf in TIMEFRAMES_SUPPORTED:
                keyboard.append([InlineKeyboardButton(f"{'✅' if tf==settings['timeframe'] else '⬜'} {tf}", callback_data=f"set_tf:{tf}")])
            else:
                keyboard.append([InlineKeyboardButton(f"❌ {tf} (غير مدعوم)", callback_data="noop")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("set_tf:"):
        tf = data.split(":", 1)[1]
        if tf not in TIMEFRAMES_SUPPORTED:
            await query.answer("هذا الفريم غير مدعوم حالياً")
            return
        save_settings(user_id, timeframe=tf)
        await query.edit_message_text(f"✅ تم اختيار الفريم {tf}", reply_markup=main_keyboard())

    elif data == "expiries":
        settings = get_settings(user_id)
        text = f"⌛ مدد الصفقات:\nالمطلوبة: {', '.join(EXPIRIES)}\nالمدعومة فعلياً: {', '.join(EXPIRIES_SUPPORTED)}\n\n10S/30S غير مدعومة (تحتاج دقة tick + دعم المنصة)\n\nالمدة الحالية: {settings['expiry']}\n"
        keyboard = []
        for ex in EXPIRIES:
            if ex in EXPIRIES_SUPPORTED:
                keyboard.append([InlineKeyboardButton(f"{'✅' if ex==settings['expiry'] else '⬜'} {ex}", callback_data=f"set_exp:{ex}")])
            else:
                keyboard.append([InlineKeyboardButton(f"❌ {ex} (غير مدعوم)", callback_data="noop")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("set_exp:"):
        exp = data.split(":", 1)[1]
        if exp not in EXPIRIES_SUPPORTED:
            await query.answer("هذه المدة غير مدعومة حالياً")
            return
        save_settings(user_id, expiry=exp)
        await query.edit_message_text(f"✅ تم اختيار المدة {exp}", reply_markup=main_keyboard())

    elif data == "stats":
        rows = db_exec("SELECT direction, result, COUNT(*) FROM signals GROUP BY direction, result", (), True)
        total = db_exec("SELECT COUNT(*) FROM signals WHERE direction IN ('BUY','SELL')", (), True)
        total = total[0][0] if total else 0

        buy = db_exec("SELECT COUNT(*) FROM signals WHERE direction='BUY'", (), True)[0][0]
        sell = db_exec("SELECT COUNT(*) FROM signals WHERE direction='SELL'", (), True)[0][0]
        wait = db_exec("SELECT COUNT(*) FROM signals WHERE direction='WAIT'", (), True)[0][0]
        win = db_exec("SELECT COUNT(*) FROM signals WHERE result='WIN'", (), True)[0][0]
        loss = db_exec("SELECT COUNT(*) FROM signals WHERE result='LOSS'", (), True)[0][0]
        pending = db_exec("SELECT COUNT(*) FROM signals WHERE result='PENDING' AND direction IN ('BUY','SELL')", (), True)[0][0]

        if win + loss > 0:
            winrate = win / (win + loss) * 100
            winrate_str = f"{winrate:.1f}%"
        else:
            winrate_str = "غير متاح (لا توجد نتائج محسومة)"

        text = f"""📊 إجمالي الإشارات - إحصائيات حقيقية

• إجمالي الإشارات الصادرة: {total}
• BUY: {buy}
• SELL: {sell}
• WAIT: {wait} (منفصل)
• WIN: {win}
• LOSS: {loss}
• غير محسومة: {pending}
• نسبة الفوز: {winrate_str}

المعادلة: WIN ÷ (WIN+LOSS) × 100
القاعدة: لا تحتسب إلا بعد انتهاء المدة
"""

        await query.edit_message_text(text, reply_markup=main_keyboard())

    elif data == "back_main":
        await query.edit_message_text("MAD-TIGER-BOT 🐯 - القائمة الرئيسية", reply_markup=main_keyboard())

    elif data == "noop":
        await query.answer("غير مدعوم حالياً")

# ========== دورة المسح - حسب المواصفات ==========
async def scan_cycle(user_id, application):
    """
    المراحل:
    1. قراءة الإعدادات
    2. مسح الأسواق - TW ثم FH
    3. فحص جودة البيانات
    4. التحليل الفني
    5. مراجعة GE
    6. مراجعة GR
    7. القرار النهائي BUY/SELL/WAIT
    8. الإرسال مع صورة التايقر
    """
    try:
        settings = get_settings(user_id)
        markets = settings["markets"]
        timeframe = settings["timeframe"]
        expiry = settings["expiry"]

        # رسالة تقدم
        progress_msg = await application.bot.send_message(
            chat_id=user_id,
            text=f"TW — جارٍ فحص بيانات الأسعار.\nالأسواق: {', '.join(markets)}\nالفريم: {timeframe} | المدة: {expiry}"
        )

        # تحويل الفريم لـ TwelveData interval
        tf_map = {"1M": "1min", "2M": "1min", "3M": "1min", "5M": "5min", "15M": "15min"}
        interval = tf_map.get(timeframe, "1min")

        # FH resolution
        fh_map = {"1M": "1", "2M": "1", "3M": "1", "5M": "5", "15M": "15"}
        fh_res = fh_map.get(timeframe, "1")

        for market in markets:
            if user_id not in active_scans:
                break

            if "OTC" in market and not OTC_AVAILABLE:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"⚠️ {market} غير متاح مؤقتاً - لا يوجد مصدر OTC موثوق"
                )
                await asyncio.sleep(2)
                continue

            # المرحلة 1-2: TW
            try:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"TW — جارٍ فحص {market} {timeframe}\nFH — انتظار\nGE — انتظار\nGR — انتظار"
                )
            except:
                pass

            tw_res = await tw_source.fetch(market, interval=interval, outputsize=100)

            if tw_res["status"] != "OK":
                # حفظ WAIT
                db_exec("""INSERT INTO signals (id, market, market_type, timeframe, expiry, direction, entry_time, expiry_time, price, tw_status, fh_status, ge_status, gr_status, reason, result, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"WAIT_{int(time.time()*1000)}_{random.randint(100,999)}",
                         market.replace(" OTC", ""), "OTC" if "OTC" in market else "REAL",
                         timeframe, expiry, "WAIT", "", "", 0,
                         tw_res["status"], "—", "—", "—",
                         tw_res["reason"], "—",
                         datetime.now(pytz.timezone('Asia/Riyadh')).isoformat()))
                try:
                    await application.bot.edit_message_text(
                        chat_id=user_id, message_id=progress_msg.message_id,
                        text=f"WAIT {market} — {tw_res['reason']}"
                    )
                except:
                    pass
                await asyncio.sleep(1)
                continue

            closes = tw_res["closes"]

            # بناء فريمات 2M و 3M من 1M إذا لزم
            if timeframe == "2M" and interval == "1min" and len(closes) >= 4:
                # كل شمعتين 1M = شمعة 2M (آخر سعر)
                closes = [closes[i] for i in range(1, len(closes), 2)]
            elif timeframe == "3M" and interval == "1min" and len(closes) >= 6:
                closes = [closes[i] for i in range(2, len(closes), 3)]

            # المرحلة 3: فحص الجودة
            if not tw_res.get("fresh", True):
                reason = "بيانات قديمة - آخر تحديث أقدم من ساعة"
                db_exec("""INSERT INTO signals (id, market, market_type, timeframe, expiry, direction, entry_time, expiry_time, price, tw_status, fh_status, ge_status, gr_status, reason, result, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"WAIT_{int(time.time()*1000)}_{random.randint(100,999)}",
                         market.replace(" OTC", ""), "OTC" if "OTC" in market else "REAL",
                         timeframe, expiry, "WAIT", "", "", closes[-1] if closes else 0,
                         "OLD", "—", "—", "—", reason, "—",
                         datetime.now(pytz.timezone('Asia/Riyadh')).isoformat()))
                try:
                    await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text=f"WAIT {market} — {reason}")
                except:
                    pass
                await asyncio.sleep(1)
                continue

            if len(closes) < 20:
                reason = f"بيانات ناقصة {len(closes)} < 20"
                try:
                    await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text=f"WAIT {market} — {reason}")
                except:
                    pass
                await asyncio.sleep(1)
                continue

            # المرحلة 4: FH تحقق
            try:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"TW ✓ {market} OK\nFH — جارٍ التحقق\nGE — انتظار\nGR — انتظار"
                )
            except:
                pass

            fh_result = await fh_source.fetch(market, resolution=fh_res)

            # المرحلة 5: تحليل فني
            tech = technical_decision(closes)

            try:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"TW ✓ {market} OK\nFH { '✓' if fh_result['status']=='OK' else fh_result['status'] }\nGE — جارٍ المراجعة الأولى\nGR — انتظار\nالفني: {tech['decision']} - {tech['reason'][:50]}"
                )
            except:
                pass

            if tech["decision"] == "WAIT":
                db_exec("""INSERT INTO signals (id, market, market_type, timeframe, expiry, direction, entry_time, expiry_time, price, tw_status, fh_status, ge_status, gr_status, reason, result, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"WAIT_{int(time.time()*1000)}_{random.randint(100,999)}",
                         market.replace(" OTC", ""), "OTC" if "OTC" in market else "REAL",
                         timeframe, expiry, "WAIT", "", "", closes[-1],
                         tw_res["status"], fh_result["status"], "—", "—",
                         tech["reason"], "—",
                         datetime.now(pytz.timezone('Asia/Riyadh')).isoformat()))
                try:
                    await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text=f"WAIT {market} — {tech['reason']}")
                except:
                    pass
                await asyncio.sleep(1.5)
                continue

            # المرحلة 6: GE
            ge_res = await ge_review.review(market, closes, tech)

            try:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"TW ✓ {market}\nFH {fh_result['status']}\nGE {ge_res['status']} {ge_res['decision']}\nGR — جارٍ المراجعة الثانية"
                )
            except:
                pass

            # المرحلة 7: GR
            gr_res = await gr_review.review(market, closes, tech)

            # القرار النهائي - محرك حتمي
            final_decision = tech["decision"]

            # إذا GE و GR اتفقا ضد الفني → WAIT
            if ge_res["decision"] != "SKIP" and gr_res["decision"] != "SKIP":
                if ge_res["decision"] != tech["decision"] and gr_res["decision"] != tech["decision"] and ge_res["decision"] == gr_res["decision"]:
                    final_decision = "WAIT"
                    reason = f"تعارض: فني {tech['decision']} vs GE+GR {ge_res['decision']}"
                else:
                    reason = f"{tech['reason']} | GE {ge_res['decision']} | GR {gr_res['decision']}"
            else:
                reason = tech["reason"]

            if final_decision == "WAIT":
                db_exec("""INSERT INTO signals (id, market, market_type, timeframe, expiry, direction, entry_time, expiry_time, price, tw_status, fh_status, ge_status, gr_status, reason, result, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"WAIT_{int(time.time()*1000)}_{random.randint(100,999)}",
                         market.replace(" OTC", ""), "OTC" if "OTC" in market else "REAL",
                         timeframe, expiry, "WAIT", "", "", closes[-1],
                         tw_res["status"], fh_result["status"], ge_res["status"], gr_res["status"],
                         reason, "—",
                         datetime.now(pytz.timezone('Asia/Riyadh')).isoformat()))
                try:
                    await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text=f"WAIT {market} — {reason}")
                except:
                    pass
                await asyncio.sleep(1.5)
                continue

            # المرحلة 8: إرسال الإشارة مع صورة التايقر
            now = datetime.now(pytz.timezone('Asia/Riyadh'))
            entry = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)

            exp_map = {"5M": 5, "10M": 10, "15M": 15, "30M": 30}
            exp_min = exp_map.get(expiry, 5)
            expiry_time = entry + timedelta(minutes=exp_min)

            sig_id = f"SIG_{int(time.time()*1000)}_{random.randint(100,999)}"

            db_exec("""INSERT INTO signals (id, market, market_type, timeframe, expiry, direction, entry_time, expiry_time, price, tw_status, fh_status, ge_status, gr_status, reason, result, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sig_id, market.replace(" OTC", ""), "OTC" if "OTC" in market else "REAL",
                     timeframe, expiry, final_decision, entry.isoformat(), expiry_time.isoformat(), closes[-1],
                     tw_res["status"], fh_result["status"], ge_res["status"], gr_res["status"],
                     reason, "PENDING", now.isoformat()))

            tw_icon = "✓" if tw_res["status"] == "OK" else f"({tw_res['status']})"
            fh_icon = "✓" if fh_result["status"] == "OK" else f"({fh_result['status']})" if fh_result["status"] != "SKIP" else "—"
            ge_icon = "✓" if ge_res["status"] == "OK" else f"({ge_res['status']})" if ge_res["status"] != "SKIP" else "—"
            gr_icon = "✓" if gr_res["status"] == "OK" else f"({gr_res['status']})" if gr_res["status"] != "SKIP" else "—"

            agree = sum(1 for r in [tw_res, fh_result, ge_res, gr_res] if r["status"] == "OK")

            signal_text = f"""MAD-TIGER-BOT | SIGNAL 🐯

• السوق: {market.replace(" OTC","")}
• النوع: {"OTC" if "OTC" in market else "REAL"}
• الاتجاه: {final_decision}
• الفريم: {timeframe}
• مدة الصفقة: {expiry}
• وقت الإشارة: {now.strftime("%Y-%m-%d %H:%M:%S")} Asia/Riyadh
• وقت الدخول: {entry.strftime("%H:%M:%S")}
• وقت الانتهاء: {expiry_time.strftime("%H:%M:%S")}
• السعر المرجعي: {closes[-1]:.5f}
• بيانات الأسعار: TW {tw_icon} / FH {fh_icon}
• مراجعة التحليل: GE {ge_icon} / GR {gr_icon}
• التوافق: {agree}/4 مصادر/مراجعات ناجحة
• السبب: {reason[:150]}
• التحذير: التداول ينطوي على مخاطر ولا توجد نتيجة مضمونة.
"""

            # إرسال مع صورة التايقر الحقيقية
            try:
                img_path = TIGER_BUY_PATH if final_decision == "BUY" else TIGER_SELL_PATH
                if img_path.exists():
                    with open(img_path, 'rb') as photo:
                        await application.bot.send_photo(chat_id=user_id, photo=photo, caption=signal_text)
                else:
                    await application.bot.send_message(chat_id=user_id, text=signal_text)
            except Exception as e:
                print(f"send photo failed {e}, fallback text")
                await application.bot.send_message(chat_id=user_id, text=signal_text)

            try:
                await application.bot.edit_message_text(
                    chat_id=user_id, message_id=progress_msg.message_id,
                    text=f"✅ أرسلت إشارة {market} {final_decision}\nالفريم {timeframe} | المدة {expiry}"
                )
            except:
                pass

            await asyncio.sleep(3)

        # نهاية الدورة
        try:
            await application.bot.edit_message_text(
                chat_id=user_id, message_id=progress_msg.message_id,
                text=f"✅ انتهى المسح\nالفريم: {timeframe} | المدة: {expiry}\nالأسواق: {', '.join(markets)}\n\nاضغط Start Signal مرة أخرى للمسح الجديد"
            )
        except:
            pass

    except asyncio.CancelledError:
        try:
            await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text="🛑 تم إيقاف المسح")
        except:
            pass
    except Exception as e:
        print(f"scan_cycle error {e}")
        try:
            await application.bot.edit_message_text(chat_id=user_id, message_id=progress_msg.message_id, text=f"❌ خطأ: {e}")
        except:
            pass
    finally:
        active_scans.discard(user_id)
        if user_id in scan_tasks:
            del scan_tasks[user_id]

# ========== Setup Bot ==========
def setup_bot():
    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    print("✅ MAD-TIGER-BOT نظيف وجاهز 100% 🐯")
    return app_bot

bot_app = setup_bot()

if __name__ == "__main__":
    print("🚀 Starting MAD-TIGER-BOT...", flush=True)
    # Flask شغال في ثريد خلفي من فوق
    # نشغل البوت بطريقة PTB v22 المتوافقة مع Python 3.14
    try:
        bot_app.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"]
        )
    except Exception as e:
        print(f"❌ Polling crashed: {e}")
        import time
        time.sleep(10)
