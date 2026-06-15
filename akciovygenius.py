import io
import math
import asyncio
import time
import json
import hashlib
import logging
from datetime import datetime, timezone
import os
import re
import requests
import xml.etree.ElementTree as ET
from groq import Groq
import PyPDF2
from bs4 import BeautifulSoup
import numpy as np
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from plotly.subplots import make_subplots
import plotly.io as pio

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from mtf_analysis import analyze_mtf_levels, format_level, format_zone, make_mtf_chart
from dotenv import load_dotenv
load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("akciovygenius")

log.info("🚀 Startuji Akciový Genius bota…")

# ── Klíče / tajné údaje ───────────────────────────────────────────────────────
# Přednost má proměnná prostředí (.env / env na serveru); když chybí, použije se
# zabudovaná hodnota, aby bot naběhl i tam, kde .env není (deployment).
TOKEN = os.getenv("TELEGRAM_TOKEN") or "8825645830:AAGat5gPqE16QUe2W_UQ4SrlzpyBEa10daU"
AV_KEY = os.getenv("AV_KEY") or "Q3UCP540D9VVDBBI"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Inicializace klienta pro Groq — jen pokud je klíč k dispozici.
# Bez této pojistky by Groq(api_key=None) vyhodil výjimku už při startu a shodil celý bot.
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
if client is None:
    log.warning("GROQ_API_KEY chybí — AI funkce (news, walter, /ai) budou vypnuté.")

active_snipers = {}


# ── Walter (makro feed) konfigurace ───────────────────────────────────────────
# Vše laditelné přes .env bez zásahu do kódu.
WALTER_INTERVAL = int(os.getenv("WALTER_INTERVAL", "35"))            # interval kontroly feedu (s)
WALTER_VOL_SPIKE = float(os.getenv("WALTER_VOL_SPIKE", "3.0"))       # kolikanásobek průměru = spike
WALTER_ATR_MULT = float(os.getenv("WALTER_ATR_MULT", "1.5"))         # násobek ATR pro stop-loss
WALTER_DEFAULT_TICKER = os.getenv("WALTER_DEFAULT_TICKER", "NVDA")   # proxy pro měření objemu u makra
WALTER_COOLDOWN_MIN = int(os.getenv("WALTER_COOLDOWN_MIN", "10"))    # min. odstup alertů na stejný ticker (min)
FIXED_RISK_PCT_FALLBACK = float(os.getenv("WALTER_FIXED_RISK_PCT", "0.008"))  # když ATR chybí
WALTER_DEDUP_HISTORY = int(os.getenv("WALTER_DEDUP_HISTORY", "30"))  # kolik posledních zpráv pamatovat

# Runtime stav pro Walter (rate-limiting alertů na ticker)
_walter_last_alert = {}            # {ticker: timestamp posledního alertu}
_WALTER_SEEN_FILE = "walter_seen.json"
_walter_seen_hashes = []           # rolling historie hashů zpráv (perzistovaná)
_walter_seen_loaded = False


def us_market_session():
    """Vrátí 'regular' | 'pre' | 'after' | 'closed' podle času v US/Eastern."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now(timezone.utc)  # nouzový fallback (přibližně)
    if now_et.weekday() >= 5:
        return "closed"
    t = now_et.hour * 60 + now_et.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= t < 16 * 60:
        return "regular"
    if 16 * 60 <= t < 20 * 60:
        return "after"
    return "closed"


def compute_atr(df, period: int = 14):
    """ATR z DataFrame s High/Low/Close. Vrátí None při nedostatku dat."""
    try:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.tail(period).mean()
        return float(atr) if (atr == atr and atr > 0) else None  # atr==atr odfiltruje NaN
    except Exception:
        return None


def _walter_cooldown_ok(ticker: str) -> bool:
    """True, pokud na daný ticker uplynul cooldown od posledního alertu."""
    last = _walter_last_alert.get(ticker)
    return last is None or (time.time() - last) >= WALTER_COOLDOWN_MIN * 60


def _walter_mark_alert(ticker: str):
    _walter_last_alert[ticker] = time.time()


def _walter_seen_check_and_remember(text: str) -> bool:
    """Vrátí True, pokud už jsme tuto zprávu viděli (dedup přes rolling hash
    historii perzistovanou na disk → odolné i vůči restartu bota)."""
    global _walter_seen_hashes, _walter_seen_loaded
    if not _walter_seen_loaded:
        try:
            if os.path.exists(_WALTER_SEEN_FILE):
                with open(_WALTER_SEEN_FILE, "r", encoding="utf-8") as f:
                    _walter_seen_hashes = json.load(f)
        except Exception:
            _walter_seen_hashes = []
        _walter_seen_loaded = True

    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if h in _walter_seen_hashes:
        return True

    _walter_seen_hashes.append(h)
    del _walter_seen_hashes[:-WALTER_DEDUP_HISTORY]  # ořež na posledních N
    try:
        with open(_WALTER_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(_walter_seen_hashes, f)
    except Exception:
        pass
    return False


def _walter_confidence(vol_mult: float):
    """Jednoduché confidence skóre z poměru objemu vůči prahu spiku."""
    ratio = vol_mult / WALTER_VOL_SPIKE if WALTER_VOL_SPIKE > 0 else 0
    if ratio >= 2.0:
        return "🟢 Vysoká"
    if ratio >= 1.3:
        return "🟡 Střední"
    return "🔴 Nízká"


# ── Helper: volání Groq s pojistkou proti chybějícímu klíči ───────────────────
async def ask_groq(prompt: str, temperature: float = 0.2,
                   model: str = "llama-3.3-70b-versatile"):
    """Zavolá Groq chat completion a vrátí text odpovědi.
    Když klient není nakonfigurovaný (chybí GROQ_API_KEY), vrátí None."""
    if client is None:
        log.warning("Pokus o volání Groq bez klíče — přeskakuji.")
        return None
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        messages=[{"role": "user", "content": prompt}],
        model=model,
        temperature=temperature,
    )
    return resp.choices[0].message.content


# ── Jednoduchá TTL cache pro yfinance stahování ───────────────────────────────
_YF_CACHE: dict = {}

def cached_yf_download(ticker: str, period: str, interval: str, ttl: int = 300):
    """yf.download s in-memory TTL cache (výchozí 5 min).
    Snižuje počet requestů na Yahoo a riziko rate-limitu při hromadných skenech."""
    key = (ticker.upper(), period, interval)
    now = time.time()
    cached = _YF_CACHE.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1].copy()
    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=True, progress=False, group_by="ticker")
    _YF_CACHE[key] = (now, df.copy())
    return df


# ── Persistence aktivních sniperů (přežije restart bota) ──────────────────────
SNIPERS_FILE = "active_snipers.json"

def load_snipers() -> dict:
    """Načte uložené snipery z disku. Klíče = chat_id (int), hodnoty = set tickerů."""
    try:
        with open(SNIPERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): set(v) for k, v in raw.items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error("Chyba při načítání sniperů: %s", e)
        return {}

def save_snipers() -> None:
    """Uloží aktuální stav active_snipers na disk."""
    try:
        with open(SNIPERS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): sorted(v) for k, v in active_snipers.items()}, f)
    except Exception as e:
        log.error("Nepodařilo se uložit snipery: %s", e)


# ── Telegram helpery (limity délky zpráv) ─────────────────────────────────────
TG_CAPTION_LIMIT = 1024   # max délka popisku fotky
TG_MSG_LIMIT = 4096       # max délka textové zprávy

async def reply_long(message, text: str, parse_mode: str = "Markdown") -> None:
    """Pošle text rozdělený na kusy do 4096 znaků. Když selže Markdown, zkusí plain text."""
    for i in range(0, len(text), TG_MSG_LIMIT):
        chunk = text[i:i + TG_MSG_LIMIT]
        try:
            await message.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await message.reply_text(chunk.replace("*", "").replace("`", "").replace("_", ""))

async def reply_photo_with_text(message, png: bytes, text: str, short_caption: str) -> None:
    """Pošle fotku s krátkým popiskem a plnou analýzu jako samostatné zprávy.
    Řeší tím limit 1024 znaků na popisek fotky."""
    try:
        await message.reply_photo(photo=io.BytesIO(png), caption=short_caption, parse_mode="Markdown")
    except Exception:
        await message.reply_photo(photo=io.BytesIO(png),
                                  caption=short_caption.replace("*", "").replace("`", ""))
    await reply_long(message, text)
# ── Timeframe konfigurace ────────────────────────────────────────────────────
TF_PERIOD = {
    "1m": "5d", "5m": "1mo", "15m": "1mo", "30m": "2mo",
    "1h": "1y", "4h": "1y", "1d": "1y", "1wk": "5y", "1mo": "max",
}

NASDAQ_100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALNY", "AMAT", "AMD", 
    "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON", "BKNG", "BKR", "CCEP", 
    "CDNS", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSGP", "CSX", 
    "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FER", 
    "FTNT", "GEHC", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INSM", "INTC", "INTU", 
    "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "MAR", "MCHP", "MDLZ", "MELI", 
    "META", "MNST", "MPWR", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA", "NXPI", 
    "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM", 
    "REGN", "ROP", "ROST", "SBUX", "SHOP", "SNPS", "STX", "TEAM", "TMUS", "TRI", 
    "TSLA", "TTWO", "TXN", "VRSK", "VRTX", "WMT", "XEL"
]

# ── S/R a Pomocné funkce ─────────────────────────────────────────────────────
def find_pivots(df, window=10):
    highs, lows = [], []
    for i in range(window, len(df) - window):
        seg = df.iloc[i - window:i + window + 1]
        if df["High"].iloc[i] == seg["High"].max():
            highs.append((df.index[i], float(df["High"].iloc[i])))
        if df["Low"].iloc[i] == seg["Low"].min():
            lows.append((df.index[i], float(df["Low"].iloc[i])))
    return highs, lows

def cluster_levels(points, tolerance):
    if not points: return []
    points = sorted(points, key=lambda p: p[1])
    clusters, current = [], [points[0]]
    for pt in points[1:]:
        if abs(pt[1] - np.mean([p[1] for p in current])) <= tolerance:
            current.append(pt)
        else:
            clusters.append(current)
            current = [pt]
    clusters.append(current)
    return [(float(np.mean([p[1] for p in c])), len(c), c) for c in clusters]

def load_russell_watchlist():
    if os.path.exists("russell2000.txt"):
        with open("russell2000.txt", "r") as f:
            return [line.strip().upper() for line in f if line.strip() and line.strip() != "TICKER"]
    return ["RKLB", "ASTS", "HIMS", "SOUN", "IONQ", "PLTR", "RGTI", "ACHR"]

# --- MTF Pomocné funkce ---
def _daily_bias_from_setup(score: int, setup_type: str, status: str) -> str:
    if "VYHNOUT" in status or "PROPADLO" in status or setup_type == "⚠️ No Setup": return "BEARISH / NO TRADE"
    if score >= 80: return "STRONG BULLISH"
    if score >= 65: return "BULLISH"
    if score >= 50: return "NEUTRAL / WAIT"
    return "WEAK / NO TRADE"

def _safe_mtf_levels(ticker: str):
    try: return analyze_mtf_levels(ticker)
    except Exception: return {}

def _format_mtf_support(levels) -> str:
    if not levels: return "N/A"
    return f"{format_level(levels.nearest_support)} (zone {format_zone(levels.support_zone)})"

def _format_mtf_resistance(levels) -> str:
    if not levels: return "N/A"
    return f"{format_level(levels.nearest_resistance)} (zone {format_zone(levels.resistance_zone)})"

def get_yf_val(statement: pd.DataFrame, row_name: str, col_index: int = 0):
    if statement is None or statement.empty: return None
    if row_name not in statement.index or col_index >= len(statement.columns): return None
    value = statement.loc[row_name].iloc[col_index]
    try:
        if pd.isna(value): return None
        return float(value)
    except (TypeError, ValueError): return None

def safe_pct(now, prev):
    if now is None or prev is None or prev == 0: return None
    return ((now - prev) / abs(prev)) * 100

def fetch_yahoo_rss(ticker: str) -> list:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []
        for item in root.findall('.//item')[:5]:
            title = item.findtext('title', 'Bez titulku')
            link = item.findtext('link', '')
            description = item.findtext('description', '')
            items.append({'title': title, 'link': link, 'summary': description})
        return items
    except Exception:
        return []

# ==============================================================================
# 2. HLAVNÍ ENGINE (make_chart)
# ==============================================================================
def make_chart(ticker: str, interval: str = "1d", render: bool = True):
    if interval in ["1h", "4h"]:
        png, text = make_sr_chart(ticker, interval)
        return png, text, None

    period = "1y" if interval in ["1d", "4h", "1wk"] else TF_PERIOD.get(interval, "1y")
    yf_interval = "1h" if interval == "4h" else interval

    df = cached_yf_download(ticker, period, yf_interval)

    if df.empty: return None, f"❌ Žádná data pro '{ticker}'.", None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker.upper() in df.columns.levels[0]: df = df[ticker.upper()]
        else: df.columns = df.columns.get_level_values(-1)

    if interval == "4h":
        df = df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()

    if df.empty or len(df) < 60: return None, f"❌ Nedostatek dat.", None

    last = float(df["Close"].iloc[-1])
    
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    
    ema20, ema50, ema200 = float(df["EMA20"].iloc[-1]), float(df["EMA50"].iloc[-1]), float(df["EMA200"].iloc[-1])

    delta = df['Close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=13, adjust=False).mean()
    ema_down = down.ewm(com=13, adjust=False).mean()
    rs = ema_up / ema_down
    df['RSI'] = 100 - (100 / (1 + rs))
    rsi = float(df['RSI'].iloc[-1]) if not pd.isna(df['RSI'].iloc[-1]) else 50

    df['PrevClose'] = df['Close'].shift(1)
    tr1 = df['High'] - df['Low']
    tr2 = (df['High'] - df['PrevClose']).abs()
    tr3 = (df['Low'] - df['PrevClose']).abs()
    df['TR'] = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()
    atr = float(df['ATR'].iloc[-1])

    l_20 = min(20, len(df)-1)
    mom_20d = ((last / float(df["Close"].iloc[-l_20-1])) - 1) * 100 if len(df) > 20 else 0

    vp_lookback = min(180, len(df))
    df_vp = df.tail(vp_lookback)
    try:
        bins = np.linspace(float(df_vp["Low"].min()), float(df_vp["High"].max()), 20)
        vp = pd.cut(df_vp["Close"], bins=bins)
        vp_vol = df_vp.groupby(vp, observed=False)["Volume"].sum()
        poc_price = float(vp_vol.idxmax().mid)
    except Exception: poc_price = 0.0

    df["Vol_MA"] = df["Volume"].rolling(20).mean()
    vol_ratio = (float(df["Volume"].iloc[-1]) / float(df["Vol_MA"].iloc[-1])) if float(df["Vol_MA"].iloc[-1]) > 0 else 1.0

    highs, lows = find_pivots(df, window=10)
    recent_highs = [p[1] for p in highs[-2:]] if len(highs) >= 2 else []
    recent_lows = [p[1] for p in lows[-2:]] if len(lows) >= 2 else []
    is_bull_struct = len(recent_highs) == 2 and recent_highs[1] > recent_highs[0] and len(recent_lows) == 2 and recent_lows[1] > recent_lows[0]

    high_52, low_52 = float(df['High'].max()), float(df['Low'].min())
    fib_618 = high_52 - 0.382 * (high_52 - low_52)
    is_near_ath = last >= high_52 * 0.98

    try:
        hits, _ = analyze_options_flow(ticker, last)
        flow_score_val, _, _ = compute_flow_score(hits) if hits else (0.0, {}, "")
    except Exception:
        flow_score_val = 0.0

    tol = atr * 0.5
    res_clusters = cluster_levels(highs, tol)
    sup_clusters = cluster_levels(lows, tol)
    
    res_levels = sorted([c[0] for c in res_clusters if c[0] > last])
    valid_sup_clusters = sorted([c for c in sup_clusters if c[0] < last], reverse=True)
    nearest_res = res_levels[0] if res_levels else None

    sm_trend = 2 if ema20 > ema50 else 0
    sm_struct = 2 if is_bull_struct else 0
    sm_flow = 2 if flow_score_val > 0.25 else 0
    sm_vol = 1 if vol_ratio > 1.2 else 0
    sm_mom = 1 if mom_20d > 15 else 0
    
    sm_score = sm_trend + sm_struct + sm_flow + sm_vol + sm_mom
    if sm_score >= 6: sm_grade = "🟢 Strong Alignment"
    elif sm_score >= 4: sm_grade = "🟡 Mixed Alignment"
    else: sm_grade = "🔴 Weak Alignment"

    sm_txt = (
        f"Trend: {'✅' if sm_trend else '❌'} | "
        f"Struct: {'✅' if sm_struct else '❌'} | "
        f"Flow: {'✅' if sm_flow else '❌'} | "
        f"Vol: {'✅' if sm_vol else '❌'} | "
        f"Mom: {'✅' if sm_mom else '❌'}"
    )

    global_score = 0
    global_bd = []

    if last < ema200: 
        global_score -= 20
        global_bd.append("Pod EMA200      -20")

    if sm_trend: global_score += 10; global_bd.append("Trend Bullish   +10")
    if rsi < 40: global_score += 10; global_bd.append("RSI Oversold    +10")
    if sm_vol: global_score += 10; global_bd.append("Volume Spike    +10")
    if mom_20d > 30: global_score += 20; global_bd.append("Mom > 30%       +20")
    elif mom_20d > 15: global_score += 10; global_bd.append("Mom > 15%       +10")

    if flow_score_val > 0.7: global_score += 20; global_bd.append("Heavy Bull Flow +20")
    elif flow_score_val > 0.25: global_score += 10; global_bd.append("Bullish Flow    +10")
    elif flow_score_val < -0.7: global_score -= 20; global_bd.append("Heavy Bear Flow -20")
    elif flow_score_val < -0.25: global_score -= 10; global_bd.append("Bearish Flow    -10")

    best_zone = None
    best_zone_bot, best_zone_top = last, last
    best_total_score = -999
    best_zone_bd = []
    best_zone_reasons = []

    for sup, count, cluster in valid_sup_clusters:
        score = global_score
        dist = (last - sup) / sup
        if dist < 0.05: score += 15
        if score > best_total_score:
            best_total_score = score
            best_zone = sup
            best_zone_bot = sup - tol
            best_zone_top = sup + tol
            best_zone_bd = global_bd + ["S/R Zóna Supportu"]
            best_zone_reasons = ["✓ Support Level"]

    is_strong_breakout = last > ema20 and ema20 > ema50 and vol_ratio > 1.2
    
    if best_total_score == -999: 
        if is_near_ath and is_strong_breakout:
            setup_type = "🚀 ATH Breakout"
            best_zone_bot, best_zone_top, best_zone = last * 0.99, last * 1.015, last
            best_total_score = min(global_score + 20, 85)
            best_zone_bd = global_bd + ["ATH Breakout    +20", "Max Skóre Cap   85"]
            best_zone_reasons = ["✓ ATH Momentum", "✓ Breakout"]
        elif is_strong_breakout and (not nearest_res or abs(last - nearest_res)/nearest_res < 0.02):
            setup_type = "🚀 Momentum Breakout"
            best_zone_bot, best_zone_top, best_zone = last * 0.99, last * 1.015, last
            best_total_score = min(global_score + 20, 85)
            best_zone_bd = global_bd + ["Moment Breakout +20"]
            best_zone_reasons = ["✓ Breakout", "✓ Volume Spike"]
        else:
            setup_type = "⚠️ No Setup"
            best_total_score = 0
            best_zone_bot, best_zone_top, best_zone = last, last, last
    else:
        setup_type = "🟢 Pullback Buy"

    best_total_score = min(100, max(0, int(best_total_score)))
    stop_loss = best_zone_bot - atr

    valid_res_for_target = [r for r in res_levels if r > best_zone_top]
    
    if valid_res_for_target:
        target1 = max(valid_res_for_target[0], best_zone + (2 * atr))
    else:
        target1 = best_zone + (3 * atr)

    valid_res_2 = [r for r in res_levels if r > target1]
    if valid_res_2:
        target2 = valid_res_2[0]
    else:
        target2 = best_zone + (6 * atr)

    risk_zone = best_zone - stop_loss
    rew_zone = target1 - best_zone
    rr_zone = rew_zone / risk_zone if risk_zone > 0 else 0

    risk_now = last - stop_loss
    rew_now = target1 - last
    rr_now = rew_now / risk_now if risk_now > 0 else 0

    if best_total_score >= 90: grade = "🟢 A+ Setup"
    elif best_total_score >= 80: grade = "🟢 A Setup"
    elif best_total_score >= 65: grade = "🟡 B Setup"
    elif best_total_score >= 50: grade = "🟠 C Setup"
    else: grade = "🔴 D Setup"

    dist_to_zone_pct = ((last - best_zone_top) / best_zone_top) * 100
    atr_dist = (last - best_zone_top) / atr if atr > 0 else 99
    
    if dist_to_zone_pct <= 0: pullback_risk = "N/A (Již v zóně)"
    elif atr_dist < 1.0: pullback_risk = "Nízký (Blízko)"
    elif atr_dist < 2.5: pullback_risk = "Střední"
    else: pullback_risk = "Vysoký (Přetaženo)"

    if best_total_score < 40 or rr_zone < 1.5:
        summary = "Slabý R:R profil nebo nedostatečná konfluence pro vstup. Neobchodovatelný setup, posuň se dál."
        status = "🔴 VYHNOUT SE"
    elif dist_to_zone_pct > 3:
        summary = f"Cena je nad ideální zónou. R:R nyní je neatraktivní ({rr_now:.1f}), ale uvnitř zóny je excelentní ({rr_zone:.1f}). Nastav si alert."
        status = f"⏳ ČEKEJ NA PULLBACK (-{dist_to_zone_pct:.1f}%)"
    elif dist_to_zone_pct <= 0 and dist_to_zone_pct >= -3:
        summary = "Výborná pozice k nákupu. Trh se nachází v silné Confluence zóně, R:R je nastavené ve tvůj prospěch."
        status = "🟢 V ZÓNĚ (Ideální vstup)"
    else:
        setup_type = "🔄 Reversal"
        summary = "Trh propadl pod klíčovou strukturu. Původní teze padla, hrozí další výplach."
        status = "🔴 PROPADLO ZÓNOU"

    fmt = "%Y-%m-%d %H:%M"
    x_dates = df.index.strftime(fmt)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.8, 0.2])

    fig.add_trace(go.Candlestick(x=x_dates, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"], increasing_line_color="#26a69a", decreasing_line_color="#ef5350", name="Cena"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x_dates, y=df["EMA20"], line=dict(color="#2196F3", width=1.5), name="EMA 20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x_dates, y=df["EMA50"], line=dict(color="#FFC107", width=1.5), name="EMA 50"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x_dates, y=df["EMA200"], line=dict(color="#E0E0E0", width=1), name="EMA 200"), row=1, col=1)

    vol_colors = ['#26a69a' if row['Close'] >= row['Open'] else '#ef5350' for _, row in df.iterrows()]
    fig.add_trace(go.Bar(x=x_dates, y=df["Volume"], marker_color=vol_colors, name="Volume"), row=2, col=1)

    if setup_type != "⚠️ No Setup":
        annotation_html = "<b>BUY ZONE</b><br>" + "<br>".join(best_zone_reasons)
        fig.add_hrect(y0=best_zone_bot, y1=best_zone_top, line_width=0, fillcolor="rgba(38, 166, 154, 0.2)", 
                      annotation_text=annotation_html, annotation_position="top left", annotation_font=dict(color="#26a69a", size=11), row=1, col=1)
        fig.add_hline(y=stop_loss, line_color="#ef5350", line_dash="dash", line_width=2, annotation_text=f"STOP", annotation_position="bottom right", row=1, col=1)
        fig.add_hline(y=target1, line_color="#8bc34a", line_dash="dot", line_width=2, annotation_text=f"T1", row=1, col=1)
        fig.add_hline(y=target2, line_color="#8bc34a", line_dash="dot", line_width=1, annotation_text=f"T2", row=1, col=1)

    fig.update_layout(title=f"🎯 {ticker.upper()} — Exekuční Setup | {interval}", template="plotly_dark", width=1100, height=850, showlegend=False, margin=dict(l=40, r=40, t=60, b=20))
    fig.update_xaxes(rangeslider_visible=False, type="category", nticks=8)
    png = fig.to_image(format="png") if render else None

    score_text = "\n".join([f"  {b}" for b in best_zone_bd]) if best_zone_bd else "  Nedostatečná konfluence pro vstup"
    daily_bias = _daily_bias_from_setup(best_total_score, setup_type, status)
    
    mtf_levels = _safe_mtf_levels(ticker)
    levels_4h = mtf_levels.get("4h") if isinstance(mtf_levels, dict) else None
    levels_1h = mtf_levels.get("1h") if isinstance(mtf_levels, dict) else None

    lines = [
        f"🎯 *SETUP TYPE:* `{setup_type}`",
        f"📌 *Summary:* _{summary}_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏦 *SMART MONEY ALIGNMENT:* `{sm_score} / 8` ({sm_grade})",
        f"`{sm_txt}`",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 *ENTRY CONFIDENCE:* {grade} ({best_total_score}/100)",
        f"```text\n{score_text}\n```",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📍 *Buy Zone:* `${best_zone_bot:.2f} - ${best_zone_top:.2f}`",
        f"💵 *Aktuálně:* `${last:.2f}`",
        f"🚦 *Status:* {status}",
        "",
        "🧭 *MULTI-TIMEFRAME CONTEXT*",
        f"daily_bias: `{daily_bias}`",
        f"support_4h: `{_format_mtf_support(levels_4h)}`",
        f"resistance_4h: `{_format_mtf_resistance(levels_4h)}`",
        f"support_1h: `{_format_mtf_support(levels_1h)}`",
        f"resistance_1h: `{_format_mtf_resistance(levels_1h)}`",
        "",
        f"⏳ *Pullback Risk:* `{pullback_risk}`",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 *TRADE MANAGEMENT*",
        f"🟢 *Target 1:* `${target1:.2f}`",
        f"🟢 *Target 2:* `${target2:.2f}`",
        f"🔴 *Stop:* `${stop_loss:.2f}` (ATR based)",
        "",
        f"⚖️ *R:R (Nyní):* `1 : {rr_now:.1f}`",
        f"⚖️ *R:R (V zóně):* `1 : {rr_zone:.1f}`"
    ]

    # Strukturovaná data pro skenery (aby nemusely parsovat text regexem).
    data = {
        "ticker": ticker.upper(),
        "setup_type": setup_type,
        "score": best_total_score,
        "sm": sm_score,
        "entry_bot": best_zone_bot,
        "entry_top": best_zone_top,
        "entry": f"${best_zone_bot:.2f}-${best_zone_top:.2f}",
        "stop": stop_loss,
        "t1": target1,
        "t2": target2,
        "rr_zone": rr_zone,
        "rr_now": rr_now,
        "mom_ok": sm_mom == 1,
        "vol_ok": sm_vol == 1,
        "last": last,
    }

    return png, "\n".join(lines), data

def make_sr_chart(ticker: str, interval: str):
    try:
        png, summary = make_mtf_chart(ticker, interval)
        if png is not None:
            return png, summary
    except Exception:
        pass

    levels = analyze_mtf_levels(ticker).get(interval)

    if levels is None:
        return None, f"❌ Nedostatek dat pro {ticker.upper()} {interval}."

    lines = [
        f"📍 *S/R ANALÝZA: {ticker.upper()} {interval.upper()}*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"nearest support: `{format_level(levels.nearest_support)}`",
        f"support zone: `{format_zone(levels.support_zone)}`",
        "",
        f"nearest resistance: `{format_level(levels.nearest_resistance)}`",
        f"resistance zone: `{format_zone(levels.resistance_zone)}`",
        "",
        f"swing highs: `{len(levels.swing_highs)}`",
        f"swing lows: `{len(levels.swing_lows)}`",
        f"clustered levels: `{len(levels.clustered_levels)}`",
    ]

    return None, "\n".join(lines)

def analyze_setup(ticker: str):
    _, text, _ = make_chart(ticker, "1d")
    return text

# ==============================================================================
# 3. EARNINGS A OPCE
# ==============================================================================
def analyze_earnings(ticker: str) -> str:
    ticker = ticker.upper()
    tk = yf.Ticker(ticker)

    try:
        inc = tk.quarterly_income_stmt
        rev_now = get_yf_val(inc, "Total Revenue", 0)
        rev_prev = get_yf_val(inc, "Total Revenue", 1)
    except Exception:
        rev_now, rev_prev = None, None

    try:
        cf = tk.quarterly_cashflow
        cash_now = get_yf_val(cf, "End Cash Position", 0)
        cash_prev = get_yf_val(cf, "End Cash Position", 1)
        
        bs = tk.quarterly_balance_sheet
        if cash_now is None:
            cash_now = get_yf_val(bs, "Cash Cash Equivalents And Short Term Investments", 0) or get_yf_val(bs, "Cash", 0)
            cash_prev = get_yf_val(bs, "Cash Cash Equivalents And Short Term Investments", 1) or get_yf_val(bs, "Cash", 1)
        
        debt_now = get_yf_val(bs, "Total Debt", 0)
        debt_prev = get_yf_val(bs, "Total Debt", 1)
    except Exception:
        cash_now, cash_prev, debt_now, debt_prev = None, None, None, None

    rev_growth = safe_pct(rev_now, rev_prev)
    cash_change = safe_pct(cash_now, cash_prev)
    debt_change = safe_pct(debt_now, debt_prev)

    try:
        info = tk.info
        target = info.get("targetMeanPrice")
        current = info.get("currentPrice")
        if target and current and current != 0:
            upside = ((target - current) / current) * 100
        else:
            upside = None
    except Exception:
        upside = None

    url = f"https://www.alphavantage.co/query?function=EARNINGS&symbol={ticker}&apikey={AV_KEY}"
    eps_surprise = None
    eps_actual_val = None
    eps_est_val = None
    eps_debug = ""
    
    try:
        data = requests.get(url, timeout=10).json()
        
        if "Information" in data:
            eps_debug = " ⚠️ (Dosažen denní limit 25 dotazů)"
        elif "Error Message" in data:
            eps_debug = " ⚠️ (Neplatný API klíč)"
        elif "quarterlyEarnings" in data and len(data["quarterlyEarnings"]) > 0:
            quarter = data["quarterlyEarnings"][0]
            
            actual_raw = quarter.get("reportedEPS")
            est_raw = quarter.get("estimatedEPS")
            
            if (actual_raw is not None and est_raw is not None and 
                str(actual_raw).lower() != "none" and str(est_raw).lower() != "none"):
                eps_actual_val = float(actual_raw)
                eps_est_val = float(est_raw)
                
                if eps_est_val != 0:
                    eps_surprise = ((eps_actual_val - eps_est_val) / abs(eps_est_val)) * 100
            else:
                surprise_pct_raw = quarter.get("surprisePercentage")
                if surprise_pct_raw is not None and str(surprise_pct_raw).lower() != "none":
                    eps_surprise = float(surprise_pct_raw)
        else:
            eps_debug = " ⚠️ (Data u Alpha V. nedostupná)"
            
    except Exception as e:
        eps_debug = f" ⚠️ (Chyba API EPS)"

    is_growth = False
    if eps_actual_val is not None and eps_actual_val <= 0:
        is_growth = True
    elif eps_actual_val is None and rev_growth is not None and rev_growth > 20:
        is_growth = True

    score = 0
    
    if is_growth:
        company_type = "🌱 Growth (Důraz na Tržby a Runway)"
        if rev_growth is not None:
            if rev_growth > 50: score += 4
            elif rev_growth > 25: score += 3
            elif rev_growth > 10: score += 2
            elif rev_growth > 0: score += 1
            else: score -= 3
            
        if cash_change is not None:
            if cash_change > 50: score += 2
            elif cash_change > 10: score += 1
            elif cash_change < -30: score -= 2
            
        if debt_change is not None:
            if debt_change < -25: score += 2
            elif debt_change < -10: score += 1
            elif debt_change > 25: score -= 2
            
        if upside is not None:
            if upside > 50: score += 2
            elif upside > 20: score += 1
            elif upside < -10: score -= 1
            
        if eps_surprise is not None:
            if eps_surprise > 20: score += 1
            elif eps_surprise < -50: score -= 1

    else:
        company_type = "🏢 Mature (Důraz na Ziskovost - EPS)"
        if eps_surprise is not None:
            if eps_surprise > 20: score += 4
            elif eps_surprise > 0: score += 3
            elif eps_surprise < 0: score -= 2
            elif eps_surprise < -20: score -= 4
            
        if rev_growth is not None:
            if rev_growth > 20: score += 3
            elif rev_growth > 10: score += 2
            elif rev_growth < 0: score -= 3
            
        if cash_change is not None:
            if cash_change > 10: score += 1
            elif cash_change < -10: score -= 1
            
        if debt_change is not None:
            if debt_change < -10: score += 1
            elif debt_change > 10: score -= 1
            
        if upside is not None:
            if upside > 20: score += 1
            elif upside < -10: score -= 1

    score = max(0, min(10, score))

    if score >= 8: verdict, v_desc = "🔥 Very Bullish", "Excelentní čísla, silné momentum."
    elif score >= 6: verdict, v_desc = "🟢 Bullish", "Solidní a zdravý kvartál."
    elif score >= 4: verdict, v_desc = "🟡 Neutral", "Smíšené výsledky, bez jasného směru."
    elif score >= 2: verdict, v_desc = "🟠 Bearish", "Slabý report, varovné signály."
    else: verdict, v_desc = "🔴 Very Bearish", "Kritický propad v klíčových metrikách."

    def fmt_num(val):
        if val is None: return "N/A"
        sign = "-" if val < 0 else ""
        val = abs(val)
        if val >= 1_000_000_000: return f"{sign}${val/1_000_000_000:.2f}B"
        elif val >= 1_000_000: return f"{sign}${val/1_000_000:.2f}M"
        elif val >= 1_000: return f"{sign}${val/1_000:.1f}K"
        return f"{sign}${val:.2f}"

    def fmt_metric(now, prev, pct):
        pct_str = f"{pct:+.1f}%" if pct is not None else "N/A"
        if now is not None and prev is not None:
            return f"{fmt_num(prev)} ➔ {fmt_num(now)} ({pct_str})"
        return pct_str

    def fmt_eps(act, est, pct):
        pct_str = f"{pct:+.1f}%" if pct is not None else "N/A"
        if act is not None and est is not None:
            return f"Est: ${est:.2f} ➔ Act: ${act:.2f} ({pct_str})"
        return pct_str
    
    def fmt_pct(val): return f"{val:+.1f}%" if val is not None else "N/A"

    lines = [
        f"📑 *EARNINGS REPORT: {ticker.upper()}*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏢 *Model:* {company_type}",
        f"🎯 *Skóre:* `{score} / 10`",
        f"📌 *Verdikt:* {verdict}",
        f"💬 *Shrnutí:* _{v_desc}_",
        "",
        "📈 *Klíčové metriky:*",
        f"  • *EPS Surprise:* {fmt_eps(eps_actual_val, eps_est_val, eps_surprise)}{eps_debug}",
        f"  • *Rev Growth:* {fmt_metric(rev_now, rev_prev, rev_growth)}",
        f"  • *Změna Cash:* {fmt_metric(cash_now, cash_prev, cash_change)}",
        f"  • *Změna Dluhu:* {fmt_metric(debt_now, debt_prev, debt_change)}",
        f"  • *Cíl Analytiků:* {fmt_pct(upside)} (Upside)",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🔗 _Zdroje: Alpha Vantage (EPS), Yahoo Finance_"
    ]
    return "\n".join(lines)

# ── Unusual Options Flow ─────────────────────────────────────────────────────
def _safe(val, default=0.0) -> float:
    try:
        if val is None: return default
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError): return default

def aggression_score(last: float, bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask <= bid: return 0.5
    return max(0.0, min(1.0, (last - bid) / (ask - bid)))

def bullish_score(opt_type: str, last: float, bid: float, ask: float) -> int:
    if bid <= 0 or ask <= 0 or ask <= bid: return 0
    spread   = ask - bid
    midpoint = bid + spread / 2
    on_ask   = last >= ask  - spread * 0.05
    above_mid = last >= midpoint + spread * 0.15
    below_mid = last <= midpoint - spread * 0.15
    on_bid   = last <= bid  + spread * 0.05

    if opt_type == "call":
        if on_ask: return +2
        if above_mid: return +1
        if below_mid: return -1
        if on_bid: return -2
    else:
        if on_ask: return -2
        if above_mid: return -1
        if below_mid: return +1
        if on_bid: return +2
    return 0

def moneyness(strike: float, spot: float, opt_type: str) -> tuple[str, float]:
    if spot <= 0: return ("ATM", 1.0)
    pct = (spot - strike) / spot if opt_type == "call" else (strike - spot) / spot
    if pct > 0.15: return ("Deep ITM", 0.35)
    if pct > 0.03: return ("ITM", 0.80)
    if pct > -0.03: return ("ATM", 1.00)
    if pct > -0.10: return ("OTM", 0.90)
    return ("Lottery OTM", 0.60)

def expected_move(spot: float, iv: float, dte: int) -> float:
    if iv <= 0 or dte <= 0: return 0.0
    return spot * iv * math.sqrt(dte / 365)

def exec_label(last: float, bid: float, ask: float) -> str:
    if bid <= 0 or ask <= 0 or ask <= bid: return "❓ neznámá"
    spread   = ask - bid
    midpoint = bid + spread / 2
    if last >= ask  - spread * 0.05: return "🔥 ASK"
    if last >= midpoint + spread * 0.15: return "↗️ nad mid"
    if last <= bid  + spread * 0.05: return "🧊 BID"
    if last <= midpoint - spread * 0.15: return "↘️ pod mid"
    return "➡️ midpoint"

def analyze_options_flow(ticker: str, spot: float) -> tuple[list[dict], float]:
    if spot <= 0: return [], 0.0
    tk = yf.Ticker(ticker)
    
    try:
        info = tk.info
        market_cap = float(info.get("marketCap") or info.get("totalAssets") or 0.0)
    except Exception: market_cap = 0.0
        
    try: expirations = tk.options
    except Exception: return [], market_cap
    if not expirations: return [], market_cap

    today = datetime.now(timezone.utc).date()
    agg: dict[tuple, dict] = {}

    for exp_str in expirations[:15]:
        try: exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError: continue
            
        dte = (exp_date - today).days
        if not (7 <= dte <= 90): continue

        try: chain = tk.option_chain(exp_str)
        except Exception: continue

        for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(-1)

            for _, row in df.iterrows():
                vol    = _safe(row.get("volume"))
                oi     = _safe(row.get("openInterest"))
                last   = _safe(row.get("lastPrice"))
                bid    = _safe(row.get("bid"))
                ask    = _safe(row.get("ask"))
                iv_raw = _safe(row.get("impliedVolatility"))
                strike = _safe(row.get("strike"))
                
                if iv_raw > 5 or vol < 100 or oi < 1 or last <= 0 or (vol / oi < 3.0): continue
                premium = vol * last * 100
                if premium < 50_000: continue

                mon_label, delta_w = moneyness(strike, spot, opt_type)
                aggr   = aggression_score(last, bid, ask)
                bscore = bullish_score(opt_type, last, bid, ask)
                wscore = premium * delta_w * aggr
                em     = expected_move(spot, iv_raw, dte)

                key = (opt_type, strike, exp_str)
                if key in agg:
                    a = agg[key]
                    total_vol = a["volume"] + int(vol)
                    a["last"]        = (a["last"] * a["volume"] + last * vol) / total_vol
                    a["bid"]         = (a["bid"]  + bid)  / 2
                    a["ask"]         = (a["ask"]  + ask)  / 2
                    a["volume"]      = total_vol
                    a["premium"]     += premium
                    a["wscore"]      += wscore
                    a["bscore_sum"]  += bscore
                    a["executions"]  += 1
                    a["oi"]          = max(a["oi"], int(oi))
                    a["ratio"]       = round(a["volume"] / max(a["oi"], 1), 1)
                else:
                    agg[key] = {
                        "opt_type": opt_type, "strike": strike, "exp": exp_str, "dte": dte,
                        "volume": int(vol), "oi": int(oi), "ratio": round(vol / oi, 1),
                        "last": last, "bid": bid, "ask": ask, "iv": round(iv_raw * 100, 1),
                        "premium": premium, "wscore": wscore, "bscore_sum": bscore,
                        "executions": 1, "moneyness": mon_label, "delta_w": delta_w, "em": em,
                    }

    if not agg: return [], market_cap
    results = list(agg.values())
    results.sort(key=lambda x: x["wscore"], reverse=True)
    return results[:12], market_cap

def compute_flow_score(hits: list[dict]) -> tuple[float, dict, str]:
    buckets = {"bull_call": 0.0, "bear_call": 0.0, "bull_put":  0.0, "bear_put":  0.0, "neutral":   0.0}
    for h in hits:
        bs, p, ot = h["bscore_sum"], h["wscore"], h["opt_type"]
        if bs >= 1: buckets["bull_call" if ot == "call" else "bull_put"] += p
        elif bs <= -1: buckets["bear_call" if ot == "call" else "bear_put"] += p
        else: buckets["neutral"] += p

    bullish = buckets["bull_call"] + buckets["bull_put"]
    bearish = buckets["bear_call"] + buckets["bear_put"]
    neutral = buckets["neutral"]
    
    total_score_weight = bullish + bearish + neutral
    score = (bullish - bearish) / total_score_weight if total_score_weight > 0 else 0.0
    total_premium = sum(h["premium"] for h in hits)

    if total_premium >= 5_000_000: confidence = "🟢 Vysoká"
    elif total_premium >= 1_000_000: confidence = "🟡 Střední"
    else: confidence = "🔴 Nízká"
        
    return score, buckets, confidence

def fmt_usd(val: float) -> str:
    if val >= 1_000_000: return f"${val/1_000_000:.2f}M"
    if val >= 1_000: return f"${val/1_000:.0f}K"
    return f"${val:.0f}"

def format_unusual(ticker: str, hits: list[dict], spot: float, market_cap: float) -> str:
    if not hits:
        return f"🌊 *Unusual Flow: {ticker.upper()}*\n✅ Žádná neobvyklá aktivita (Vol/OI ≥ 3×, prémium ≥ $50k)."

    flow_score, buckets, confidence = compute_flow_score(hits)
    
    if flow_score >= 0.6: fs_label = "🔥 Silně Bullish"
    elif flow_score >= 0.2: fs_label = "📈 Mírně Bullish"
    elif flow_score >= -0.2: fs_label = "➡️ Neutrální"
    elif flow_score >= -0.6: fs_label = "📉 Mírně Bearish"
    else: fs_label = "🧊 Silně Bearish"

    raw_buckets = {"bullish": 0.0, "bearish": 0.0, "neutral": 0.0}
    total_premium = 0.0
    for h in hits:
        bs, pr = h["bscore_sum"], h["premium"]
        total_premium += pr
        if bs >= 1: raw_buckets["bullish"] += pr
        elif bs <= -1: raw_buckets["bearish"] += pr
        else: raw_buckets["neutral"] += pr

    lines = [
        f"🌊 *UNUSUAL OPTIONS FLOW: {ticker.upper()}*",
        f"💵 Cena akcie: `${spot:.2f}` | 🔎 Detekováno signálů: `{len(hits)}`"
    ]
    
    if market_cap > 0:
        lines.append(f"📊 Market Impact: `{(total_premium / market_cap) * 100:.4f}%`")
    
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "⚖️ *Net Premium Breakdown:*",
        f"  🟢 Bullish: {fmt_usd(raw_buckets['bullish'])}",
        f"  🔴 Bearish: {fmt_usd(raw_buckets['bearish'])}",
        f"  ⚪ Neutral: {fmt_usd(raw_buckets['neutral'])}",
        "",
        f"🌡 *FlowScore:* `{flow_score:+.2f}` — {fs_label}",
        f"🛡 *Důvěra:* {confidence}",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ])

    for i, h in enumerate(hits, 1):
        ot_emoji = "📞 CALL" if h["opt_type"] == "call" else "📉 PUT"
        pct_diff = ((h["strike"] - spot) / spot * 100) if spot > 0 else 0
        bid_ask = f"${h['bid']:.2f} / ${h['ask']:.2f}" if h["bid"] > 0 and h["ask"] > 0 else "_mimo trh_"
        sweep_str = f" ⚡ Sweep ({h['executions']}×)" if h["executions"] >= 2 else ""
        em_str = f"| EM ±${h['em']:.1f}" if h["em"] > 0 else ""
        
        whale = "🐳 *MEGA WHALE*" if h["premium"] >= 1_000_000 else ("🐋 *WHALE ALERT*" if h["premium"] >= 300_000 else "")
        if whale: lines.append(f"{whale}")

        bs = h["bscore_sum"]
        if bs >= 2: sent = "🔥 Bullish"
        elif bs == 1: sent = "↗️ Mírně Bullish"
        elif bs == 0: sent = "➡️ Neutrální"
        elif bs == -1: sent = "↘️ Mírně Bearish"
        else: sent = "🧊 Bearish"

        lines.append(
            f"*{i}.* {ot_emoji} *${h['strike']:.0f}* | Exp: {h['exp']} ({h['dte']}d){sweep_str}\n"
            f"  💰 Prémium: `{fmt_usd(h['premium'])}` | IV: {h['iv']}% {em_str}\n"
            f"  📊 Vol: {h['volume']:,} | OI: {h['oi']:,} | Ratio: `{h['ratio']}×`\n"
            f"  🏷 {h['moneyness']} | 🎯 {pct_diff:+.1f}% od ceny\n"
            f"  🛠 B/A: {bid_ask} | Last: ${h['last']:.2f}\n"
            f"  🧭 {exec_label(h['last'], h['bid'], h['ask'])} | Sentiment: {sent}\n"
        )

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🔗 _Zdroj: Yahoo Finance Options Chain_"
    ])
    return "\n".join(lines)

async def get_net_whale_flow(ticker: str) -> dict:
    try:
        tk = yf.Ticker(ticker)
        hist = await asyncio.to_thread(tk.history, period="1d")
        if hist.empty:
            return None
            
        spot = float(hist["Close"].iloc[-1])
        hits, market_cap = await asyncio.to_thread(analyze_options_flow, ticker, spot)
        
        if not hits:
            return None
            
        flow_score, _, _ = compute_flow_score(hits)
        
        bullish_prem = 0.0
        bearish_prem = 0.0
        
        for h in hits:
            bs = h["bscore_sum"]
            pr = h["premium"]
            if bs >= 1:
                bullish_prem += pr
            elif bs <= -1:
                bearish_prem += pr
                
        net_flow = bullish_prem - bearish_prem
        flow_strength = (net_flow / market_cap) * 100 if market_cap > 0 else 0.0
        
        return {
            "ticker": ticker,
            "net_flow": net_flow,
            "flow_strength": flow_strength,
            "flow_score": flow_score,
            "market_cap": market_cap
        }
        
    except Exception:
        return None

# ==============================================================================
# 4. TELEGRAM HANDLERY
# ==============================================================================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Ahoj! Jsem tvůj analytický bot.*\n\n"
        "Pošli mi ticker (např. `AAPL`) pro S/R úrovně a graf. Můžeš přidat i timeframe:\n"
        "• `AAPL` – denní svíčky (1d)\n"
        "• `RKLB 4h` – 4hodinové svíčky\n"
        "• _Podporované TF: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1wk, 1mo_\n\n"
        "🛠 *Další příkazy:*\n"
        "📰 `/news ONDS` – Nejnovější zprávy\n"
        "🌊 `/unusual AAPL` – Detekce velkých opčních obchodů (Whale activity)\n"
        "📑 `/earnings ASTS` – Hodnocení posledních kvartálních výsledků\n\n"
        "ℹ️ Kompletní seznam příkazů: `/help`",
        parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *PŘEHLED PŘÍKAZŮ*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*📊 Grafy & setupy*\n"
        "• `AAPL` nebo `RKLB 4h` – graf + S/R úrovně (TF: 1m,5m,15m,30m,1h,4h,1d,1wk,1mo)\n"
        "• `/setup ASTS` – Confluence setup (entry zóna, stop, targety, R:R)\n"
        "• `/smc ASTS` – Smart Money Concepts (Order Blocks, FVG, sweepy)\n"
        "• `/sniper ASTS` – alert na zásah OB zóny (vypnutí: `/sniper off ASTS`)\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🔍 Skenery*\n"
        "• `/nasdaq` – TOP 10 setupů z NASDAQ-100\n"
        "• `/darkhorse` – skryté příležitosti z Russell 2000\n"
        "• `/whales` – ranní whale-flow skener\n"
        "• `/unusual AAPL` – neobvyklá opční aktivita\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🤖 AI & fundament*\n"
        "• `/news ONDS` – AI sentiment z nejnovějších zpráv\n"
        "• `/earnings ASTS` – hodnocení posledních výsledků\n"
        "• `/ai` (s PDF) – tvrdý výtah z prezentace/reportu\n"
        "• `/walter`, `/testwalter` – makro market alerty",
        parse_mode="Markdown")

last_market_text = ""

def get_smc_zones(df):
    """Vylepšená HFT funkce: Displacement filtr, Proximity filtr a FIRST TOUCH mitigace."""
    bull_fvg, bear_fvg = [], []
    bull_ob, bear_ob = [], []
    
    # Průměrné tělo svíčky pro filtr momenta
    df['body'] = abs(df['Close'] - df['Open'])
    avg_body = float(df['body'].mean())
    current_price = float(df['Close'].iloc[-1])
    
    for i in range(2, len(df) - 1):
        p2_h, p2_l = float(df['High'].iloc[i-2]), float(df['Low'].iloc[i-2])
        c_h, c_l = float(df['High'].iloc[i]), float(df['Low'].iloc[i])
        
        # Svíčka musí být o 50% větší než průměr (Displacement)
        displacement = abs(float(df['Close'].iloc[i-1]) - float(df['Open'].iloc[i-1]))
        if displacement < avg_body * 1.5: 
            continue
            
        # Bullish FVG & OB
        if c_l > p2_h:
            # FIRST TOUCH: Mitigace FVG hned při prvním dotyku knotem shora (c_l)
            mitigated = any(float(df['Low'].iloc[j]) <= c_l for j in range(i + 1, len(df)))
            if not mitigated:
                bull_fvg.append({'top': c_l, 'bot': p2_h, 'start_idx': df.index[i-1]})
                for k in range(i-2, max(0, i-7), -1):
                    if float(df['Close'].iloc[k]) < float(df['Open'].iloc[k]): 
                        ob_t, ob_b = float(df['High'].iloc[k]), float(df['Low'].iloc[k])
                        # FIRST TOUCH: Mitigace OB dotykem shora (ob_t)
                        ob_mitig = any(float(df['Low'].iloc[m]) <= ob_t for m in range(k + 1, len(df)))
                        if not ob_mitig: 
                            bull_ob.append({'top': ob_t, 'bot': ob_b, 'start_idx': df.index[k]})
                        break
                        
        # Bearish FVG & OB
        if c_h < p2_l:
            # FIRST TOUCH: Mitigace FVG hned při prvním dotyku knotem zdola (c_h)
            mitigated = any(float(df['High'].iloc[j]) >= c_h for j in range(i + 1, len(df)))
            if not mitigated:
                bear_fvg.append({'top': p2_l, 'bot': c_h, 'start_idx': df.index[i-1]})
                for k in range(i-2, max(0, i-7), -1):
                    if float(df['Close'].iloc[k]) > float(df['Open'].iloc[k]): 
                        ob_t, ob_b = float(df['High'].iloc[k]), float(df['Low'].iloc[k])
                        # FIRST TOUCH: Mitigace OB dotykem zdola (ob_b)
                        ob_mitig = any(float(df['High'].iloc[m]) >= ob_b for m in range(k + 1, len(df)))
                        if not ob_mitig: 
                            bear_ob.append({'top': ob_t, 'bot': ob_b, 'start_idx': df.index[k]})
                        break
                        
    # PROXIMITY FILTER: Ponecháme jen 3 nejbližší zóny k aktuální ceně
    bull_ob = sorted(bull_ob, key=lambda x: abs(current_price - x['top']))[:3]
    bear_ob = sorted(bear_ob, key=lambda x: abs(current_price - x['bot']))[:3]
    bull_fvg = sorted(bull_fvg, key=lambda x: abs(current_price - x['top']))[:3]
    bear_fvg = sorted(bear_fvg, key=lambda x: abs(current_price - x['bot']))[:3]
                        
    return bull_fvg, bear_fvg, bull_ob, bear_ob

async def smc_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Použití: `/smc ONDS`", parse_mode="Markdown")
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Kompletuji Premium SMC Profil pro *{ticker}*...", parse_mode="Markdown")
    
    try:
        df = await asyncio.to_thread(yf.download, ticker, period="5d", interval="15m", progress=False)
        if df.empty: return await msg.edit_text(f"❌ Žádná data pro {ticker}.")
        if isinstance(df.columns, pd.MultiIndex):
            df = df[ticker] if ticker in df.columns.levels[0] else df.copy()
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                
        df = df.tail(150).copy()
        
        # Volání nového výpočetního jádra
        bull_fvg, bear_fvg, bull_ob, bear_ob = get_smc_zones(df)
        
        # Detekce Sweepů (Liquidity Grabs) a BoS/CHoCH
        sweeps = []
        structures = []
        
        for i in range(20, len(df) - 1):
            roll_high = float(df['High'].iloc[i-20:i-1].max())
            roll_low = float(df['Low'].iloc[i-20:i-1].min())
            c_high, c_low, c_close, c_open = float(df['High'].iloc[i]), float(df['Low'].iloc[i]), float(df['Close'].iloc[i]), float(df['Open'].iloc[i])
            
            if c_high > roll_high and c_close < roll_high:
                sweeps.append({'idx': df.index[i], 'val': c_high, 'type': 'Bear Sweep 🧹', 'color': '#ef5350'})
            if c_low < roll_low and c_close > roll_low:
                sweeps.append({'idx': df.index[i], 'val': c_low, 'type': 'Bull Sweep 🧹', 'color': '#26a69a'})
            if c_close > roll_high and c_open < roll_high:
                structures.append({'idx': df.index[i], 'val': roll_high, 'type': 'BoS/CHoCH (Up)', 'color': '#2196F3'})
            if c_close < roll_low and c_open > roll_low:
                structures.append({'idx': df.index[i], 'val': roll_low, 'type': 'BoS/CHoCH (Down)', 'color': '#FF9800'})

        recent_high = float(df['High'].tail(30).max())
        recent_low = float(df['Low'].tail(30).min())
        
        # Kreslení - Profesionální TradingView styl
        fig = go.Figure(data=[go.Candlestick(
            x=df.index.strftime("%Y-%m-%d %H:%M"), 
            open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350", name="Cena"
        )])
        
        fmt = "%Y-%m-%d %H:%M"
        end_str = df.index[-1].strftime(fmt)
        
        def draw_zone(zones, color, border, name):
            for z in zones:
                start_str = z['start_idx'].strftime(fmt)
                fig.add_shape(type="rect", x0=start_str, x1=end_str, y0=z['bot'], y1=z['top'], 
                              fillcolor=color, line=dict(color=border, width=1), layer="below")
                fig.add_annotation(x=start_str, y=(z['top']+z['bot'])/2, text=name, showarrow=False, 
                                   font=dict(color=border, size=10), xanchor="left")

        draw_zone(bull_ob, "rgba(76, 175, 80, 0.25)", "#4CAF50", "+OB")
        draw_zone(bear_ob, "rgba(244, 67, 54, 0.25)", "#F44336", "-OB")
        draw_zone(bull_fvg, "rgba(38, 166, 154, 0.1)", "rgba(38, 166, 154, 0)", "FVG")
        draw_zone(bear_fvg, "rgba(239, 83, 80, 0.1)", "rgba(239, 83, 80, 0)", "FVG")
            
        for sw in sweeps[-3:]: 
            fig.add_annotation(x=sw['idx'].strftime(fmt), y=sw['val'], text=sw['type'], showarrow=True, arrowhead=1, arrowsize=1, arrowwidth=1.5, arrowcolor=sw['color'], ax=0, ay=-20 if 'Bear' in sw['type'] else 20, font=dict(color=sw['color'], size=9))
            
        for st in structures[-2:]: 
            fig.add_hline(y=st['val'], line_dash="dot", line_color=st['color'], annotation_text=st['type'], annotation_position="left", opacity=0.4)

        fig.add_hline(y=recent_high, line_dash="dash", line_color="rgba(255, 193, 7, 0.5)", annotation_text="BSL", annotation_position="top right", annotation_font=dict(color="rgba(255,193,7,0.7)", size=9))
        fig.add_hline(y=recent_low, line_dash="dash", line_color="rgba(255, 193, 7, 0.5)", annotation_text="SSL", annotation_position="bottom right", annotation_font=dict(color="rgba(255,193,7,0.7)", size=9))
        
        current_price = float(df['Close'].iloc[-1])
        eq_level = (recent_high + recent_low) / 2
        
        if current_price < recent_high and current_price > recent_low:
            fig.add_hline(y=eq_level, line_dash="dot", line_color="rgba(158, 158, 158, 0.4)", annotation_text="EQ (50%)", annotation_position="bottom right")

        fig.update_layout(title=f"🎯 Premium SMC | {ticker} (15m)", template="plotly_dark", width=1100, height=700, showlegend=False, margin=dict(l=30, r=40, t=50, b=20), xaxis_rangeslider_visible=False, xaxis_type="category", xaxis_nticks=6)
        
        png = fig.to_image(format="png")
        
        pd_status = "🔴 Premium" if current_price > eq_level else "🟢 Discount"
        text_zpravy = (
            f"🎯 *Premium SMC Profil: {ticker} (15m)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚖️ *P/D Zóna:* `{pd_status}`\n"
            f"🎯 *Klíčové aktivní OBs:* `Bull {len(bull_ob)} | Bear {len(bear_ob)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _Graf nyní ukazuje čisté a přesné zóny. Začínají na svíčce svého vzniku a filtrují jen ty nejsilnější (Displacement)._\n"
            f"⚡ _Sniper alert:_ `/sniper {ticker}`"
        )
        
        await msg.delete()
        await update.message.reply_photo(photo=io.BytesIO(png), caption=text_zpravy, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Chyba SMC analýzy: {e}")

       # ==============================================================================
# 3. SMC SNIPER (Sledování na pozadí)
# ==============================================================================
async def sniper_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        return await update.message.reply_text("Použití: `/sniper AAPL`\n_Vypnutí:_ `/sniper off AAPL`", parse_mode="Markdown")
        
    prikaz = ctx.args[0].upper()
    
    if chat_id not in active_snipers:
        active_snipers[chat_id] = set()

    if prikaz == "OFF":
        if len(ctx.args) > 1:
            ticker = ctx.args[1].upper()
            if ticker in active_snipers[chat_id]:
                active_snipers[chat_id].remove(ticker)
                save_snipers()
                await update.message.reply_text(f"🔕 Sniper pro *{ticker}* byl deaktivován.", parse_mode="Markdown")
        else:
            active_snipers[chat_id].clear()
            save_snipers()
            await update.message.reply_text(f"🔕 Všichni SMC Snipeři byli deaktivováni.", parse_mode="Markdown")
        return

    ticker = prikaz
    active_snipers[chat_id].add(ticker)
    save_snipers()
    await update.message.reply_text(
        f"🎯 *SMC SNIPER AKTIVOVÁN: {ticker}*\n"
        f"Bot nyní každou minutu skenuje graf na pozadí. Jakmile cena zasáhne nezasažený (unmitigated) Order Block, pošlu ti okamžitý alert.", 
        parse_mode="Markdown"
    )

async def sniper_background_task(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, tickers in list(active_snipers.items()):
        for ticker in list(tickers):
            try:
                df = await asyncio.wait_for(
                    asyncio.to_thread(yf.download, ticker, period="3d", interval="15m", progress=False),
                    timeout=20.0,
                )
                if df.empty: continue
                if isinstance(df.columns, pd.MultiIndex):
                    df = df[ticker] if ticker in df.columns.levels[0] else df.copy()
                    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                
                bull_fvg, bear_fvg, bull_ob, bear_ob = get_smc_zones(df)
                current_price, current_low, current_high = float(df['Close'].iloc[-1]), float(df['Low'].iloc[-1]), float(df['High'].iloc[-1])
                
                alert_msg = ""
                for ob in bull_ob:
                    if current_low <= ob['top'] and current_price >= ob['bot']:
                        alert_msg = f"🟢 *LONG ALERT ({ticker})*\nCena propíchla Bullish Order Block (`${ob['top']:.2f}`). Hledej long!"
                        break
                for ob in bear_ob:
                    if current_high >= ob['bot'] and current_price <= ob['top']:
                        alert_msg = f"🔴 *SHORT ALERT ({ticker})*\nCena zasáhla Bearish Order Block (`${ob['bot']:.2f}`). Hledej short!"
                        break

                if alert_msg:
                    await context.bot.send_message(chat_id=chat_id, text=f"🎯 *SMC SNIPER HIT*\n━━━━━━━━━━━━━━━━━━━━━━\n{alert_msg}", parse_mode="Markdown")
                    active_snipers[chat_id].remove(ticker)
                    save_snipers()
            except asyncio.TimeoutError:
                log.warning("Sniper: timeout při stahování %s", ticker)
            except Exception as e:
                log.error("Sniper chyba u %s: %s", ticker, e)


# Tuhle proměnnou musíme definovat ZVENKU před funkcí, aby se na ni mohl globálně odkazovat
last_market_text = ""

async def walter_macro_loop(context: ContextTypes.DEFAULT_TYPE):
    """Hlavní smyčka na pozadí pro makro zprávy z Telegram zrcadla (Anti-X-Ban)"""
    global last_market_text
    
    try:
        # 1. OPRAVA CACHE: Přidáme do URL aktuální čas vteřinách, Telegram vždy vrátí nejnovější stav
        url = f"https://t.me/s/marketfeed?nocache={int(time.time())}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        
        resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        if resp.status_code != 200:
            log.warning("[WALTER] Feed vrátil HTTP %s — přeskakuji tento cyklus.", resp.status_code)
            return

        soup = BeautifulSoup(resp.text, 'html.parser')
        zpravy = soup.find_all('div', class_='tgme_widget_message_text')
        if not zpravy:
            log.warning("[WALTER] Feed neobsahuje žádné zprávy (změna struktury stránky?).")
            return
            
        text_tweetu = zpravy[-1].get_text(separator=" ", strip=True)
        
        # 2. BEZPEČNÉ NAČTENÍ PAMĚTI: Zabrání tichému pádu při prázdném souboru
        pamet_soubor = "posledni_tweet.txt"
        if os.path.exists(pamet_soubor):
            with open(pamet_soubor, "r", encoding="utf-8") as f:
                last_market_text = f.read().strip()
                
        # 3. KONTROLA DUPLICITY — rychlá (poslední zpráva) + rolling hash historie
        #    (odolná i vůči restartu a drobně přeposlaným/opakovaným titulkům)
        if text_tweetu == last_market_text or _walter_seen_check_and_remember(text_tweetu):
            # Je to stará/už zpracovaná zpráva, nic neděláme
            return

        # 4. JE TO NOVÉ! Uložíme do paměti a jdeme pracovat
        log.info("[WALTER] Nová zpráva: %s...", text_tweetu[:50])
        with open(pamet_soubor, "w", encoding="utf-8") as f:
            f.write(text_tweetu)

        last_market_text = text_tweetu

        # ... (zde pokračuje tvůj stávající kód: # --- FÁZE 1: AI DETEKCE TICKERU...)

        # --- FÁZE 1: AI DETEKCE TICKERU PŘES GROQ (JSON) ---
        prompt_detekce = f"""
        Jsi HFT quant algoritmus. Přečti si tuto bleskovou zprávu:
        "{text_tweetu}"
        
        Urči, zda má tato zpráva reálný dopad na americký akciový trh (NASDAQ, technologické akcie) nebo krypto.
        Ignoruj lokální zprávy z Evropy a Asie (např. švýcarské ceny, lokální ekonomika mimo USA), pokud nemají globální vliv.
        Pokud zpráva nemá vliv na US trh, označ typ jako "ignore".
        
        Odpověz POUZE v čistém formátu JSON:
        {{
            "typ": "akcie", "makro" nebo "ignore",
            "ticker": "TICKER" (pokud akcie, napiš ticker. Pokud makro, napiš "NVDA". Pokud ignore, napiš "NONE"),
            "sentiment": "bullish" nebo "bearish" nebo "neutral",
            "duvod": "stručný důvod 1 větou"
        }}
        """
        
        ai_raw = await ask_groq(prompt_detekce, temperature=0)
        if ai_raw is None:
            return
        ai_text = ai_raw.replace("```json", "").replace("```", "").strip()

        try:
            analyza = json.loads(ai_text)
        except Exception:
            analyza = {"typ": "ignore", "ticker": "NONE", "sentiment": "neutral", "duvod": "Chyba parsování"}
            
        # Zastavení nesmyslných zpráv hned na začátku
        if analyza.get("typ") == "ignore":
            log.info("Walter zahodil irelevantní zprávu: %s", text_tweetu)
            return
            
        # --- FÁZE 2: VOLUME SPIKE + ATR STOP ---
        target_ticker = analyza.get("ticker")
        typ = analyza.get("typ")
        is_weekend = datetime.now().weekday() >= 5
        session = us_market_session()

        if is_weekend:
            # O víkendu obchoduje jen krypto → BTC jako proxy/aktivum.
            target_ticker = "BTC-USD"
            is_macro = True
        elif not target_ticker or str(target_ticker).lower() in ("none", "null"):
            # Nemáme konkrétní ticker → bereme to čistě jako makro a NVDA je jen teploměr objemu.
            target_ticker = WALTER_DEFAULT_TICKER
            is_macro = True
        else:
            target_ticker = target_ticker.upper()
            is_macro = (typ == "makro")

        sentiment = analyza.get("sentiment", "neutral").lower()
        is_btc = (target_ticker == "BTC-USD")
        # Mimo obchodní hodiny jsou akciová data stará → entry/stop nedává smysl.
        equity_tradable = is_btc or (session in ("regular", "pre", "after"))

        aktualni_cena = 0.0
        aktualni_vol = 0.0
        prumer_vol_10m = 0.0
        atr = None
        ma_data = False

        if is_btc:
            try:
                url_binance = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20"
                resp_binance = await asyncio.to_thread(requests.get, url_binance, timeout=5)
                if resp_binance.status_code == 200:
                    k_data = resp_binance.json()
                    if len(k_data) >= 11:
                        df_btc = pd.DataFrame(k_data).iloc[:, 1:6]
                        df_btc.columns = ["Open", "High", "Low", "Close", "Volume"]
                        df_btc = df_btc.astype(float)
                        aktualni_cena = float(df_btc["Close"].iloc[-1])
                        aktualni_vol = float(df_btc["Volume"].iloc[-1])
                        prumer_vol_10m = float(df_btc["Volume"].iloc[-11:-1].mean())
                        atr = compute_atr(df_btc)
                        ma_data = True
            except Exception as e:
                log.error("Chyba Binance API: %s", e)

        else:
            data_1m = await asyncio.to_thread(yf.download, target_ticker, period="1d", interval="1m", progress=False)
            if not data_1m.empty and len(data_1m) > 10:
                if isinstance(data_1m.columns, pd.MultiIndex):
                    if 'Close' in data_1m.columns.get_level_values(0):
                        data_1m.columns = data_1m.columns.get_level_values(0)
                    else:
                        data_1m.columns = data_1m.columns.get_level_values(-1)

                data_1m.columns = [str(c).strip() for c in data_1m.columns]

                if 'Close' in data_1m.columns and 'Volume' in data_1m.columns:
                    aktualni_cena = float(data_1m['Close'].iloc[-1])
                    aktualni_vol = float(data_1m['Volume'].iloc[-1])
                    prumer_vol_10m = float(data_1m['Volume'].tail(11).head(10).mean())
                    atr = compute_atr(data_1m)
                    ma_data = True

        # Směr + stop z ATR (s fallbackem na pevné % když ATR chybí).
        smer = ""
        stop_loss = 0.0
        if ma_data and aktualni_cena > 0:
            risk_abs = (WALTER_ATR_MULT * atr) if (atr and atr > 0) else (aktualni_cena * FIXED_RISK_PCT_FALLBACK)
            if sentiment == "bullish":
                smer = "LONG 🟢"
                stop_loss = aktualni_cena - risk_abs
            elif sentiment == "bearish":
                smer = "SHORT 🔴"
                stop_loss = aktualni_cena + risk_abs

        spike = ma_data and prumer_vol_10m > 0 and (aktualni_vol > prumer_vol_10m * WALTER_VOL_SPIKE)

        # NEWS ENTRY jen pro konkrétní akcii/krypto (NE makro proxy), v obchodních
        # hodinách, při objemovém spiku, jasném sentimentu a po vychladnutí cooldownu.
        if (not is_macro) and equity_tradable and spike and smer and _walter_cooldown_ok(target_ticker):
            risk_display = abs(aktualni_cena - stop_loss) / aktualni_cena * 100
            stop_basis = f"{WALTER_ATR_MULT:g}×ATR" if (atr and atr > 0) else f"{FIXED_RISK_PCT_FALLBACK*100:g}% fallback"
            vol_mult = aktualni_vol / prumer_vol_10m
            confidence = _walter_confidence(vol_mult)

            zprava = f"⚡ *NEWS ENTRY DETECTED: {target_ticker}*\n"
            zprava += f"━━━━━━━━━━━━━━━━━━━━━━\n"
            zprava += f"📰 *Katalyzátor:* _{analyza.get('duvod', '')}_\n"
            zprava += f"📈 *Síla signálu:* {confidence} ({vol_mult:.1f}x objem)\n"
            if is_btc:
                zprava += f"📊 *1m Volume Spike:* `{aktualni_vol:,.2f} BTC` ({aktualni_vol/prumer_vol_10m:.1f}x normál)\n\n"
            else:
                zprava += f"📊 *1m Volume Spike:* `{aktualni_vol:,.0f}` ({aktualni_vol/prumer_vol_10m:.1f}x normál)\n"
                if session != "regular":
                    zprava += f"🕒 _Seance: {session.upper()} (mimo hlavní hodiny)_\n"
                zprava += "\n"
            zprava += f"🎯 *Akce:* `{smer}`\n"
            zprava += f"💵 *Vstup:* `${aktualni_cena:.2f}`\n"
            zprava += f"🛑 *Stop:* `${stop_loss:.2f}` (Risk {risk_display:.2f}% · {stop_basis})\n"
            zprava += f"⚠️ *Sizing:* `Max 0.5% portfolia!`"

            await context.bot.send_message(chat_id=context.job.chat_id, text=zprava, parse_mode="Markdown")
            _walter_mark_alert(target_ticker)
            return

        # --- FÁZE 3: MAKRO ALERT (bez konkrétního entry/stop) ---
        prompt_makro = f"""
        Jsi institucionální quant analytik. Zde je nejnovější blesková zpráva z trhu:
        "{text_tweetu}"

        Tato zpráva není o jedné firmě, ale o makroekonomickém dění.
        Vygeneruj PŘESNĚ tento výstup pro tradera:

        🌍 *Překlad:* [Český přesný překlad]
        📉 *Impact Nasdaq:* [např. -0.8% nebo +1.2%] ([stručný důvod])
        🤖 *AI Analýza:* [1 úderná věta makro-kontextu]
        """

        makro_text = await ask_groq(prompt_makro, temperature=0.2)
        if makro_text is None:
            return

        vol_info = ""
        if ma_data and prumer_vol_10m > 0:
            nasobek = aktualni_vol / prumer_vol_10m
            teplomer = "🔥 zvýšený" if nasobek >= WALTER_VOL_SPIKE else "klidný"
            if is_btc:
                vol_info = f"\n\n📊 *Objem ({target_ticker}, {teplomer}):* `{aktualni_vol:,.2f} BTC` ({nasobek:.1f}x normál)"
            else:
                vol_info = f"\n\n📊 *Objem ({target_ticker}, {teplomer}):* `{aktualni_vol:,.0f}` ({nasobek:.1f}x normál)"
        elif not is_btc and session == "closed":
            vol_info = "\n\n🕒 _US burza je zavřená — objem se nesleduje._"

        zprava_makro = f"🚨 *MARKET MACRO ALERT*\n━━━━━━━━━━━━━━━━━━━━━━\n{makro_text}{vol_info}"
        
        await context.bot.send_message(chat_id=context.job.chat_id, text=zprava_makro, parse_mode="Markdown")
        
    except Exception as e:
        log.error("Chyba v makro smyčce: %s", e) 
        pass

async def cmd_walter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Použij: `/walter on` nebo `/walter off`", parse_mode="Markdown")
        return
        
    prikaz = context.args[0].lower()
    current_jobs = context.job_queue.get_jobs_by_name("walter_job")
    
    if prikaz == 'on':
        if current_jobs:
            await update.message.reply_text("ℹ️ *Walter Bloomberg je již aktivní.*", parse_mode="Markdown")
            return
            
        context.job_queue.run_repeating(
            walter_macro_loop,
            interval=WALTER_INTERVAL,
            first=1,
            chat_id=chat_id,
            name="walter_job"
        )
        await update.message.reply_text("🚨 *AUTOMATICKÉ SLEDOVÁNÍ ZAPNUTO*", parse_mode="Markdown")
        
    elif prikaz == 'off':
        if not current_jobs:
            await update.message.reply_text("ℹ️ *Walter Bloomberg je již vypnutý.*", parse_mode="Markdown")
            return
            
        for job in current_jobs:
            job.schedule_removal()
            
        await update.message.reply_text("🔕 *AUTOMATICKÉ SLEDOVÁNÍ VYPNUTO*", parse_mode="Markdown")

async def cmd_testwalter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Zahajuji test:* Odesílám GET požadavek na zrcadlo MarketFeed...", parse_mode="Markdown")
    
    try:
        url = "https://t.me/s/marketfeed"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        
        resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        
        if resp.status_code != 200:
            await msg.edit_text(f"❌ *Chyba:* Zrcadlo neodpovídá (Status: {resp.status_code})")
            return
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        zpravy = soup.find_all('div', class_='tgme_widget_message_text')
        
        if not zpravy:
            await msg.edit_text("❌ *Chyba:* Nepodařilo se najít žádné zprávy v HTML kódu.")
            return
            
        text_tweetu = zpravy[-1].get_text(separator=" ", strip=True)
        
        await msg.edit_text(f"✅ *Poslední zpráva stažena (MarketFeed):*\n`{text_tweetu}`\n\n🧠 *Posílám do Groq AI na analýzu...*", parse_mode="Markdown")
        
        prompt = f"""
        Jsi institucionální quant analytik. Zde je nejnovější blesková zpráva z trhu:
        "{text_tweetu}"
        
        Tato zpráva není o jedné firmě, ale o makroekonomickém dění.
        Vygeneruj PŘESNĚ tento výstup pro tradera:
        
        🌍 *Překlad:* [Český přesný překlad]
        📉 *Impact Nasdaq:* [např. -0.8% nebo +1.2%] ([stručný důvod])
        🤖 *AI Analýza:* [1 úderná věta makro-kontextu]
        """
        
        ai_out = await ask_groq(prompt, temperature=0.2)
        if ai_out is None:
            await update.message.reply_text("⚠️ AI není nakonfigurovaná (chybí GROQ_API_KEY).")
            return

        zprava = f"🛠️ *TEST MARKET ALERT*\n━━━━━━━━━━━━━━━━━━━━━━\n{ai_out}"
        await update.message.reply_text(zprava, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"❌ *Kritická chyba:*\n`{str(e)}`", parse_mode="Markdown")

async def news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/news AAPL`", parse_mode="Markdown")
        return
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Stahuji zprávy pro {ticker} přes nezávislý RSS kanál...")
    
    items = await asyncio.to_thread(fetch_yahoo_rss, ticker)
        
    if not items:
        await msg.edit_text(f"❌ Žádné zprávy pro '{ticker}' (Nebo je špatný ticker).")
        return

    lines = [f"📰 *POSLEDNÍ ZPRÁVY: {ticker}*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for item in items:
        lines.append(f"🔹 *[{item['title']}]({item['link']})*\n")
        
    keyboard = [[InlineKeyboardButton("🧠 AI Analýza Zpráv", callback_data=f"ainews_{ticker}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception:
        await msg.edit_text("\n".join(lines), disable_web_page_preview=True, reply_markup=reply_markup)

async def ai_news_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Groq čte zprávy...")
    
    data = query.data
    if not data.startswith("ainews_"):
        return
        
    ticker = data.split("_")[1]
    await query.edit_message_text(f"🧠 *Groq AI analyzuje sentiment zpráv pro {ticker}...*", parse_mode="Markdown")
    
    try:
        items = await asyncio.to_thread(fetch_yahoo_rss, ticker)
        
        if not items:
            await query.edit_message_text(f"❌ Nejsou data k analýze pro {ticker}.")
            return
            
        news_texts = []
        for item in items:
            news_texts.append(f"Titulek: {item['title']}\nShrnutí: {item['summary']}")
            
        combined_news = "\n---\n".join(news_texts)
        
        prompt = (
            f"Jsi agresivní quant trader. Zhodnoť tyto nejnovější zprávy pro {ticker}:\n\n{combined_news}\n\n"
            "PRAVIDLA:\n"
            "1. Hned první slovo musí být verdikt: 🟢 BULLISH, 🔴 BEARISH nebo 🟡 NEUTRAL.\n"
            "2. Napiš k tomu max 3 stručné věty vysvětlení. Vynech PR kecy a jdi po číslech nebo reálných dopadech.\n"
            "3. Nepoužívej vůbec žádné Markdown hvězdičky."
        )
        
        ai_out = await ask_groq(prompt, temperature=0.2)
        if ai_out is None:
            await query.edit_message_text("⚠️ AI není nakonfigurovaná (chybí GROQ_API_KEY).")
            return

        final_text = f"📰 *AI SENTIMENT: {ticker}*\n━━━━━━━━━━━━━━━━━━━━━━\n{ai_out}"
        await query.edit_message_text(final_text, parse_mode="Markdown")
        
    except Exception as e:
        await query.edit_message_text(f"❌ Chyba AI analýzy: {str(e)}")

async def nasdaq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"⏳ Spouštím masivní skener pro NASDAQ-100...\n"
        f"Stahuji data a analyzuji {len(NASDAQ_100)} akcií. Může to trvat 1-2 minuty.", 
        parse_mode="Markdown"
    )

    sem = asyncio.Semaphore(3)
    
    async def analyze_for_nasdaq(ticker):
        async with sem:
            await asyncio.sleep(1.5)
            try:
                result = await asyncio.to_thread(make_chart, ticker, "1d", False)
                if not result: return None
                _, _, data = result
                if not data: return None

                if "No Setup" in data["setup_type"]: return None
                if data["score"] <= 0: return None

                return {
                    "ticker": ticker,
                    "type": data["setup_type"].replace("🟢 ", "").replace("🚀 ", ""),
                    "score": data["score"],
                    "sm": data["sm"],
                    "entry": data["entry"],
                    "stop": f"${data['stop']:.2f}",
                    "t1": f"${data['t1']:.2f}",
                    "rr": f"1:{data['rr_zone']:.1f}",
                }
            except Exception:
                return None

    tasks = [analyze_for_nasdaq(ticker) for ticker in NASDAQ_100]
    raw_results = await asyncio.gather(*tasks)
    
    valid_setups = [r for r in raw_results if r is not None]
    valid_setups.sort(key=lambda x: x["score"], reverse=True)
    top_10 = valid_setups[:10]

    if not top_10:
        await msg.edit_text("❌ Nebyly nalezeny žádné validní setupy v NASDAQ-100.")
        return

    lines = ["📊 *TOP 10 NASDAQ SETUPŮ*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for s in top_10:
        lines.append(
            f"*{s['ticker']}* | 🏆 Score: `{s['score']}` | SM: `{s['sm']}/8`\n"
            f"🎯 Type: `{s['type']}` | ⚖️ R:R: `{s['rr']}`\n"
            f"📍 Vstup: `{s['entry']}`\n"
            f"🔴 Stop: `{s['stop']}` | 🟢 T1: `{s['t1']}`\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 _Generováno automaticky_")

    try: await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception: await msg.edit_text("\n".join(lines).replace("*", "").replace("`", ""))

async def darkhorse_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watchlist = load_russell_watchlist()
    
    msg = await update.message.reply_text(
        f"⏳ *Skenuji temné koně trhu...*\n"
        f"Analyzuji `{len(watchlist)}` akcií z Russell 2000.", 
        parse_mode="Markdown"
    )

    try: await asyncio.to_thread(yf.download, "SPY", period="1d", progress=False)
    except Exception: pass

    sem = asyncio.Semaphore(3) 

    async def scan_darkhorse(ticker):
        async with sem:
            await asyncio.sleep(1.5) 
            try:
                result = await asyncio.to_thread(make_chart, ticker, "1d", False)
                if not result: return None

                _, _, data = result
                if not data: return None
                if "No Setup" in data["setup_type"]: return None

                score = data["score"]
                if score < 70: return None

                rr_zone = data["rr_zone"]
                if rr_zone < 2.0: return None

                sm_score = data["sm"]
                mom_norm = 1.0 if data["mom_ok"] else 0.0
                vol_norm = 1.0 if data["vol_ok"] else 0.0

                score_norm = score / 100.0
                rr_norm = min(rr_zone, 10.0) / 10.0
                dh_score = (score_norm * 0.4 + rr_norm * 0.3 + mom_norm * 0.2 + vol_norm * 0.1) * 100

                return {"ticker": ticker, "score": score, "sm": sm_score, "rr": rr_zone, "dh_score": dh_score}
            except Exception: return None

    tasks = [scan_darkhorse(ticker) for ticker in watchlist]
    raw_results = await asyncio.gather(*tasks)
    
    valid_setups = [r for r in raw_results if r is not None]
    valid_setups.sort(key=lambda x: x["dh_score"], reverse=True)
    top_10 = valid_setups[:10]

    if not top_10:
        await msg.edit_text("❌ Nebyly nalezeny žádné validní Dark Horse setupy (Score > 70, RR > 2).")
        return

    lines = ["🐎 *DARK HORSE SCAN (Russell 2000)*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for i, s in enumerate(top_10, 1):
        lines.append(
            f"*{i}. {s['ticker']}*\n"
            f"🏆 Score: `{s['score']}` | SM: `{s['sm']}/8`\n"
            f"⚖️ RR: `{s['rr']:.1f}R` | 🐎 DarkHorse: `{s['dh_score']:.0f}`\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    try: await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception: await msg.edit_text("\n".join(lines).replace("*", "").replace("`", ""))

async def unusual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/unusual AAPL`", parse_mode="Markdown")
        return
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Skenuji opční trh pro *{ticker}*...", parse_mode="Markdown")

    try:
        tk = yf.Ticker(ticker)
        hist = await asyncio.wait_for(asyncio.to_thread(tk.history, period="1d"), timeout=30.0)
        if hist.empty: raise ValueError(f"Nepodařilo se načíst tržní cenu pro {ticker}")
            
        current_price = float(hist["Close"].iloc[-1])
        hits, market_cap = await asyncio.wait_for(asyncio.to_thread(analyze_options_flow, ticker, current_price), timeout=30.0)
        text = format_unusual(ticker, hits, current_price, market_cap)
    except asyncio.TimeoutError:
        text = f"❌ Skenování trvalo moc dlouho."
    except Exception as e:
        text = f"❌ Chyba: {e}"

    try: await msg.edit_text(text, parse_mode="Markdown")
    except Exception: await msg.edit_text(text)

async def whales_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watchlist = [
        "ASTS", "RKLB", "LUNR", "ONDS", "SOUN", "IONQ", "RGTI", "QBTS", 
        "ACHR", "JOBY", "LPTH", "UMAC", "AMPX", "KOPN", "SPAI", "LTRX", 
        "SRFM", "DPRO", "CEG", "NOK", "AAOI", "DDD", "BBAI", "RDW", 
        "SATL", "HOOD", "OKLO"
    ]
    
    msg = await update.message.reply_text(
        f"⏳ Spouštím ranní skener pro Whale Tracker ({len(watchlist)} tickerů)...", 
        parse_mode="Markdown"
    )

    sem = asyncio.Semaphore(3)
    
    async def safe_get_flow(ticker):
        async with sem:
            await asyncio.sleep(0.5)
            return await get_net_whale_flow(ticker)

    tasks = [safe_get_flow(ticker) for ticker in watchlist]
    raw_results = await asyncio.gather(*tasks)

    valid_results = [r for r in raw_results if r is not None and r["net_flow"] != 0]
    zero_count = len(watchlist) - len(valid_results)

    if not valid_results:
        await msg.edit_text(f"🐳 *RANNÍ SKENER*\n\nDnes zatím žádný výrazný pohyb.", parse_mode="Markdown")
        return

    by_money = sorted(valid_results, key=lambda x: abs(x["net_flow"]), reverse=True)[:5]
    by_strength = sorted([r for r in valid_results if r["market_cap"] > 0], key=lambda x: abs(x["flow_strength"]), reverse=True)[:5]
    by_score = sorted(valid_results, key=lambda x: abs(x["flow_score"]), reverse=True)[:5]

    lines = ["📊 *RANNÍ SKENER TRHU*", "━━━━━━━━━━━━━━━━━━━━━━", "🐳 *BIGGEST MONEY* _(Největší objem)_"]
    for r in by_money:
        sign = "+" if r["net_flow"] > 0 else ""
        lines.append(f"  • *{r['ticker'].ljust(5)}* {sign}{fmt_usd(r['net_flow'])}")
        
    lines.extend(["", "🚀 *RELATIVE FLOW* _(Největší dopad)_"])
    for r in by_strength:
        sign = "+" if r["flow_strength"] > 0 else ""
        lines.append(f"  • *{r['ticker'].ljust(5)}* {sign}{r['flow_strength']:.4f}%")

    lines.extend(["━━━━━━━━━━━━━━━━━━━━━━", "🎯 *TOP SETUPY* _(Nejvyšší přesvědčení)_"])
    if zero_count > 0:
        lines.append(f"_(Skryto {zero_count} tickerů bez výrazné aktivity)_")
    for r in by_score:
        fs = r["flow_score"]
        if fs >= 0.6: verdict = "🔥 VERY STRONG BULLISH"
        elif fs >= 0.2: verdict = "🟢 BULLISH"
        elif fs >= -0.2: verdict = "➡️ NEUTRAL"
        elif fs >= -0.6: verdict = "🟠 BEARISH"
        else: verdict = "🧊 STRONG BEARISH"
        
        sign_flow = "+" if r["net_flow"] > 0 else ""
        sign_fs = "+" if fs > 0 else ""
        
        lines.append(
            f"*{r['ticker']}*\n"
            f" 💵 Net Flow: `{sign_flow}{fmt_usd(r['net_flow'])}`\n"
            f" 🌡 FlowScore: `{sign_fs}{fs:.2f}`\n"
            f" 📌 Verdikt: {verdict}\n"
        )

    try: await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception: await msg.edit_text("\n".join(lines).replace("*", "").replace("_", "").replace("`", ""))

async def earnings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/earnings ASTS`", parse_mode="Markdown")
        return
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Stahuji finanční výkazy pro *{ticker}*...", parse_mode="Markdown")
    
    try:
        text = await asyncio.wait_for(asyncio.to_thread(analyze_earnings, ticker), timeout=20.0)
    except asyncio.TimeoutError:
        text = f"❌ Vypršel časový limit (20s)."
    except Exception as e:
        text = f"❌ Při analýze nastala neočekávaná chyba: {e}"
        
    try: await msg.edit_text(text, parse_mode="Markdown")
    except Exception: await msg.edit_text(text)

async def handle_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    ticker = parts[0].upper()
    interval = parts[1].lower() if len(parts) > 1 else "1d"

    if interval not in TF_PERIOD:
        await update.message.reply_text(f"❌ Neznámý timeframe '{interval}'.")
        return

    msg = await update.message.reply_text(f"⏳ Generuji graf a počítám S/R úrovně pro *{ticker}*...", parse_mode="Markdown")
    
    try:
        png, text, _ = await asyncio.wait_for(asyncio.to_thread(make_chart, ticker, interval), timeout=30.0)
    except asyncio.TimeoutError:
        await msg.edit_text(f"❌ Generování trvalo moc dlouho a bylo ukončeno.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Chyba: {e}")
        return

    if png is None:
        await msg.edit_text(text)
        return

    await msg.delete()

    short_caption = f"🎯 Technický setup pro *{ticker}* ({interval})"
    await reply_photo_with_text(update.message, png, text, short_caption)

async def setup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/setup ASTS`", parse_mode="Markdown")
        return
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Kalkuluji Confluence Setup pro *{ticker}*...", parse_mode="Markdown")
    
    try:
        text = await asyncio.to_thread(analyze_setup, ticker)
    except Exception as e:
        text = f"❌ Při analýze nastala chyba: {e}"
        
    try: await msg.edit_text(text, parse_mode="Markdown")
    except Exception: await msg.edit_text(text.replace("*", "").replace("`", "").replace("_", ""))

async def ai_pdf_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    document = None
    if message.document:
        document = message.document
    elif message.reply_to_message and message.reply_to_message.document:
        document = message.reply_to_message.document
        
    if not document:
        await message.reply_text("❌ Musíš mi poslat PDF soubor s popiskem `/ai`, nebo na nějaké PDF odpovědět příkazem `/ai`.")
        return

    if document.mime_type != "application/pdf":
        await message.reply_text("❌ Zatím umím číst jen PDF formát. Pošli mi klasickou prezentaci.")
        return

    msg = await message.reply_text("⏳ Stahuji PDF a předávám ho Groq analytikovi...")
    local_path = f"{document.file_id}.pdf"
    
    try:
        tg_file = await ctx.bot.get_file(document.file_id)
        await tg_file.download_to_drive(local_path)
        
        await msg.edit_text("🧠 Extrahuje text z PDF a posílá do modelu...")
        
        # --- NOVÉ: Extrakce textu pomocí PyPDF2 ---
        pdf_text = ""
        with open(local_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pdf_text += text + "\n"
        
        # Pojistka pro extrémně dlouhá PDF, aby nedošlo k přetečení kontextu u Llama-3
        if len(pdf_text) > 30000:
            pdf_text = pdf_text[:30000] + "... (Text zkrácen)"

        prompt = (
            "Jsi investigativní short-seller a finanční analytik na Wall Street, piš vždy česky. Udělej tvrdý výtah z tohoto PDF:\n\n"
            f"{pdf_text}\n\n"
            "PRAVIDLA:\n"
            "1. Žádná omáčka. Běž rovnou k číslům.\n"
            "2. Odpovídej striktně česky a formátuj přesně podle šablony níže.\n"
            "3. Hledej skryté 'Red Flags' (poznámky pod čarou, ředění akcií, pálení hotovosti).\n\n"
            "ODPOVĚZ PŘESNĚ TAKTO:\n\n"
            "🟢 PLUSY (Max 3 body):\n"
            "- [Tvrdé číslo/fakt]\n\n"
            "🔴 MÍNUSY & RED FLAGS (Max 3 body):\n"
            "- [Ztráty, dluhy, ředění]\n\n"
            "🔮 VÝHLED (Guidance):\n"
            "- [Zvýšili/snížili výhled na další kvartál/rok? Nebo ho úplně stáhli?]"
        )
        
        # Groq API volání
        text_odpovedi = await ask_groq(prompt, temperature=0.2)

        os.remove(local_path)

        if text_odpovedi is None:
            await msg.edit_text("⚠️ AI není nakonfigurovaná (chybí GROQ_API_KEY).")
            return

        limit = 4000
        
        if len(text_odpovedi) <= limit:
            await msg.edit_text(text_odpovedi)
        else:
            await msg.edit_text(text_odpovedi[:limit])
            for i in range(limit, len(text_odpovedi), limit):
                await message.reply_text(text_odpovedi[i:i+limit])     
    except Exception as e:
        await msg.edit_text(f"❌ Nastala chyba při AI analýze: {str(e)}")
        if os.path.exists(local_path):
            os.remove(local_path)

# ==============================================================================
# 5. START BOTA
# ==============================================================================
def main():
    if not TOKEN:
        log.error("CHYBA: Chybí TELEGRAM_TOKEN! Nastav ho v .env souboru.")
        return

    # Obnov aktivní snipery z minulého běhu
    global active_snipers
    active_snipers = load_snipers()
    if active_snipers:
        log.info("Obnoveno %d chatů s aktivními snipery.", len(active_snipers))

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("unusual", unusual))
    app.add_handler(CommandHandler("earnings", earnings_cmd))
    app.add_handler(CallbackQueryHandler(ai_news_callback, pattern="^ainews_"))
    app.add_handler(CommandHandler("setup", setup_cmd))
    app.add_handler(CommandHandler("nasdaq", nasdaq_cmd))
    app.add_handler(CommandHandler("walter", cmd_walter))
    app.add_handler(CommandHandler("testwalter", cmd_testwalter))
    app.add_handler(CommandHandler("smc", smc_cmd))
    app.add_handler(CommandHandler("sniper", sniper_cmd))
    app.add_handler(CommandHandler("darkhorse", darkhorse_cmd))
    app.add_handler(CommandHandler("whales", whales_cmd))
    app.add_handler(CommandHandler("ai", ai_pdf_cmd))
    app.add_handler(MessageHandler(filters.Document.PDF & filters.CaptionRegex(r'^/ai'), ai_pdf_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticker))

    # Naplánuj SMC Sniper skener na pozadí (každou minutu), pokud je JobQueue dostupná
    if app.job_queue:
        app.job_queue.run_repeating(sniper_background_task, interval=60, first=15, name="sniper_job")
    else:
        log.warning("JobQueue není dostupná – SMC Sniper poběží jen po /sniper. Nainstaluj python-telegram-bot[job-queue].")

    log.info("✅ Bot běží. Zastav pomocí Ctrl+C.")
    app.run_polling()
    
if __name__ == "__main__":
    main()
