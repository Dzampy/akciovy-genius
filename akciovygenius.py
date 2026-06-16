import io
import math
import asyncio
import time
import json
import hashlib
import threading
import atexit
import logging
import random
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
from telegram.error import Conflict, NetworkError
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
# yfinance loguje na ERROR zavádějící „possibly delisted; no price data found"
# i tehdy, když je ticker platný a jen nás Yahoo zablokoval/rate-limitnul. Ten
# per-ticker šum ztlumíme (CRITICAL) — SKUTEČNÝ signál o blokaci teď dává náš
# vlastní wrapper yf_download() (WARNING) po vyčerpání retry s impersonací.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
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

# Dva modely s ODDĚLENÝMI denními kvótami (TPD) na Groq:
#   - "heavy" 70b na finální analýzy (kvalitní text, běží zřídka)
#   - "fast"  8b  na vysokofrekvenční klasifikaci (Walter detekce na KAŽDÉ zprávě)
# Rozdělení šetří 100k/den limit 70b — 8b má vlastní mnohem větší kvótu.
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")

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

# ── SMC konfigurace ───────────────────────────────────────────────────────────
# Max stáří zóny (v svíčkách), kterou ještě kreslíme — starší = stale, zaneřádí graf.
SMC_ZONE_MAX_AGE = int(os.getenv("SMC_ZONE_MAX_AGE", "60"))

# ── Whale Radar konfigurace ───────────────────────────────────────────────────
# Proaktivní skener celého trhu na velké agresivní opční bloky (lov cizích signálů).
WHALE_RADAR_INTERVAL = int(os.getenv("WHALE_RADAR_INTERVAL", "180"))   # interval cyklu (s)
WHALE_MIN_PREMIUM = float(os.getenv("WHALE_MIN_PREMIUM", "1000000"))         # práh pro velké akcie ($)
WHALE_MIN_PREMIUM_SMALL = float(os.getenv("WHALE_MIN_PREMIUM_SMALL", "250000"))  # nižší práh pro smallcapy ($)
WHALE_MIN_AGGR = float(os.getenv("WHALE_MIN_AGGR", "0.6"))             # min. agrese (0=bid, 1=ask)
WHALE_CHUNK = int(os.getenv("WHALE_CHUNK", "12"))                      # tickerů na jeden cyklus
WHALE_MAX_ALERTS = int(os.getenv("WHALE_MAX_ALERTS", "5"))             # max alertů na cyklus

# ── Skenery: sdílená konkurence a throttling ──────────────────────────────────
# Jednotné parametry pro všechny hromadné skeny (whale radar, /nasdaq, /darkhorse, /whales).
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "3"))     # paralelních tasků naráz
SCAN_DELAY_CHART = float(os.getenv("SCAN_DELAY_CHART", "1.0")) # rozestup u chart skenů (/nasdaq, /darkhorse)
SCAN_DELAY_FLOW = float(os.getenv("SCAN_DELAY_FLOW", "0.5"))   # rozestup u options-flow skenů (/whales, radar)

# ── Options-flow prahy ────────────────────────────────────────────────────────
# Filtry pro detekci neobvyklé opční aktivity (analyze_options_flow).
OPT_MIN_VOL = int(os.getenv("OPT_MIN_VOL", "100"))            # min. denní objem kontraktů
OPT_MIN_OI = int(os.getenv("OPT_MIN_OI", "1"))               # min. open interest (děleno → != 0)
OPT_MIN_VOL_OI = float(os.getenv("OPT_MIN_VOL_OI", "3.0"))   # min. poměr volume/OI (unusual)
OPT_MIN_PREMIUM = float(os.getenv("OPT_MIN_PREMIUM", "50000"))  # min. prémie bloku ($)
OPT_MAX_EXPIRATIONS = int(os.getenv("OPT_MAX_EXPIRATIONS", "15"))  # kolik nejbližších expirací projít
OPT_DTE_MIN = int(os.getenv("OPT_DTE_MIN", "7"))             # min. dní do expirace
OPT_DTE_MAX = int(os.getenv("OPT_DTE_MAX", "90"))            # max. dní do expirace

# ── Flow paměť (akumulace/distribuce v čase) ──────────────────────────────────
# Engine si pamatuje denní stav každého neobvyklého strike → pozná, jestli někdo
# pozici teprve staví (akumulace) nebo opouští (distribuce), ne jen jednorázový blok.
FLOW_HISTORY_FILE = "flow_history.json"
FLOW_HISTORY_DAYS = int(os.getenv("FLOW_HISTORY_DAYS", "10"))             # okno paměti (dní)
FLOW_HISTORY_MAX_KEYS = int(os.getenv("FLOW_HISTORY_MAX_KEYS", "4000"))   # strop záznamů (strike-klíčů)
FLOW_ACCUM_MIN_DAYS = int(os.getenv("FLOW_ACCUM_MIN_DAYS", "2"))          # min. dní pro „akumulaci"
FLOW_ACCUM_OI_GROWTH = float(os.getenv("FLOW_ACCUM_OI_GROWTH", "1.3"))    # OI růst (×) = akumulace
FLOW_DISTRIB_OI_DROP = float(os.getenv("FLOW_DISTRIB_OI_DROP", "0.6"))    # OI pokles (×) = distribuce

_flow_history: dict = {}           # {ticker|type|strike|exp: {ticker,opt_type,strike,exp,history[],last_seen}}
_flow_lock = threading.Lock()      # chrání _flow_history (engine běží ve více vláknech přes to_thread)
_flow_dirty = False                # je co flushnout na disk?
_flow_loaded = False               # už jsme načetli z disku?

# ── Genius Score (fúze více pohledů do jediného přesvědčení) ───────────────────
# Sloučí technický setup, options flow a (volitelně) news sentiment do jednoho
# 0–100 skóre + směru + jistoty. Váhy se přepočítají jen přes dostupné pohledy.
GENIUS_W_TECH = float(os.getenv("GENIUS_W_TECH", "0.45"))     # váha technického pohledu
GENIUS_W_FLOW = float(os.getenv("GENIUS_W_FLOW", "0.40"))     # váha options flow
GENIUS_W_NEWS = float(os.getenv("GENIUS_W_NEWS", "0.15"))     # váha news sentimentu
GENIUS_DIR_THRESHOLD = float(os.getenv("GENIUS_DIR_THRESHOLD", "0.15"))  # |bias| pro směr
GENIUS_EARN_RISK_DAYS = int(os.getenv("GENIUS_EARN_RISK_DAYS", "7"))     # earnings „blízko" = riziko

_WHALE_RADAR_FILE = "whale_radar.json"
whale_radar_chats: set = set()     # chat_id odběratelů radaru (perzistované)
_whale_scan_idx = 0                # rotující ukazatel do univerza tickerů
_whale_seen = {}                   # {klíč bloku: ISO datum} — denní dedup

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
                   model: str = None):
    """Zavolá Groq chat completion a vrátí text odpovědi.
    Když klient není nakonfigurovaný (chybí GROQ_API_KEY), vrátí None.
    Při rate-limitu (429 / vyčerpané TPD) NEpadá — vrátí None a jen zaloguje,
    aby smyčka cyklus tiše přeskočila místo vyhození výjimky."""
    if client is None:
        log.warning("Pokus o volání Groq bez klíče — přeskakuji.")
        return None
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            messages=[{"role": "user", "content": prompt}],
            model=model or GROQ_MODEL,
            temperature=temperature,
        )
        return resp.choices[0].message.content
    except Exception as e:
        msg = str(e)
        if "rate_limit" in msg or "429" in msg:
            log.warning("[GROQ] Rate-limit (%s) — přeskakuji volání.", model or GROQ_MODEL)
        else:
            log.warning("[GROQ] Volání selhalo: %s", msg)
        return None

# ── yfinance: anti-blocking session + retry ───────────────────────────────────
# Yahoo nás při hromadných skenech (agent, /darkhorse z Russellu) rate-limituje
# a blokuje. Příznak: yfinance vrátí prázdno a zaloguje zavádějící
# „possibly delisted; no price data found" — ačkoli ticker JE platný; ve
# skutečnosti nás Yahoo jen odřízl (často 401 Invalid Crumb / 429). Bez obrany
# tím agent TIŠE přichází o data a zahazuje validní picky.
#
# Dvě obranné vrstvy:
#   1) curl_cffi session s impersonací prohlížeče — obejde TLS/JA3 fingerprint
#      blok, na který Yahoo defaultní requests session chytá.
#   2) retry s exponenciálním backoffem + jitterem na prázdnou/selhanou odpověď.
#
# Session je THREAD-LOCAL: curl_cffi Session není bezpečná pro sdílení napříč
# vlákny a skeny běží přes asyncio.to_thread, takže každé vlákno má vlastní.
try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL = True
except Exception:
    _HAS_CURL = False
    log.warning("curl_cffi není dostupné — yfinance pojede bez impersonace "
                "(vyšší riziko blokace od Yahoo).")

_yf_tls = threading.local()

def _yf_session():
    """Thread-local curl_cffi session s impersonací Chrome (líně vytvořená).
    Vrací None, když curl_cffi chybí → yfinance použije vlastní default session."""
    if not _HAS_CURL:
        return None
    s = getattr(_yf_tls, "session", None)
    if s is None:
        try:
            s = _curl_requests.Session(impersonate="chrome")
        except Exception as e:
            log.debug("[YF] nelze vytvořit curl_cffi session: %s", e)
            return None
        _yf_tls.session = s
    return s

def _looks_like_block(exc_msg: str) -> bool:
    """Heuristika: vypadá chyba na blokaci/rate-limit od Yahoo (vs. opravdu
    chybějící data)? Slouží k odlišení vážného signálu od běžného šumu."""
    m = exc_msg.lower()
    return any(t in m for t in
               ("crumb", "401", "429", "too many", "rate", "forbidden", "throttl"))

def yf_download(ticker, period=None, interval="1d", *, start=None,
                tries: int = 3, empty_tries: int = 2, **kwargs):
    """yf.download s anti-blocking session a retry-with-backoff.

    Dvě různé příčiny selhání → dvě různé strategie (jinak by hromadný sken
    Russellu trval věčnost na mrtvých tickerech):
      • VÝJIMKA (401 Invalid Crumb, 429, …) = reálná blokace → retry až `tries`×
        s exponenciálním backoffem; persistentní blok = WARNING (skutečný signál).
      • PRÁZDNÝ DataFrame bez výjimky = nejčastěji neplatný/mrtvý ticker → retry
        jen `empty_tries`× (default 1 = bez retry, vrať hned). Impersonace session
        řeší soft-blok u zdroje, takže nemá smysl pálit 3 pokusy na každé prázdno.
    Vrací prázdný DataFrame, když nic nepřišlo (volající ho už umí přeskočit)."""
    kwargs.setdefault("auto_adjust", True)
    kwargs.setdefault("progress", False)
    kwargs.setdefault("group_by", "ticker")
    last_exc = None
    empty_seen = 0
    for attempt in range(tries):
        try:
            params = dict(kwargs)
            params["interval"] = interval
            if period is not None:
                params["period"] = period
            if start is not None:
                params["start"] = start
            sess = _yf_session()
            if sess is not None:
                params["session"] = sess
            df = yf.download(ticker, **params)
            if df is not None and not df.empty:
                return df
            # prázdno bez výjimky → mrtvý ticker; nepálit plný retry
            empty_seen += 1
            if empty_seen >= empty_tries:
                log.debug("[YF] %s: prázdná odpověď (nejspíš neplatný ticker).", ticker)
                return pd.DataFrame()
            backoff = 0.4 + random.uniform(0, 0.3)           # krátký, plochý
        except Exception as e:
            last_exc = e
            _yf_tls.session = None                            # zahoď zaseknutou session
            backoff = 0.8 * (2 ** attempt) + random.uniform(0, 0.6)  # exponenciální
        if attempt < tries - 1:
            time.sleep(backoff)

    if last_exc is not None and _looks_like_block(str(last_exc)):
        log.warning("[YF] %s: Yahoo nás zřejmě blokuje (%s) — vyčerpáno %d pokusů.",
                    ticker, last_exc, tries)
    elif last_exc is not None:
        log.debug("[YF] %s: stažení selhalo (%s).", ticker, last_exc)
    return pd.DataFrame()

def yf_ticker(ticker):
    """yf.Ticker s thread-local impersonační session (anti-blocking)."""
    sess = _yf_session()
    return yf.Ticker(ticker, session=sess) if sess is not None else yf.Ticker(ticker)

# ── Jednoduchá TTL cache pro yfinance stahování ───────────────────────────────
_YF_CACHE: dict = {}
_YF_CACHE_MAX = 256   # strop záznamů, ať cache neroste donekonečna (memory-leak)

def cached_yf_download(ticker: str, period: str, interval: str, ttl: int = 300):
    """yf.download s in-memory TTL cache (výchozí 5 min).
    Snižuje počet requestů na Yahoo a riziko rate-limitu při hromadných skenech.

    Důležité: prázdné/selhané výsledky se NEcachují — jinak by jeden rate-limit
    od Yahoo „zamkl" ticker na celý TTL a další pokusy by marně vracely prázdno.
    """
    key = (ticker.upper(), period, interval)
    now = time.time()
    cached = _YF_CACHE.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1].copy()

    df = yf_download(ticker, period=period, interval=interval)

    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()  # neukládej prázdno

    # Eviction: vyhoď expirované; když je pořád plno, zahoď nejstarší záznam.
    if len(_YF_CACHE) >= _YF_CACHE_MAX:
        for k in [k for k, (t, _) in _YF_CACHE.items() if now - t >= ttl]:
            del _YF_CACHE[k]
        if len(_YF_CACHE) >= _YF_CACHE_MAX:
            del _YF_CACHE[min(_YF_CACHE, key=lambda k: _YF_CACHE[k][0])]

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

async def safe_send(bot, chat_id: str, text: str) -> None:
    """Pošle zprávu s Markdownem; když parser spadne (nepárová */_/`), zopakuje
    jako čistý text, ať se alert NIKDY neztratí kvůli formátování."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text.replace("*", "").replace("`", "").replace("_", ""),
            )
        except Exception as e:
            log.error("safe_send selhal i jako plain text (chat %s): %s", chat_id, e)

async def scan_universe(tickers, worker, *, concurrency=SCAN_CONCURRENCY, delay=1.0):
    """Sjednocený paralelní sken seznamu tickerů.

    Spustí `worker(ticker)` pro každý ticker s omezenou konkurencí (`concurrency`)
    a rozestupem `delay` sekund mezi starty (šetří yfinance rate-limit). `worker`
    smí být sync i async — sync se automaticky odsune do vlákna. Výjimka v jednom
    workeru se zaloguje a vrátí None (ostatní pokračují). Vrací seznam výsledků
    ve stejném pořadí jako `tickers`.
    """
    sem = asyncio.Semaphore(concurrency)
    total = len(tickers)
    done = 0
    # Heartbeat do logu: u dlouhých skenů (Russell ~2000) jinak 20–30 min ticho
    # a nepoznáš, jestli to jede nebo visí. Logni progres každých ~10 %.
    step = max(1, total // 10)

    async def run(tk):
        nonlocal done
        async with sem:
            await asyncio.sleep(delay)
            try:
                if asyncio.iscoroutinefunction(worker):
                    return await worker(tk)
                return await asyncio.to_thread(worker, tk)
            except Exception as e:
                log.debug("[SCAN] %s chyba: %s", tk, e)
                return None
            finally:
                done += 1
                if total >= 50 and (done % step == 0 or done == total):
                    log.info("[SCAN] průběh %d/%d (%d %%)", done, total, done * 100 // total)

    return await asyncio.gather(*[run(tk) for tk in tickers])

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

# Smallcap / momentum watchlist (sdílený mezi /whales a Whale Radarem).
# SPAI a SRFM odebrány – fakticky bez likvidních opcí (objem < 500 / 3 expirace).
WHALE_SMALLCAPS = [
    "ASTS", "RKLB", "LUNR", "ONDS", "SOUN", "IONQ", "RGTI", "QBTS",
    "ACHR", "JOBY", "LPTH", "UMAC", "AMPX", "KOPN", "LTRX",
    "DPRO", "CEG", "NOK", "AAOI", "DDD", "BBAI", "RDW",
    "SATL", "HOOD", "OKLO"
]
WHALE_SMALLCAP_SET = set(WHALE_SMALLCAPS)   # rychlá příslušnost → volba prahu

# Univerzum pro proaktivní Whale Radar (NASDAQ-100 + smallcapy, bez duplicit)
WHALE_UNIVERSE = list(dict.fromkeys(NASDAQ_100 + WHALE_SMALLCAPS))

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
# Jádro analýzy je rozdělené do dvou čistých funkcí, které volá živý make_chart
# i historický backtest (/edge). Díky tomu backtest přehrává PŘESNĚ stejnou
# klasifikaci setupů jako živý engine — čísla z backtestu pak nelžou.

def _compute_indicators(df) -> dict:
    """Spočítá všechny indikátory + pivoty + S/R úrovně z OHLCV rámce.
    MUTUJE df (přidá sloupce EMA/RSI/ATR/Vol_MA — potřebné pro vykreslení grafu)
    a vrátí dict odvozených hodnot. Vše je kauzální (jen minulost), takže na
    trailing okně dává stejné výsledky jako živé stažení končící týmž dnem."""
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

    tol = atr * 0.5
    res_clusters = cluster_levels(highs, tol)
    sup_clusters = cluster_levels(lows, tol)

    res_levels = sorted([c[0] for c in res_clusters if c[0] > last])
    valid_sup_clusters = sorted([c for c in sup_clusters if c[0] < last], reverse=True)
    nearest_res = res_levels[0] if res_levels else None

    return {
        "last": last, "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "rsi": rsi, "atr": atr, "mom_20d": mom_20d, "vol_ratio": vol_ratio,
        "is_bull_struct": is_bull_struct, "is_near_ath": is_near_ath,
        "highs": highs, "lows": lows, "high_52": high_52, "low_52": low_52,
        "fib_618": fib_618, "poc_price": poc_price, "tol": tol,
        "res_levels": res_levels, "valid_sup_clusters": valid_sup_clusters,
        "nearest_res": nearest_res,
    }

def classify_setup(ind: dict, flow_score_val: float = 0.0) -> dict:
    """Z indikátorů (+ volitelně options flow) určí typ setupu, vstupní zónu,
    stop, targety, skóre a status. Čistá funkce → identická živě i v backtestu."""
    last = ind["last"]; atr = ind["atr"]
    ema20 = ind["ema20"]; ema50 = ind["ema50"]; ema200 = ind["ema200"]
    rsi = ind["rsi"]; mom_20d = ind["mom_20d"]; vol_ratio = ind["vol_ratio"]
    is_bull_struct = ind["is_bull_struct"]; is_near_ath = ind["is_near_ath"]
    tol = ind["tol"]; res_levels = ind["res_levels"]
    valid_sup_clusters = ind["valid_sup_clusters"]; nearest_res = ind["nearest_res"]

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

    return {
        "setup_type": setup_type, "best_total_score": best_total_score,
        "sm_score": sm_score, "sm_grade": sm_grade, "sm_txt": sm_txt,
        "sm_mom": sm_mom, "sm_vol": sm_vol,
        "global_bd": global_bd, "best_zone": best_zone,
        "best_zone_bot": best_zone_bot, "best_zone_top": best_zone_top,
        "best_zone_bd": best_zone_bd, "best_zone_reasons": best_zone_reasons,
        "stop_loss": stop_loss, "target1": target1, "target2": target2,
        "rr_zone": rr_zone, "rr_now": rr_now, "grade": grade,
        "summary": summary, "status": status, "pullback_risk": pullback_risk,
        "dist_to_zone_pct": dist_to_zone_pct,
    }

def make_chart(ticker: str, interval: str = "1d", render: bool = True, flow: bool = True):
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

    ind = _compute_indicators(df)
    last = ind["last"]; atr = ind["atr"]
    ema20, ema50, ema200 = ind["ema20"], ind["ema50"], ind["ema200"]

    # Options flow je nejdražší část (15 expirací × 2 chainy na ticker). Ve sken-
    # módu (flow=False) ho přeskočíme → /nasdaq a /darkhorse jsou násobně rychlejší
    # a méně narážejí na rate-limit. Setup se pak skóruje technicky (bez flow bonusu).
    flow_score_val = 0.0
    if flow:
        try:
            hits, _ = analyze_options_flow(ticker, last)
            flow_score_val, _, _ = compute_flow_score(hits) if hits else (0.0, {}, "")
        except Exception:
            flow_score_val = 0.0

    res = classify_setup(ind, flow_score_val)
    setup_type = res["setup_type"]
    best_total_score = res["best_total_score"]
    sm_score, sm_grade, sm_txt = res["sm_score"], res["sm_grade"], res["sm_txt"]
    sm_mom, sm_vol = res["sm_mom"], res["sm_vol"]
    best_zone = res["best_zone"]
    best_zone_bot, best_zone_top = res["best_zone_bot"], res["best_zone_top"]
    best_zone_bd, best_zone_reasons = res["best_zone_bd"], res["best_zone_reasons"]
    stop_loss, target1, target2 = res["stop_loss"], res["target1"], res["target2"]
    rr_zone, rr_now, grade = res["rr_zone"], res["rr_now"], res["grade"]
    summary, status = res["summary"], res["status"]
    pullback_risk = res["pullback_risk"]
    dist_to_zone_pct = res["dist_to_zone_pct"]

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

# ==============================================================================
# 3. FUNDAMENTÁLNÍ SCORECARD + INVESTIČNÍ PROFIL
# ==============================================================================
# Čisté (bezsíťové) scoring funkce → testovatelné a identické živě i v testech.
# Každá kategorie vrací 0–100 sub-skóre; písmenná známka se odvozuje z _letter_grade.
# Ke každé kategorii se přidává „note" = vysvětlení lidskou řečí (pro začátečníky).

def _fmt_big(val) -> str:
    """Formátuje velká USD čísla (T/B/M/K)."""
    sign = "-" if val < 0 else ""
    v = abs(val)
    if v >= 1e12: return f"{sign}${v/1e12:.2f}T"
    if v >= 1e9:  return f"{sign}${v/1e9:.2f}B"
    if v >= 1e6:  return f"{sign}${v/1e6:.2f}M"
    if v >= 1e3:  return f"{sign}${v/1e3:.1f}K"
    return f"{sign}${v:.0f}"

def _letter_grade(s) -> str:
    if s is None: return "—"
    if s >= 88: return "A+"
    if s >= 78: return "A"
    if s >= 65: return "B"
    if s >= 50: return "C"
    if s >= 35: return "D"
    return "F"

def _grade_icon(grade: str) -> str:
    return {"A+": "🟢", "A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "F": "🔴"}.get(grade, "⚪")

def _band_high(x, table, default):
    """table: (práh, skóre) odshora; vrátí skóre prvního x >= práh (vyšší x = lepší)."""
    for thr, sc in table:
        if x >= thr: return sc
    return default

def _band_low(x, table, default):
    """table: (práh, skóre) odspoda; vrátí skóre prvního x <= práh (nižší x = lepší)."""
    for thr, sc in table:
        if x <= thr: return sc
    return default

def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None

# Vysvětlení každé kategorie lidskou řečí podle dosaženého skóre (5 pásem).
_CAT_NOTES = {
    "growth":   ["Tržby i zisk klesají — varovný signál.",
                 "Růst zpomaluje, byznys spíš stagnuje.",
                 "Růst je vlažný, nic extra.",
                 "Slušný růst, firma se rozšiřuje.",
                 "Roste svižně — tržby i zisk míří nahoru."],
    "profit":   ["Firma vydělává málo, nebo je ztrátová.",
                 "Tenké marže, moc jí nezbývá.",
                 "Marže jsou průměrné.",
                 "Zdravě zisková firma.",
                 "Mimořádně zisková — z každého dolaru tržeb si nechá hodně."],
    "balance":  ["Hodně dluhů a slabá likvidita — rizikové.",
                 "Vyšší zadlužení, sleduj to.",
                 "Rozvaha je v normě.",
                 "Finančně zdravá, dluh pod kontrolou.",
                 "Pevná rozvaha — málo dluhů, dost hotovosti."],
    "value":    ["Hodně drahá — v ceně sedí velká očekávání.",
                 "Spíš drahá, platíš prémii.",
                 "Oceněná férově — ani levná, ani drahá.",
                 "Cena je rozumná vůči číslům.",
                 "Levná vůči svým číslům — atraktivní vstup."],
    "cashflow": ["Pálí hotovost — reálně peníze nevydělává.",
                 "Slabý cash flow.",
                 "Cash flow je v pohodě.",
                 "Zdravý volný cash flow.",
                 "Silný volný cash flow — reálné peníze do pokladny."],
}

def _cat_note(kind: str, score) -> str:
    if score is None:
        return "Chybí data k vyhodnocení."
    notes = _CAT_NOTES[kind]
    if score >= 78: i = 4
    elif score >= 65: i = 3
    elif score >= 50: i = 2
    elif score >= 35: i = 1
    else: i = 0
    return notes[i]


def score_growth(rev_g, eps_g, q_g=None) -> dict:
    """Růst: meziroční tržby + EPS + poslední kvartál (vše v %)."""
    comps, parts = [], []
    if rev_g is not None:
        comps.append(_band_high(rev_g, [(30, 100), (20, 88), (12, 75), (6, 60), (0, 45), (-8, 28)], 12))
        parts.append(f"Tržby {rev_g:+.0f}%")
    if eps_g is not None:
        comps.append(_band_high(eps_g, [(30, 100), (18, 85), (8, 70), (0, 52), (-10, 30)], 12))
        parts.append(f"Zisk {eps_g:+.0f}%")
    if q_g is not None:
        comps.append(_band_high(q_g, [(25, 100), (12, 80), (3, 62), (0, 48), (-10, 28)], 12))
        parts.append(f"Q {q_g:+.0f}%")
    sc = _avg(comps)
    return {"score": sc, "grade": _letter_grade(sc), "parts": parts, "note": _cat_note("growth", sc)}


def score_profitability(net_m, oper_m, gross_m, roe, roa=None) -> dict:
    """Ziskovost: marže (v %) + ROE/ROA (desetinné, 0.15 = 15 %)."""
    comps, parts = [], []
    if net_m is not None:
        comps.append(_band_high(net_m, [(25, 100), (15, 85), (8, 70), (3, 55), (0, 42), (-5, 22)], 8))
        parts.append(f"Čistá marže {net_m:.0f}%")
    if oper_m is not None:
        comps.append(_band_high(oper_m, [(30, 100), (20, 85), (12, 70), (5, 55), (0, 40)], 20))
        parts.append(f"Provozní {oper_m:.0f}%")
    if roe is not None:
        comps.append(_band_high(roe * 100, [(25, 100), (15, 82), (8, 65), (0, 45)], 20))
        parts.append(f"ROE {roe*100:.0f}%")
    if roa is not None:
        comps.append(_band_high(roa * 100, [(15, 100), (8, 82), (4, 65), (0, 45)], 20))
        parts.append(f"ROA {roa*100:.0f}%")
    if gross_m is not None:
        parts.append(f"Hrubá {gross_m:.0f}%")
    sc = _avg(comps)
    return {"score": sc, "grade": _letter_grade(sc), "parts": parts, "note": _cat_note("profit", sc)}


def score_balance(d_e, curr, cash, debt, quick=None) -> dict:
    """Rozvaha: dluh/equity (poměr), current/quick ratio, čistá hotovost vs dluh."""
    comps, parts = [], []
    if d_e is not None:
        comps.append(_band_low(d_e, [(0.3, 100), (0.5, 88), (1.0, 72), (1.5, 55), (2.5, 35)], 18))
        parts.append(f"Dluh/Equity {d_e:.2f}")
    if curr is not None:
        comps.append(_band_high(curr, [(2.0, 100), (1.5, 82), (1.2, 68), (1.0, 52)], 30))
        parts.append(f"Current {curr:.2f}")
    if quick is not None:
        comps.append(_band_high(quick, [(1.5, 100), (1.0, 82), (0.7, 64), (0.4, 48)], 30))
        parts.append(f"Quick {quick:.2f}")
    if cash is not None and debt is not None:
        ratio = cash / debt if debt > 0 else 5.0
        comps.append(_band_high(ratio, [(1.0, 95), (0.5, 72), (0.25, 55), (0.1, 40)], 25))
        net = cash - debt
        parts.append(f"Net {'hotovost' if net >= 0 else 'dluh'} {_fmt_big(abs(net))}")
    sc = _avg(comps)
    return {"score": sc, "grade": _letter_grade(sc), "parts": parts, "note": _cat_note("balance", sc)}


def score_valuation(pe, ps, peg, pb=None, ev_ebitda=None) -> dict:
    """Valuace: P/E, P/S, PEG, P/B, EV/EBITDA — nižší = levnější = VYŠŠÍ skóre."""
    comps, parts = [], []
    if pe is not None and pe > 0:
        comps.append(_band_low(pe, [(12, 100), (18, 82), (25, 66), (35, 50), (50, 33)], 18))
        parts.append(f"P/E {pe:.0f}")
    if ps is not None and ps > 0:
        comps.append(_band_low(ps, [(2, 100), (4, 82), (7, 64), (12, 46), (20, 30)], 18))
        parts.append(f"P/S {ps:.1f}")
    if peg is not None and peg > 0:
        comps.append(_band_low(peg, [(1.0, 100), (1.5, 80), (2.0, 62), (3.0, 42)], 25))
        parts.append(f"PEG {peg:.2f}")
    if pb is not None and pb > 0:
        comps.append(_band_low(pb, [(1.5, 100), (3, 82), (6, 62), (10, 45), (20, 30)], 18))
        parts.append(f"P/B {pb:.1f}")
    if ev_ebitda is not None and ev_ebitda > 0:
        comps.append(_band_low(ev_ebitda, [(8, 100), (12, 82), (18, 64), (28, 46), (40, 30)], 18))
        parts.append(f"EV/EBITDA {ev_ebitda:.0f}")
    sc = _avg(comps)
    return {"score": sc, "grade": _letter_grade(sc), "parts": parts, "note": _cat_note("value", sc)}


def score_cashflow(fcf, fcf_margin, ocf=None) -> dict:
    """Cash flow: volný cash flow (USD), jeho marže (% z tržeb), provozní CF."""
    comps, parts = [], []
    if fcf is not None:
        comps.append(80 if fcf > 0 else 20)
        parts.append(f"FCF {_fmt_big(fcf)}")
    if fcf_margin is not None:
        comps.append(_band_high(fcf_margin, [(20, 100), (12, 85), (6, 68), (0, 48), (-5, 25)], 10))
        parts.append(f"FCF marže {fcf_margin:.0f}%")
    if ocf is not None:
        parts.append(f"Provozní CF {_fmt_big(ocf)}")
    sc = _avg(comps)
    return {"score": sc, "grade": _letter_grade(sc), "parts": parts, "note": _cat_note("cashflow", sc)}


def invest_profile(growth, profit, balance, value, cashflow, trend, upside) -> dict:
    """Investiční fúze: kvalita firmy × valuace × trend × analytici → 'chci to vlastnit?'.
    growth/profit/balance/value/cashflow = 0–100 sub-skóre (value: vyšší = levnější).
    trend = 0–100 (cena vs 200d), upside = % k cíli analytiků (může být None)."""
    quality = _avg([growth, profit, balance, cashflow])
    analyst = None
    if upside is not None:
        analyst = _band_high(upside, [(25, 92), (10, 74), (0, 56), (-10, 36)], 20)

    # Celkové investiční skóre (kvalita váží nejvíc, pak valuace).
    weighted = [(quality, 0.50), (value, 0.25), (trend, 0.15), (analyst, 0.10)]
    num = sum(s * wt for s, wt in weighted if s is not None)
    den = sum(wt for s, wt in weighted if s is not None)
    overall = (num / den) if den > 0 else None

    # 2×2 matice: kvalita firmy × valuace.
    if quality is None or value is None:
        verdict, desc = "⚪ Nedostatek dat", "Chybí klíčové fundamenty pro závěr."
    elif quality >= 68 and value >= 55:
        verdict, desc = "💎 Skvělá firma za rozumnou cenu", "Kvalita i cena hrají pro tebe — ideál dlouhodobého investora."
    elif quality >= 68 and value < 55:
        verdict, desc = "🌟 Kvalitní firma, ale draho", "Skvělý byznys, jenže trh si ho cení. Vyplatí se počkat na slevu."
    elif quality < 50 and value >= 60:
        verdict, desc = "⚠️ Laciná, ale rozbitá", "Levná z důvodu — slabé fundamenty. Pozor na value trap."
    elif quality < 50 and value < 45:
        verdict, desc = "🔴 Drahá spekulace", "Slabá kvalita a k tomu vysoká cena. Nejhorší kombinace."
    else:
        verdict, desc = "🟡 Průměr napříč úrovněmi", "Ani jasná koupě, ani odpad — bez výrazné výhody."

    return {"overall": overall, "quality": quality, "value": value,
            "trend": trend, "analyst": analyst, "verdict": verdict, "desc": desc}


# ── Pomocné popisky pro „snímek firmy" (beginner-friendly) ───────────────────

def _cap_label(mcap) -> str:
    if mcap is None: return ""
    if mcap >= 200e9: return "Mega-cap"
    if mcap >= 10e9:  return "Large-cap"
    if mcap >= 2e9:   return "Mid-cap"
    if mcap >= 300e6: return "Small-cap"
    return "Micro-cap"

def _beta_label(beta) -> str:
    if beta is None: return ""
    if beta < 0.8:  return f"Nízká kolísavost (klidná, beta {beta:.2f})"
    if beta < 1.2:  return f"Střední kolísavost (beta {beta:.2f})"
    if beta < 1.8:  return f"Vyšší kolísavost (beta {beta:.2f})"
    return f"Vysoká kolísavost (divoká, beta {beta:.2f})"

def _range_bar(price, low, high) -> str:
    """Mini ukazatel pozice ceny v ročním rozpětí (12 polí)."""
    if None in (price, low, high) or high <= low:
        return ""
    pos = max(0.0, min(1.0, (price - low) / (high - low)))
    idx = round(pos * 11)
    bar = "─" * idx + "●" + "─" * (11 - idx)
    from_high = (high - price) / high * 100
    return f"${low:.0f} {bar} ${high:.0f}  ({from_high:+.0f}% od max)"

def _rec_label(key) -> str:
    return {
        "strong_buy": "💚 Silně koupit", "buy": "🟢 Koupit",
        "hold": "🟡 Držet", "underperform": "🟠 Spíš prodat",
        "sell": "🔴 Prodat",
    }.get((key or "").lower(), "")


def _gather_fundamentals(ticker: str) -> dict:
    """Stáhne fundamenty z yfinance, převede jednotky a spočítá všechny kategorie."""
    tk = yf_ticker(ticker)
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    def num(key):
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) else None

    def pct(key):
        v = num(key)
        return v * 100 if v is not None else None

    rev_g = pct("revenueGrowth")
    eps_g = pct("earningsGrowth")
    q_g = pct("earningsQuarterlyGrowth")
    net_m = pct("profitMargins")
    oper_m = pct("operatingMargins")
    gross_m = pct("grossMargins")
    roe = num("returnOnEquity")
    roa = num("returnOnAssets")
    d_e_raw = num("debtToEquity")
    d_e = d_e_raw / 100 if d_e_raw is not None else None   # yfinance dává v %
    curr = num("currentRatio")
    quick = num("quickRatio")
    cash = num("totalCash")
    debt = num("totalDebt")
    pe = num("trailingPE")
    ps = num("priceToSalesTrailing12Months")
    peg = num("trailingPegRatio") or num("pegRatio")
    pb = num("priceToBook")
    ev_ebitda = num("enterpriseToEbitda")
    fcf = num("freeCashflow")
    ocf = num("operatingCashflow")
    rev = num("totalRevenue")
    fcf_margin = (fcf / rev * 100) if (fcf is not None and rev) else None
    price = num("currentPrice")
    dma200 = num("twoHundredDayAverage")
    target = num("targetMeanPrice")
    upside = ((target - price) / price * 100) if (target and price) else None

    trend = None
    if price and dma200:
        diff = (price - dma200) / dma200 * 100
        trend = _band_high(diff, [(15, 90), (5, 72), (0, 58), (-8, 40), (-20, 22)], 12)

    div_yield = num("dividendYield")        # už v % (0.37 = 0.37 %)
    payout = num("payoutRatio")

    return {
        "ticker": ticker.upper(),
        "name": info.get("shortName") or info.get("longName") or ticker.upper(),
        "sector": info.get("sector"),
        "market_cap": num("marketCap"), "beta": num("beta"),
        "price": price, "target": target, "upside": upside, "trend": trend,
        "wk_low": num("fiftyTwoWeekLow"), "wk_high": num("fiftyTwoWeekHigh"),
        "rec": info.get("recommendationKey"), "n_analysts": num("numberOfAnalystOpinions"),
        "div_yield": div_yield, "payout": payout, "forward_pe": num("forwardPE"),
        "growth": score_growth(rev_g, eps_g, q_g),
        "profit": score_profitability(net_m, oper_m, gross_m, roe, roa),
        "balance": score_balance(d_e, curr, cash, debt, quick),
        "value": score_valuation(pe, ps, peg, pb, ev_ebitda),
        "cashflow": score_cashflow(fcf, fcf_margin, ocf),
    }


def _build_profile(data: dict) -> dict:
    return invest_profile(
        data["growth"]["score"], data["profit"]["score"], data["balance"]["score"],
        data["value"]["score"], data["cashflow"]["score"], data["trend"], data["upside"])


def format_fundamentals(data: dict) -> str:
    prof = _build_profile(data)
    t = data["ticker"]
    sector = data.get("sector") or "—"
    cap = _cap_label(data.get("market_cap"))
    head_sub = " · ".join(x for x in [data["name"], sector, cap] if x)

    def cat_line(emoji, label, cat):
        g = cat["grade"]
        sc = cat["score"]
        sc_str = f"{sc:.0f}" if sc is not None else "—"
        detail = "  ·  ".join(cat["parts"]) if cat["parts"] else "data N/A"
        return (f"{emoji} *{label}*  {_grade_icon(g)} `{g}`  _{sc_str}/100_\n"
                f"     {detail}\n"
                f"     → _{cat['note']}_")

    overall = prof["overall"]
    overall_str = f"{overall:.0f}/100" if overall is not None else "—"
    quality_str = f"{prof['quality']:.0f}" if prof["quality"] is not None else "—"
    value_str = f"{prof['value']:.0f}" if prof["value"] is not None else "—"

    lines = [
        f"🧭 *INVESTIČNÍ PROFIL: {t}*",
        f"_{head_sub}_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "*📸 Snímek firmy:*",
    ]
    # Cena + roční rozpětí
    if data.get("price") is not None:
        bar = _range_bar(data["price"], data.get("wk_low"), data.get("wk_high"))
        lines.append(f"  💵 Cena `${data['price']:.0f}`" + (f"\n     {bar}" if bar else ""))
    if data.get("beta") is not None:
        lines.append(f"  📈 {_beta_label(data['beta'])}")
    rec = _rec_label(data.get("rec"))
    if rec:
        n = data.get("n_analysts")
        n_str = f" ({n:.0f} analytiků)" if n else ""
        up = f", cíl {data['upside']:+.0f}%" if data.get("upside") is not None else ""
        lines.append(f"  🏆 Analytici: {rec}{n_str}{up}")
    dy = data.get("div_yield")
    if dy is not None and dy > 0:
        po = data.get("payout")
        po_str = f" (vyplácí {po*100:.0f}% zisku)" if po and 0 < po < 2 else ""
        lines.append(f"  💸 Dividenda: {dy:.2f}% ročně{po_str}")
    else:
        lines.append("  💸 Dividenda: nevyplácí")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"*🎯 VERDIKT:* {prof['verdict']}",
        f"_{prof['desc']}_",
        "",
        f"📊 *Investiční skóre:* `{overall_str}`",
        f"   kvalita firmy `{quality_str}/100`  ·  atraktivita ceny `{value_str}/100`",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "*🧪 Fundamentální scorecard:*",
        cat_line("🌱", "Růst", data["growth"]),
        cat_line("💰", "Ziskovost", data["profit"]),
        cat_line("🏦", "Rozvaha", data["balance"]),
        cat_line("🏷️", "Valuace", data["value"]),
        cat_line("💵", "Cash Flow", data["cashflow"]),
        "━━━━━━━━━━━━━━━━━━━━━━",
        "ℹ️ _Jak číst: známky *A* (výborné) → *F* (slabé). „Kvalita firmy“ = jak dobrý "
        "je byznys, „atraktivita ceny“ = jak levně ho kupuješ (vyšší = levnější). "
        "Skvělá firma + nízká cena = sen investora._",
        "_Fundamenty z Yahoo Finance. Ne investiční doporučení._",
    ]
    return "\n".join(lines)


def analyze_earnings(ticker: str) -> str:
    """Kompletní fundamentální profil firmy: snímek + scorecard (5 kategorií se
    známkami) korunovaný investičním verdiktem (kvalita × valuace)."""
    data = _gather_fundamentals(ticker)
    if data["price"] is None and data["growth"]["score"] is None and data["value"]["score"] is None:
        return f"❌ Nepodařilo se načíst fundamenty pro *{ticker.upper()}* (špatný ticker, nebo Yahoo nevrací data)."
    return format_fundamentals(data)


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

# ── Flow paměť: perzistence denních snapshotů + detekce akumulace/distribuce ──
def _et_today() -> str:
    """Dnešní datum v US/Eastern (ISO). Trh i opce žijí v ET, ne v UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()

def load_flow_history() -> None:
    """Líně načte flow paměť z disku (jednou). Po načtení rovnou prořeže."""
    global _flow_history, _flow_loaded
    with _flow_lock:
        if _flow_loaded:
            return
        try:
            if os.path.exists(FLOW_HISTORY_FILE):
                with open(FLOW_HISTORY_FILE, "r", encoding="utf-8") as f:
                    _flow_history = json.load(f)
        except Exception as e:
            log.error("Chyba při načítání flow paměti: %s", e)
            _flow_history = {}
        _flow_loaded = True
    _prune_flow_history()

def save_flow_history() -> None:
    """Atomicky uloží paměť na disk — jen když je co (dirty). Bezpečné z více vláken."""
    global _flow_dirty
    with _flow_lock:
        if not _flow_dirty:
            return
        snapshot = json.dumps(_flow_history)
        _flow_dirty = False
    try:
        tmp = FLOW_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(tmp, FLOW_HISTORY_FILE)   # atomická výměna → nikdy půlka souboru
    except Exception as e:
        log.error("Nepodařilo se uložit flow paměť: %s", e)

def _prune_flow_history() -> None:
    """Vyhodí expirované strikes, staré denní záznamy a přebytek nad strop."""
    global _flow_history
    today = _et_today()
    with _flow_lock:
        dead = []
        for key, rec in _flow_history.items():
            exp = rec.get("exp", "")
            if exp and exp < today:                  # opce už vypršela → pryč celá
                dead.append(key)
                continue
            hist = rec.get("history", [])
            if len(hist) > FLOW_HISTORY_DAYS:        # ořež okno paměti
                rec["history"] = hist[-FLOW_HISTORY_DAYS:]
        for key in dead:
            del _flow_history[key]
        if len(_flow_history) > FLOW_HISTORY_MAX_KEYS:   # strop: drž nejčerstvěji viděné
            ordered = sorted(_flow_history.items(),
                             key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
            _flow_history = dict(ordered[:FLOW_HISTORY_MAX_KEYS])

def record_flow_snapshot(ticker: str, hits: list[dict]) -> None:
    """Zapíše dnešní stav každého bloku do paměti (1 záznam/strike/den, jen do RAM).
    Na disk se flushne jinde (save_flow_history) — tady jen levný update pod zámkem."""
    if not hits:
        return
    load_flow_history()
    today = _et_today()
    global _flow_dirty
    with _flow_lock:
        for h in hits:
            key = f"{ticker}|{h['opt_type']}|{h['strike']}|{h['exp']}"
            snap = {
                "date": today,
                "oi": int(h.get("oi", 0)),
                "volume": int(h.get("volume", 0)),
                "premium": float(h.get("premium", 0.0)),
                "wscore": float(h.get("wscore", 0.0)),
                "bscore": int(h.get("bscore_sum", 0)),
            }
            rec = _flow_history.get(key)
            if rec is None:
                _flow_history[key] = {
                    "ticker": ticker, "opt_type": h["opt_type"],
                    "strike": float(h["strike"]), "exp": h["exp"],
                    "history": [snap], "last_seen": today,
                }
            else:
                hist = rec["history"]
                if hist and hist[-1]["date"] == today:
                    hist[-1] = snap                  # přepiš dnešní (poslední = nejaktuálnější)
                else:
                    hist.append(snap)
                    del hist[:-FLOW_HISTORY_DAYS]
                rec["last_seen"] = today
            _flow_dirty = True

def _accum_from_history(hist: list[dict]) -> dict | None:
    """Z denní historie strike spočítá trend: label + růst OI/prémie + počet dní."""
    if not hist:
        return None
    days = len({s["date"] for s in hist})
    first, last = hist[0], hist[-1]
    oi_growth = last["oi"] / first["oi"] if first.get("oi", 0) > 0 else 1.0
    prem_growth = last["premium"] / first["premium"] if first.get("premium", 0) > 0 else 1.0
    cum_premium = sum(s.get("premium", 0.0) for s in hist)   # kolik se do strike celkem „nalilo"

    if days < FLOW_ACCUM_MIN_DAYS:
        label = "🆕 Nový"
    elif oi_growth >= FLOW_ACCUM_OI_GROWTH and prem_growth >= 1.0:
        label = "🟢 Akumulace"
    elif oi_growth <= FLOW_DISTRIB_OI_DROP:
        label = "🔴 Distribuce"
    else:
        label = "➡️ Stabilní"

    return {
        "label": label, "days": days,
        "oi_growth": oi_growth, "prem_growth": prem_growth,
        "cum_premium": cum_premium,
        "is_accum": label == "🟢 Akumulace",
    }

def flow_accumulation(ticker: str, opt_type: str, strike: float, exp: str) -> dict | None:
    """Trend pro konkrétní strike z paměti. None = žádná historie."""
    key = f"{ticker}|{opt_type}|{strike}|{exp}"
    with _flow_lock:
        rec = _flow_history.get(key)
        hist = list(rec["history"]) if rec else []
    return _accum_from_history(hist)

def analyze_options_flow(ticker: str, spot: float) -> tuple[list[dict], float]:
    if spot <= 0: return [], 0.0
    tk = yf_ticker(ticker)
    
    try:
        info = tk.info
        market_cap = float(info.get("marketCap") or info.get("totalAssets") or 0.0)
    except Exception: market_cap = 0.0
        
    try: expirations = tk.options
    except Exception: return [], market_cap
    if not expirations: return [], market_cap

    today = datetime.now(timezone.utc).date()
    agg: dict[tuple, dict] = {}

    for exp_str in expirations[:OPT_MAX_EXPIRATIONS]:
        try: exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError: continue

        dte = (exp_date - today).days
        if not (OPT_DTE_MIN <= dte <= OPT_DTE_MAX): continue

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
                
                if iv_raw > 5 or vol < OPT_MIN_VOL or oi < OPT_MIN_OI or last <= 0 or (vol / oi < OPT_MIN_VOL_OI): continue
                premium = vol * last * 100
                if premium < OPT_MIN_PREMIUM: continue

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
    top = results[:12]

    # Engine má paměť: ulož dnešní stav a obohať bloky o trend (akumulace/distribuce).
    record_flow_snapshot(ticker, top)
    for h in top:
        h["accum"] = flow_accumulation(ticker, h["opt_type"], h["strike"], h["exp"])
    return top, market_cap

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
    ])

    accum_hits = [h for h in hits if (h.get("accum") or {}).get("is_accum")]
    if accum_hits:
        lines.append(f"🧲 *Akumulace:* `{len(accum_hits)}` strike(ů) se nabaluje víc dní po sobě")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

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

        accum = h.get("accum")
        accum_line = ""
        if accum and accum["days"] >= FLOW_ACCUM_MIN_DAYS:
            accum_line = (
                f"  {accum['label']} • `{accum['days']}` dní | "
                f"OI ×{accum['oi_growth']:.1f} | Σprémie {fmt_usd(accum['cum_premium'])}\n"
            )

        lines.append(
            f"*{i}.* {ot_emoji} *${h['strike']:.0f}* | Exp: {h['exp']} ({h['dte']}d){sweep_str}\n"
            f"  💰 Prémium: `{fmt_usd(h['premium'])}` | IV: {h['iv']}% {em_str}\n"
            f"  📊 Vol: {h['volume']:,} | OI: {h['oi']:,} | Ratio: `{h['ratio']}×`\n"
            f"  🏷 {h['moneyness']} | 🎯 {pct_diff:+.1f}% od ceny\n"
            f"{accum_line}"
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
        tk = yf_ticker(ticker)
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

# ── Whale Radar: perzistence odběrů, dedup, skener, formát ────────────────────
def load_whale_chats() -> set:
    """Načte odběratele Whale Radaru z disku (přežije restart)."""
    try:
        with open(_WHALE_RADAR_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.error("Chyba při načítání whale radaru: %s", e)
        return set()

def save_whale_chats() -> None:
    try:
        with open(_WHALE_RADAR_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(whale_radar_chats), f)
    except Exception as e:
        log.error("Nepodařilo se uložit whale radar: %s", e)

def _whale_dedup_ok(key: str) -> bool:
    """True, pokud tento blok dnes ještě nebyl odeslán. Paměť se resetuje denně."""
    today = datetime.now(timezone.utc).date().isoformat()
    # vyčisti záznamy z předchozích dnů
    for k in [k for k, d in _whale_seen.items() if d != today]:
        del _whale_seen[k]
    if _whale_seen.get(key) == today:
        return False
    _whale_seen[key] = today
    return True

def scan_ticker_whales(ticker: str) -> list[dict]:
    """Najde velké agresivní směrové opční bloky pro jeden ticker (sync, do to_thread)."""
    try:
        tk = yf_ticker(ticker)
        hist = tk.history(period="1d")
        if hist.empty:
            return []
        spot = float(hist["Close"].iloc[-1])
    except Exception:
        return []

    hits, _ = analyze_options_flow(ticker, spot)
    # Smallcapy mají menší bloky → nižší práh, ať je radar vůbec zachytí.
    min_prem = WHALE_MIN_PREMIUM_SMALL if ticker in WHALE_SMALLCAP_SET else WHALE_MIN_PREMIUM
    out = []
    for h in hits:
        if h["premium"] < min_prem:
            continue
        aggr = aggression_score(h["last"], h["bid"], h["ask"])
        if aggr < WHALE_MIN_AGGR:
            continue
        bull = h["bscore_sum"] >= 1
        bear = h["bscore_sum"] <= -1
        if not (bull or bear):
            continue  # jen jasné směrové sázky, ne neutrál
        out.append({
            "ticker": ticker, "opt_type": h["opt_type"], "strike": h["strike"],
            "exp": h["exp"], "dte": h["dte"], "premium": h["premium"], "iv": h["iv"],
            "volume": h["volume"], "oi": h["oi"], "ratio": h["ratio"],
            "aggr": aggr, "bullish": bull, "spot": spot,
            "accum": h.get("accum"),
            "key": f"{ticker}|{h['opt_type']}|{h['strike']}|{h['exp']}",
        })
    return out

def format_whale_alert(a: dict) -> str:
    side = "CALLS" if a["opt_type"] == "call" else "PUTS"
    direction = "📈 Bullish sázka" if a["bullish"] else "📉 Bearish sázka"
    aggr_lbl = "na asku (agresivní)" if a["aggr"] >= 0.85 else "blízko asku"

    accum = a.get("accum")
    if accum and accum.get("is_accum"):
        head = "🐋🧲 *WHALE — AKUMULACE*"
        accum_line = (
            f"\n🧲 *{accum['days']}. den nabalování!* "
            f"OI ×{accum['oi_growth']:.1f} | Σ prémie {fmt_usd(accum['cum_premium'])}"
        )
    else:
        head = "🐳 *MEGA WHALE*" if a["premium"] >= 5_000_000 else "🐋 *WHALE ALERT*"
        accum_line = ""

    return (
        f"{head}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{a['ticker']}* — `{fmt_usd(a['premium'])}` {side} {aggr_lbl}\n"
        f"🎯 Strike `${a['strike']:.2f}` | exp {a['exp']} (`{a['dte']}` DTE)\n"
        f"📊 Vol `{a['volume']:,}` / OI `{a['oi']:,}` (`{a['ratio']}×`) | IV {a['iv']}%\n"
        f"💵 Spot `${a['spot']:.2f}` | {direction}"
        f"{accum_line}"
    )

async def whale_radar_loop(context: ContextTypes.DEFAULT_TYPE):
    """Proaktivní skener celého trhu na velké opční bloky. Rotuje po dávkách."""
    global _whale_scan_idx
    if not whale_radar_chats:
        return
    if us_market_session() == "closed":
        return  # mimo US tržní hodiny se opce neobchodují

    universe = WHALE_UNIVERSE
    if not universe:
        return
    start = _whale_scan_idx % len(universe)
    chunk = universe[start:start + WHALE_CHUNK]
    if len(chunk) < WHALE_CHUNK:                       # wrap-around na konci seznamu
        chunk += universe[:WHALE_CHUNK - len(chunk)]
    _whale_scan_idx = (start + WHALE_CHUNK) % len(universe)

    results = await scan_universe(chunk, scan_ticker_whales, delay=SCAN_DELAY_FLOW)
    await asyncio.to_thread(save_flow_history)     # sken naplnil paměť → flushni na disk

    alerts = [a for sub in results if sub for a in sub]
    fresh = [a for a in alerts if _whale_dedup_ok(a["key"])]
    if not fresh:
        return

    # Vícedenní akumulace má přednost před jednorázovým blokem (silnější signál).
    fresh.sort(key=lambda a: ((a.get("accum") or {}).get("is_accum", False), a["premium"]), reverse=True)
    for a in fresh[:WHALE_MAX_ALERTS]:
        msg = format_whale_alert(a)
        for chat_id in list(whale_radar_chats):
            await safe_send(context.bot, chat_id, msg)

async def flow_history_flush_job(context: ContextTypes.DEFAULT_TYPE):
    """Pravidelně prořeže a uloží flow paměť (i když radar zrovna neběží).
    Oba kroky jsou no-op, když není co dělat → levné."""
    await asyncio.to_thread(_prune_flow_history)
    await asyncio.to_thread(save_flow_history)

# ==============================================================================
# 3b. GENIUS SCORE — fúzní engine (technika + flow + news → 1 přesvědčení)
# ==============================================================================
# Každý dílčí engine vidí jen svůj kousek trhu. Genius Score je sloučí do jediného
# 0–100 přesvědčení + směru + jistoty. Klíčová myšlenka: shoda více nezávislých
# pohledů je víc než součet jejich částí, rozpor je naopak varování.

def genius_fuse(lenses: dict) -> dict:
    """Čistá (testovatelná) fúze pohledů → finální verdikt.

    lenses = {
        "ticker": str, "last": float|None,
        "tech":  {"setup_type","score","entry","stop","t1","t2","rr_zone","last"} | None,
        "flow":  {"score" ∈[-1,1], "confidence", "accum_count", "premium"} | None,
        "news":  {"label": "bullish"|"bearish"|"neutral"} | None,
        "earn_days": int | None,
    }
    """
    factors = []   # každý: {name, bias ∈ [-1,1], weight, desc}

    tech = lenses.get("tech")
    if tech:
        st = tech.get("setup_type", "")
        score01 = max(0.0, min(1.0, tech.get("score", 0) / 100.0))
        if "No Setup" in st:
            t_bias = 0.0
        elif "Reversal" in st or "🔄" in st:
            t_bias = -score01            # reversal = proti dosavadnímu trendu
        else:
            t_bias = score01             # ostatní setupy enginu jsou long-biased
        factors.append({"name": "Technika", "bias": t_bias, "weight": GENIUS_W_TECH,
                        "desc": f"{st} ({tech.get('score', 0):.0f}/100)"})

    flow = lenses.get("flow")
    if flow:
        f_bias = max(-1.0, min(1.0, flow.get("score", 0.0)))
        accum_n = int(flow.get("accum_count", 0) or 0)
        if accum_n > 0:
            f_bias = max(-1.0, min(1.0, f_bias * 1.2))   # vícedenní akumulace zesiluje signál
        desc = f"flow {f_bias:+.2f}"
        if accum_n > 0:
            desc += f", {accum_n}× akumulace"
        factors.append({"name": "Options flow", "bias": f_bias, "weight": GENIUS_W_FLOW,
                        "desc": desc})

    news = lenses.get("news")
    if news and news.get("label") in ("bullish", "bearish", "neutral"):
        n_map = {"bullish": 0.6, "bearish": -0.6, "neutral": 0.0}
        n_bias = n_map[news["label"]]
        factors.append({"name": "News sentiment", "bias": n_bias, "weight": GENIUS_W_NEWS,
                        "desc": {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish",
                                 "neutral": "🟡 Neutral"}[news["label"]]})

    # Vážený průměr jen přes DOSTUPNÉ pohledy (váhy se přenormují).
    w_total = sum(f["weight"] for f in factors)
    net_bias = sum(f["bias"] * f["weight"] for f in factors) / w_total if w_total > 0 else 0.0

    if net_bias >= GENIUS_DIR_THRESHOLD:
        direction = "📈 BULLISH"
    elif net_bias <= -GENIUS_DIR_THRESHOLD:
        direction = "📉 BEARISH"
    else:
        direction = "➡️ NEUTRÁLNÍ"

    # Přesvědčení = síla biasu, modulovaná shodou/rozporem technika×flow a earnings.
    t_bias = next((f["bias"] for f in factors if f["name"] == "Technika"), None)
    f_bias = next((f["bias"] for f in factors if f["name"] == "Options flow"), None)
    agree = conflict = False
    if t_bias is not None and f_bias is not None and abs(t_bias) > 0.05 and abs(f_bias) > 0.05:
        if (t_bias > 0) == (f_bias > 0): agree = True
        else: conflict = True

    conviction = abs(net_bias) * 100.0
    if agree:    conviction *= 1.15
    if conflict: conviction *= 0.65

    earn_days = lenses.get("earn_days")
    earn_soon = earn_days is not None and 0 <= earn_days <= GENIUS_EARN_RISK_DAYS
    if earn_soon:
        conviction *= 0.9        # blízké earnings = binární riziko → sleva na jistotě
    conviction = max(0.0, min(100.0, conviction))

    n_avail = len(factors)
    if conviction >= 70 and agree and n_avail >= 2:
        confidence = "🟢 Vysoká"
    elif conviction >= 45:
        confidence = "🟡 Střední"
    else:
        confidence = "🔴 Nízká"

    # Pro & riziko v lidské řeči.
    pro, risk = [], []
    for f in factors:
        if f["bias"] >= 0.2:   pro.append(f"{f['name']}: {f['desc']}")
        elif f["bias"] <= -0.2: risk.append(f"{f['name']}: {f['desc']}")
    if conflict:
        risk.append("Technika a flow si protiřečí")
    if earn_soon:
        risk.append(f"Earnings za {earn_days} d (binární riziko)")

    return {
        "ticker": lenses.get("ticker", "?"),
        "last": lenses.get("last"),
        "score": round(conviction),
        "direction": direction,
        "confidence": confidence,
        "net_bias": net_bias,
        "factors": factors,
        "pro": pro,
        "risk": risk,
        "agree": agree,
        "conflict": conflict,
        "earn_days": earn_days,
        "n_lenses": n_avail,
        "tech": tech,
    }

def _genius_thesis(r: dict) -> str:
    """Jednovětá teze v lidské řeči podle směru + jistoty."""
    d = r["direction"]
    if "BULLISH" in d:
        core = "Pohledy se kloní na long stranu"
    elif "BEARISH" in d:
        core = "Pohledy se kloní na short/opatrnou stranu"
    else:
        core = "Pohledy jsou rozdělené, jasný směr chybí"
    if r["agree"]:
        core += " a technika se shoduje s flow"
    elif r["conflict"]:
        core += ", ale technika a flow si protiřečí"
    return core + "."

def format_genius(r: dict) -> str:
    bar_n = int(round(r["score"] / 10))
    bar = "█" * bar_n + "░" * (10 - bar_n)
    last = f" • spot ${r['last']:.2f}" if r.get("last") else ""

    lines = [
        f"🧠 *GENIUS SCORE: {r['ticker']}*{last}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"*{r['score']}/100*  `{bar}`",
        f"{r['direction']}  •  Jistota: {r['confidence']}",
        "",
        f"_{_genius_thesis(r)}_",
        "",
        "*Pohledy:*",
    ]
    if r["factors"]:
        for f in r["factors"]:
            arrow = "🟢" if f["bias"] >= 0.2 else ("🔴" if f["bias"] <= -0.2 else "⚪")
            lines.append(f" {arrow} *{f['name']}:* {f['desc']}")
    else:
        lines.append(" _(žádná data)_")

    if r["pro"]:
        lines += ["", "✅ *Pro:*"] + [f"  • {p}" for p in r["pro"]]
    if r["risk"]:
        lines += ["", "⚠️ *Riziko:*"] + [f"  • {x}" for x in r["risk"]]

    # Obchodní úrovně ukazuj jen pro bullish setup (engine je long-biased).
    tech = r.get("tech")
    if tech and "BULLISH" in r["direction"] and "No Setup" not in tech.get("setup_type", ""):
        lines += [
            "",
            "🎯 *Úrovně (z techniky):*",
            f"  Vstup: `{tech.get('entry', 'N/A')}`",
            f"  Stop: `${tech.get('stop', 0):.2f}`  •  T1: `${tech.get('t1', 0):.2f}`  •  T2: `${tech.get('t2', 0):.2f}`",
        ]

    lines += ["━━━━━━━━━━━━━━━━━━━━━━", "_Fúze veřejných dat, ne investiční doporučení._"]
    return "\n".join(lines)

async def get_news_sentiment(ticker: str) -> dict | None:
    """News lens přes Groq. None = nedostupné (chybí klíč / data / chyba)."""
    if client is None:
        return None
    try:
        items = await asyncio.to_thread(fetch_yahoo_rss, ticker)
        if not items:
            return None
        combined = "\n---\n".join(f"Titulek: {i['title']}\nShrnutí: {i['summary']}" for i in items)
        prompt = (
            f"Jsi quant trader. Zhodnoť sentiment těchto zpráv pro {ticker}:\n\n{combined}\n\n"
            "Odpověz JEDNÍM slovem: BULLISH, BEARISH nebo NEUTRAL. Nic víc."
        )
        out = await ask_groq(prompt, temperature=0.0)
        if not out:
            return None
        u = out.strip().upper()
        if "BULL" in u:   return {"label": "bullish"}
        if "BEAR" in u:   return {"label": "bearish"}
        return {"label": "neutral"}
    except Exception as e:
        log.debug("[GENIUS] news lens %s chyba: %s", ticker, e)
        return None

def _next_earnings_days(ticker: str):
    """Počet dní do nejbližších earnings (None = neznámé)."""
    try:
        cal = yf_ticker(ticker).calendar
        ed = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)):
                ed = ed[0] if ed else None
        elif cal is not None and hasattr(cal, "loc"):
            try: ed = cal.loc["Earnings Date"][0]
            except Exception: ed = None
        if ed is None:
            return None
        if hasattr(ed, "date"):
            ed = ed.date()
        return (ed - datetime.now().date()).days
    except Exception:
        return None

async def _next_earnings_days_async(ticker: str):
    return await asyncio.to_thread(_next_earnings_days, ticker)

async def gather_genius(ticker: str) -> dict:
    """Posbírá všechny pohledy paralelně a vrátí finální verdikt z genius_fuse."""
    ticker = ticker.upper()

    async def get_tech():
        # flow=False → čistý technický pohled (flow fúzujeme zvlášť, ať se nezapočítá 2×).
        try:
            _, _, data = await asyncio.to_thread(make_chart, ticker, "1d", False, False)
            return data
        except Exception as e:
            log.debug("[GENIUS] tech lens %s chyba: %s", ticker, e)
            return None

    async def get_flow():
        try:
            tk = yf_ticker(ticker)
            hist = await asyncio.to_thread(tk.history, period="1d")
            if hist.empty:
                return None
            spot = float(hist["Close"].iloc[-1])
            hits, _ = await asyncio.to_thread(analyze_options_flow, ticker, spot)
            if not hits:
                return {"score": 0.0, "confidence": "🔴 Nízká", "accum_count": 0,
                        "premium": 0.0, "spot": spot}
            score, _, confidence = compute_flow_score(hits)
            accum_count = sum(1 for h in hits if (h.get("accum") or {}).get("is_accum"))
            premium = sum(h["premium"] for h in hits)
            return {"score": score, "confidence": confidence, "accum_count": accum_count,
                    "premium": premium, "spot": spot}
        except Exception as e:
            log.debug("[GENIUS] flow lens %s chyba: %s", ticker, e)
            return None

    tech, flow, news, earn_days = await asyncio.gather(
        get_tech(), get_flow(), get_news_sentiment(ticker), _next_earnings_days_async(ticker)
    )

    last = None
    if tech and tech.get("last"):
        last = tech["last"]
    elif flow and flow.get("spot"):
        last = flow["spot"]

    return genius_fuse({
        "ticker": ticker, "last": last,
        "tech": tech, "flow": flow, "news": news, "earn_days": earn_days,
    })

# ==============================================================================
# 3c. EDGE LAB — historická validace setupů (backtest)
# ==============================================================================
# Přehraje PŘESNĚ stejnou klasifikaci setupů (classify_setup) na trailing oknech
# napříč roky historie a u každého typu setupu spočítá reálnou úspěšnost. Tím se
# z „názoru algoritmu" stává empirická pravděpodobnost: 61 % WR, +0.7R expectancy.

EDGE_LOOKBACK = int(os.getenv("EDGE_LOOKBACK", "252"))   # trailing okno = 1 rok (jako živě)
EDGE_MAX_HOLD = int(os.getenv("EDGE_MAX_HOLD", "20"))    # max. dní na vyřešení obchodu
EDGE_DEFAULT_YEARS = int(os.getenv("EDGE_DEFAULT_YEARS", "4"))
EDGE_MIN_SAMPLE = int(os.getenv("EDGE_MIN_SAMPLE", "10"))  # min. obchodů pro důvěryhodný edge
EDGE_PULLBACK_WINDOW = int(os.getenv("EDGE_PULLBACK_WINDOW", "10"))  # dní čekání na vstup do zóny
EDGE_MIN_RR = float(os.getenv("EDGE_MIN_RR", "1.5"))    # min. R:R zóny (stejný práh jako „VYHNOUT" živě)
BULLISH_SETUPS = ("🟢 Pullback Buy", "🚀 ATH Breakout", "🚀 Momentum Breakout")

def simulate_trade(entry: float, stop: float, target: float,
                   highs, lows, closes, max_hold: int) -> dict | None:
    """Čistá simulace jednoho obchodu vpřed. None = neplatný vstup.
    Konzervativně: když svíčka protne stop i target, počítá se zásah STOPu."""
    if entry <= 0 or stop >= entry or target <= entry:
        return None
    risk = (entry - stop) / entry
    n = min(max_hold, len(highs))
    for j in range(n):
        if lows[j] <= stop:
            ret = stop / entry - 1
            return {"outcome": "stop", "ret": ret, "bars": j + 1,
                    "risk": risk, "r": ret / risk if risk > 0 else 0.0}
        if highs[j] >= target:
            ret = target / entry - 1
            return {"outcome": "target", "ret": ret, "bars": j + 1,
                    "risk": risk, "r": ret / risk if risk > 0 else 0.0}
    if n == 0:
        return {"outcome": "open", "ret": 0.0, "bars": 0, "risk": risk, "r": 0.0}
    ret = closes[n - 1] / entry - 1
    return {"outcome": "timeout", "ret": ret, "bars": n,
            "risk": risk, "r": ret / risk if risk > 0 else 0.0}

def _aggregate_edge(trades: list[dict]) -> dict | None:
    """Z listu obchodů spočítá win-rate, Ø zisk/ztrátu, expectancy v R, profit factor."""
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["ret"] > 0]
    losses = [t for t in trades if t["ret"] <= 0]
    gross_win = sum(t["ret"] for t in wins)
    gross_loss = abs(sum(t["ret"] for t in losses))
    return {
        "n": n,
        "wr": len(wins) / n,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (sum(t["ret"] for t in losses) / len(losses)) if losses else 0.0,
        "exp_r": sum(t["r"] for t in trades) / n,
        "exp_ret": sum(t["ret"] for t in trades) / n,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "target_hits": sum(1 for t in trades if t["outcome"] == "target"),
        "stop_hits": sum(1 for t in trades if t["outcome"] == "stop"),
        "timeouts": sum(1 for t in trades if t["outcome"] == "timeout"),
        "avg_bars": sum(t["bars"] for t in trades) / n,
    }

def backtest_setups(ticker: str, years: int = EDGE_DEFAULT_YEARS,
                    max_hold: int = EDGE_MAX_HOLD, min_score: int = 0) -> dict | None:
    """Projde historii, přehraje classify_setup na každém trailing okně a u každého
    obchodovatelného setupu (býčí typ + R:R zóny ≥ EDGE_MIN_RR — stejný práh, pod
    kterým engine živě hlásí „VYHNOUT") odsimuluje obchod s enginovým stopem/T1.
    Vstup = breakout na close, nebo limit při pullbacku do zóny. Obchody se
    nepřekrývají (cooldown do vyřešení). Vrací report po typech setupu.

    Flow se historicky přehrát nedá (chybí archiv opcí), proto backtest běží
    flow=0 a testuje čistě technickou detekci setupu. Skóre tím není gate —
    rozhoduje typ setupu a geometrie R:R, které jsou na flow nezávislé."""
    df = yf_download(ticker, period=f"{years}y", interval="1d")
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker.upper() in df.columns.levels[0]:
            df = df[ticker.upper()]
        else:
            df.columns = df.columns.get_level_values(-1)
    df = df.dropna()
    n = len(df)
    if n < EDGE_LOOKBACK + 30:
        return {"ticker": ticker.upper(), "insufficient": True, "bars": n,
                "need": EDGE_LOOKBACK + 30}

    O = df["Open"].values
    H = df["High"].values
    L = df["Low"].values
    C = df["Close"].values

    trades: list[dict] = []
    i = EDGE_LOOKBACK
    while i < n - 1:
        window = df.iloc[i - EDGE_LOOKBACK + 1:i + 1].copy()
        try:
            ind = _compute_indicators(window)
            res = classify_setup(ind, 0.0)
        except Exception:
            i += 1
            continue

        st = res["setup_type"]
        # Backtestujeme detekci setupu (ne flow-závislý status label — bez historie
        # opcí by skóre bylo uměle nízké a engine by skoro vše označil „VYHNOUT").
        # Filtr: býčí setup + R:R zóny ≥ práh (geometrie, flow-nezávislá) + skóre.
        if (st not in BULLISH_SETUPS or res["rr_zone"] < EDGE_MIN_RR
                or res["best_total_score"] < min_score):
            i += 1
            continue

        zone_top = res["best_zone_top"]
        stop, target = res["stop_loss"], res["target1"]
        entry_idx = entry_price = None

        if st.startswith("🚀") or res["dist_to_zone_pct"] <= 0:
            # Breakout, nebo cena už v/pod horní hranou zóny → vstup na close.
            entry_idx, entry_price = i, float(C[i])
        else:
            # Pullback s cenou nad zónou = limit v zóně: čekáme až cena klesne do zóny.
            for k in range(i + 1, min(i + 1 + EDGE_PULLBACK_WINDOW, n)):
                if L[k] <= zone_top:
                    entry_idx = k
                    entry_price = min(float(O[k]), zone_top)   # gap-down → fill na open
                    break

        if entry_idx is None:
            i += 1
            continue

        sim = simulate_trade(entry_price, stop, target,
                             H[entry_idx + 1:entry_idx + 1 + max_hold],
                             L[entry_idx + 1:entry_idx + 1 + max_hold],
                             C[entry_idx + 1:entry_idx + 1 + max_hold], max_hold)
        if sim:
            trades.append({"setup": st, "score": res["best_total_score"],
                           "date": df.index[entry_idx], **sim})
            i = entry_idx + max(1, sim["bars"]) + 1   # cooldown do vyřešení obchodu
            continue
        i += 1

    by_setup = {}
    for st in BULLISH_SETUPS:
        agg = _aggregate_edge([t for t in trades if t["setup"] == st])
        if agg:
            by_setup[st] = agg

    return {
        "ticker": ticker.upper(), "years": years, "bars": n,
        "max_hold": max_hold, "n_trades": len(trades),
        "by_setup": by_setup, "overall": _aggregate_edge(trades),
    }

def _edge_verdict(agg: dict) -> str:
    """Slovní verdikt nad expectancy + velikostí vzorku."""
    if agg["n"] < EDGE_MIN_SAMPLE:
        return "⚪ Málo dat"
    if agg["exp_r"] >= 0.15 and agg["wr"] >= 0.45:
        return "✅ Edge potvrzen"
    if agg["exp_r"] > 0:
        return "🟡 Slabý edge"
    return "🔴 Bez edge"

def _fmt_edge_block(title: str, agg: dict) -> list[str]:
    pf = "∞" if agg["pf"] == float("inf") else f"{agg['pf']:.2f}"
    return [
        f"*{title}*  ({agg['n']}×)  {_edge_verdict(agg)}",
        f"  WR `{agg['wr']*100:.0f}%`  •  Exp `{agg['exp_r']:+.2f}R`  •  PF `{pf}`",
        f"  Ø zisk `{agg['avg_win']*100:+.1f}%`  •  Ø ztráta `{agg['avg_loss']*100:+.1f}%`  •  Ø {agg['avg_bars']:.0f} dní",
    ]

def format_edge(report: dict) -> str:
    if report is None:
        return "❌ Nepodařilo se načíst historická data."
    if report.get("insufficient"):
        return (f"🔬 *EDGE: {report['ticker']}*\n"
                f"❌ Málo historie ({report['bars']} barů, potřeba ≥ {report['need']}). "
                "Zkus delší období nebo zavedenější ticker.")

    lines = [
        f"🔬 *EDGE LAB: {report['ticker']}*  _({report['years']}r, {report['bars']} barů)_",
        f"_Vstup = breakout/pullback do zóny, exit = enginový stop/T1, max {report['max_hold']} dní._",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if report["n_trades"] == 0:
        lines.append("Za sledované období nepadl jediný obchodovatelný setup. Žádná data k vyhodnocení.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("_Historická statistika, ne investiční doporučení._")
        return "\n".join(lines)

    if report["overall"]:
        lines += _fmt_edge_block("CELKEM", report["overall"])

    # Rozpad po typech jen když přispěl víc než jeden typ — jinak by duplikoval CELKEM.
    if len(report["by_setup"]) > 1:
        lines.append("")
        lines.append("*Podle typu setupu:*")
        for st, agg in sorted(report["by_setup"].items(), key=lambda kv: -kv[1]["exp_r"]):
            lines += _fmt_edge_block(st, agg)

    lines += ["━━━━━━━━━━━━━━━━━━━━━━",
              "_WR = win-rate, Exp = expectancy v R, PF = profit factor._",
              "_Historická statistika, ne investiční doporučení._"]
    return "\n".join(lines)

# ==============================================================================
# 4. TELEGRAM HANDLERY
# ==============================================================================

# ==============================================================================
# 2.0 UI VRSTVA — interaktivní menu + kontextová navigace
# ==============================================================================
# Producenti vrací HOTOVÝ text → sdílí je slash-příkazy i callbacky (žádné dupl
# logiky). Klávesnice dávají pod každý výstup tlačítka pro přepnutí analýzy na
# stejném tickeru a /start funguje jako proklikávací rozcestník.

# Pořadí tlačítek kontextové navigace pod výstupem k tickeru.
_NAV_BUTTONS = [
    ("profil", "🧭 Profil"),
    ("genius", "🧠 Genius"),
    ("flow",   "🌊 Flow"),
    ("news",   "📰 News"),
    ("chart",  "📈 Graf"),
    ("edge",   "🔬 Edge"),
]

def nav_keyboard(ticker: str, exclude: str = "") -> InlineKeyboardMarkup:
    """Kontextová tlačítka pod výstupem → 1 klik = stejná analýza jinou optikou."""
    t = ticker.upper()
    btns = [InlineKeyboardButton(lbl, callback_data=f"nav:{act}:{t}")
            for act, lbl in _NAV_BUTTONS if act != exclude]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return InlineKeyboardMarkup(rows)

def news_keyboard(ticker: str) -> InlineKeyboardMarkup:
    """AI-analýza zpráv + standardní navigace."""
    t = ticker.upper()
    rows = [[InlineKeyboardButton("🧠 AI Analýza zpráv", callback_data=f"ainews_{t}")]]
    rows += nav_keyboard(t, exclude="news").inline_keyboard
    return InlineKeyboardMarkup(rows)

def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 Investor", callback_data="menu:investor"),
         InlineKeyboardButton("🌊 Flow & Whales", callback_data="menu:flow")],
        [InlineKeyboardButton("📈 Grafy & setupy", callback_data="menu:chart"),
         InlineKeyboardButton("🌍 Makro", callback_data="menu:macro")],
        [InlineKeyboardButton("🔍 Skenery", callback_data="menu:scan"),
         InlineKeyboardButton("❓ Vše / Help", callback_data="menu:help")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Zpět do menu", callback_data="menu:home")]])

def scan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 NASDAQ TOP 10", callback_data="menu:run:nasdaq")],
        [InlineKeyboardButton("🐳 Whale sken trhu", callback_data="menu:run:whales")],
        [InlineKeyboardButton("🐎 Dark Horse (Russell)", callback_data="menu:run:darkhorse")],
        [InlineKeyboardButton("⬅️ Zpět do menu", callback_data="menu:home")],
    ])


# ── Doručení textu (řeší 4096 limit + fallback bez Markdownu) ─────────────────
async def reply_long_with_kb(message, text: str, keyboard, parse_mode: str = "Markdown") -> None:
    """Pošle text po částech; klávesnici připne k poslední části."""
    chunks = [text[i:i + TG_MSG_LIMIT] for i in range(0, len(text), TG_MSG_LIMIT)] or [text]
    for idx, chunk in enumerate(chunks):
        kb = keyboard if idx == len(chunks) - 1 else None
        try:
            await message.reply_text(chunk, parse_mode=parse_mode,
                                     reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await message.reply_text(chunk.replace("*", "").replace("`", "").replace("_", ""),
                                     reply_markup=kb)

async def deliver_result(loading, base, text: str, keyboard) -> None:
    """Edit loading zprávy výsledkem; když je moc dlouhý, smaž a pošli po částech."""
    if len(text) <= TG_MSG_LIMIT:
        try:
            await loading.edit_text(text, parse_mode="Markdown",
                                    reply_markup=keyboard, disable_web_page_preview=True)
            return
        except Exception:
            try:
                await loading.edit_text(text.replace("*", "").replace("`", "").replace("_", ""),
                                        reply_markup=keyboard)
                return
            except Exception:
                pass
    try:
        await loading.delete()
    except Exception:
        pass
    await reply_long_with_kb(base, text, keyboard)


# ── Producenti (text-vracející, sdílené příkazy i callbacky) ──────────────────
async def produce_profil(ticker: str) -> str:
    try:
        return await asyncio.wait_for(asyncio.to_thread(analyze_earnings, ticker), timeout=20.0)
    except asyncio.TimeoutError:
        return f"❌ Vypršel časový limit profilu *{ticker}* (20s)."
    except Exception as e:
        return f"❌ Chyba profilu *{ticker}*: {e}"

async def produce_genius(ticker: str) -> str:
    try:
        r = await asyncio.wait_for(gather_genius(ticker), timeout=45.0)
        text = format_genius(r)
        await asyncio.to_thread(save_flow_history)
        return text
    except asyncio.TimeoutError:
        return f"❌ Genius *{ticker}* trval moc dlouho."
    except Exception as e:
        return f"❌ Chyba: {e}"

async def produce_edge(ticker: str, years: int = EDGE_DEFAULT_YEARS) -> str:
    try:
        report = await asyncio.wait_for(asyncio.to_thread(backtest_setups, ticker, years), timeout=90.0)
        return format_edge(report)
    except asyncio.TimeoutError:
        return f"❌ Backtest *{ticker}* trval moc dlouho."
    except Exception as e:
        return f"❌ Chyba: {e}"

async def produce_flow_ticker(ticker: str) -> str:
    """Hloubkový pohled na opční flow jednoho tickeru (dřív /unusual)."""
    try:
        tk = yf_ticker(ticker)
        hist = await asyncio.wait_for(asyncio.to_thread(tk.history, period="1d"), timeout=30.0)
        if hist.empty:
            return f"❌ Nepodařilo se načíst tržní cenu pro *{ticker}*."
        spot = float(hist["Close"].iloc[-1])
        hits, market_cap = await asyncio.wait_for(
            asyncio.to_thread(analyze_options_flow, ticker, spot), timeout=30.0)
        text = format_unusual(ticker, hits, spot, market_cap)
        await asyncio.to_thread(save_flow_history)
        return text
    except asyncio.TimeoutError:
        return "❌ Skenování opčního trhu trvalo moc dlouho."
    except Exception as e:
        return f"❌ Chyba: {e}"

async def produce_news(ticker: str):
    """Vrací (text, items). Když nejsou zprávy → (None, None)."""
    items = await asyncio.to_thread(fetch_yahoo_rss, ticker)
    if not items:
        return None, None
    lines = [f"📰 *POSLEDNÍ ZPRÁVY: {ticker}*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for item in items:
        lines.append(f"🔹 *[{item['title']}]({item['link']})*\n")
    return "\n".join(lines), items

async def produce_whales_scan() -> str:
    """Plošný whale-flow sken smallcap watchlistu (dřív /whales)."""
    watchlist = WHALE_SMALLCAPS
    raw_results = await scan_universe(watchlist, get_net_whale_flow, delay=SCAN_DELAY_FLOW)
    valid_results = [r for r in raw_results if r is not None and r["net_flow"] != 0]
    zero_count = len(watchlist) - len(valid_results)
    if not valid_results:
        return "🐳 *WHALE SKEN TRHU*\n\nDnes zatím žádný výrazný pohyb."

    by_money = sorted(valid_results, key=lambda x: abs(x["net_flow"]), reverse=True)[:5]
    by_strength = sorted([r for r in valid_results if r["market_cap"] > 0],
                         key=lambda x: abs(x["flow_strength"]), reverse=True)[:5]
    by_score = sorted(valid_results, key=lambda x: abs(x["flow_score"]), reverse=True)[:5]

    lines = ["📊 *WHALE SKEN TRHU*", "━━━━━━━━━━━━━━━━━━━━━━", "🐳 *BIGGEST MONEY* _(Největší objem)_"]
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
    await asyncio.to_thread(save_flow_history)
    return "\n".join(lines)

async def produce_nasdaq() -> str:
    def analyze_for_nasdaq(ticker):
        result = make_chart(ticker, "1d", False, False)
        if not result: return None
        _, _, data = result
        if not data: return None
        if "No Setup" in data["setup_type"]: return None
        if data["score"] <= 0: return None
        return {
            "ticker": ticker,
            "type": data["setup_type"].replace("🟢 ", "").replace("🚀 ", ""),
            "score": data["score"], "sm": data["sm"], "entry": data["entry"],
            "stop": f"${data['stop']:.2f}", "t1": f"${data['t1']:.2f}",
            "rr": f"1:{data['rr_zone']:.1f}",
        }
    raw_results = await scan_universe(NASDAQ_100, analyze_for_nasdaq, delay=SCAN_DELAY_CHART)
    valid_setups = [r for r in raw_results if r is not None]
    valid_setups.sort(key=lambda x: x["score"], reverse=True)
    top_10 = valid_setups[:10]
    if not top_10:
        return "❌ Nebyly nalezeny žádné validní setupy v NASDAQ-100."
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
    return "\n".join(lines)

async def produce_darkhorse() -> str:
    watchlist = load_russell_watchlist()
    try:
        await asyncio.to_thread(yf_download, "SPY", period="1d")
    except Exception:
        pass
    def scan_darkhorse(ticker):
        result = make_chart(ticker, "1d", False, False)
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
    raw_results = await scan_universe(watchlist, scan_darkhorse, delay=SCAN_DELAY_CHART)
    valid_setups = [r for r in raw_results if r is not None]
    valid_setups.sort(key=lambda x: x["dh_score"], reverse=True)
    top_10 = valid_setups[:10]
    if not top_10:
        return "❌ Nebyly nalezeny žádné validní Dark Horse setupy (Score > 70, RR > 2)."
    lines = ["🐎 *DARK HORSE SCAN (Russell 2000)*", "━━━━━━━━━━━━━━━━━━━━━━"]
    for i, s in enumerate(top_10, 1):
        lines.append(
            f"*{i}. {s['ticker']}*\n"
            f"🏆 Score: `{s['score']}` | SM: `{s['sm']}/8`\n"
            f"⚖️ RR: `{s['rr']:.1f}R` | 🐎 DarkHorse: `{s['dh_score']:.0f}`\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ── Callbacky: kontextová navigace + menu ────────────────────────────────────
_NAV_LOADING = {
    "profil": "🧭 Skládám profil",
    "genius": "🧠 Počítám Genius",
    "flow":   "🌊 Skenuji flow",
    "news":   "📰 Stahuji zprávy",
    "chart":  "📈 Generuji graf",
    "edge":   "🔬 Backtestuji",
}

async def nav_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Zpracuje tlačítka 'nav:<akce>:<ticker>' pod výstupy."""
    query = update.callback_query
    try:
        _, action, ticker = query.data.split(":")
    except ValueError:
        await query.answer()
        return
    await query.answer()
    base = query.message
    loading = await base.reply_text(
        f"{_NAV_LOADING.get(action, '⏳ Pracuji na')} *{ticker}*...", parse_mode="Markdown")

    if action == "chart":
        try:
            png, text, _ = await asyncio.wait_for(
                asyncio.to_thread(make_chart, ticker, "1d"), timeout=30.0)
        except Exception as e:
            await loading.edit_text(f"❌ Chyba grafu: {e}")
            return
        if png is None:
            await loading.edit_text(text)
            return
        try:
            await loading.delete()
        except Exception:
            pass
        try:
            await base.reply_photo(photo=io.BytesIO(png),
                                   caption=f"📈 *Graf {ticker}* (1d)", parse_mode="Markdown")
        except Exception:
            await base.reply_photo(photo=io.BytesIO(png), caption=f"Graf {ticker} (1d)")
        await reply_long_with_kb(base, text, nav_keyboard(ticker, exclude="chart"))
        return

    if action == "news":
        text, items = await produce_news(ticker)
        if not text:
            await loading.edit_text(f"❌ Žádné zprávy pro *{ticker}*.", parse_mode="Markdown")
            return
        await deliver_result(loading, base, text, news_keyboard(ticker))
        return

    if action == "profil":
        text = await produce_profil(ticker)
    elif action == "genius":
        text = await produce_genius(ticker)
    elif action == "flow":
        text = await produce_flow_ticker(ticker)
    elif action == "edge":
        text = await produce_edge(ticker)
    else:
        text = "❓ Neznámá akce."
    await deliver_result(loading, base, text, nav_keyboard(ticker, exclude=action))


HOME_TEXT = (
    "🧠 *AKCIOVÝ GENIUS 2.0*\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Tvůj analytický parťák na akcie, opce i makro.\n\n"
    "Napiš *ticker* (např. `AAPL`) pro graf + S/R úrovně,\n"
    "nebo si vyber kategorii níže. Pod každým výstupem najdeš\n"
    "tlačítka pro rychlé přepnutí analýzy. 👇\n\n"
    "🤖 *Novinka:* `/agent on` — autonomní AI analytik, co sám "
    "skenuje trh a pošle ti jen TOP příležitosti s tezí. "
    "Svůj track record ukáže `/genius_score`."
)

MENU_TEXTS = {
    "investor": (
        "🧭 *INVESTOR — dlouhodobá analýza firmy*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• `/profil AAPL` — investiční profil + fundamentální scorecard\n"
        "   _(růst, ziskovost, rozvaha, valuace, cash flow + verdikt)_\n"
        "• `/genius AAPL` — fúze techniky + flow + news do 1 přesvědčení\n"
        "• `/edge AAPL` — backtest: historická úspěšnost setupů\n"
        "• `/news AAPL` — nejnovější zprávy + AI sentiment\n\n"
        "🤖 *GENIUS AGENT* — autonomní lov\n"
        "• `/agent on` — bot sám skenuje trh a pošle TOP picky s tezí\n"
        "• `/agent now` — spustit lov hned\n"
        "• `/genius_score` — reálná výsledkovka agenta"
    ),
    "flow": (
        "🌊 *FLOW & WHALES — kam tečou velké peníze*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• `/flow AAPL` — hloubkový opční flow tickeru (whale bloky, akumulace)\n"
        "• `/flow` — plošný whale sken trhu (bez tickeru)\n"
        "• `/akumulace` — strikes, co se nabalují víc dní po sobě\n"
        "• `/whaleradar on` — 🐋 živý radar velkých opčních bloků"
    ),
    "chart": (
        "📈 *GRAFY & SETUPY — technická analýza*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• `AAPL` nebo `RKLB 4h` — graf + S/R úrovně\n"
        "   _(TF: 1m,5m,15m,30m,1h,4h,1d,1wk,1mo)_\n"
        "• `/smc ASTS` — Smart Money Concepts (Order Blocks, FVG, sweepy)\n"
        "• `/sniper ASTS` — alert na zásah OB zóny _(vypnutí: `/sniper off ASTS`)_"
    ),
    "macro": (
        "🌍 *MAKRO — celkový pohled na trh*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• `/walter on` — makro market alerty z bleskového feedu\n"
        "• `/ai` (s PDF) — tvrdý výtah z prezentace/reportu"
    ),
    "help": (
        "❓ *KOMPLETNÍ NÁPOVĚDA*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*Investor:* `/profil` · `/genius` · `/edge` · `/news`\n"
        "*Flow:* `/flow` · `/akumulace` · `/whaleradar`\n"
        "*Grafy:* _ticker_ · `/smc` · `/sniper`\n"
        "*Skenery:* `/nasdaq` · `/darkhorse` · `/flow` (bez tickeru)\n"
        "*Makro:* `/walter` · `/ai` (PDF)\n\n"
        "💡 _Pod každým výstupem k tickeru máš tlačítka pro přepnutí analýzy._"
    ),
}

def _menu_kb(screen: str) -> InlineKeyboardMarkup:
    return scan_keyboard() if screen == "scan" else back_keyboard()

async def menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Zpracuje tlačítka rozcestníku 'menu:*'."""
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "menu:home":
        await query.edit_message_text(HOME_TEXT, parse_mode="Markdown", reply_markup=home_keyboard())
        return

    if data.startswith("menu:run:"):
        what = data.split(":")[2]
        loading = await query.message.reply_text("⏳ Spouštím sken trhu... _(může chvíli trvat)_",
                                                 parse_mode="Markdown")
        if what == "nasdaq":
            text = await produce_nasdaq()
        elif what == "whales":
            text = await produce_whales_scan()
        elif what == "darkhorse":
            text = await produce_darkhorse()
        else:
            text = "❓ Neznámý sken."
        await deliver_result(loading, query.message, text, None)
        return

    screen = data.split(":")[1]
    if screen == "scan":
        await query.edit_message_text(
            "🔍 *SKENERY TRHU*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "Klikni a spustím sken (běží na pozadí, výsledek pošlu jako novou zprávu):",
            parse_mode="Markdown", reply_markup=scan_keyboard())
        return
    text = MENU_TEXTS.get(screen, HOME_TEXT)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_menu_kb(screen))


# ==============================================================================
# 3d. GENIUS AGENT — autonomní lov nejlepších příležitostí + výsledkovka
# ==============================================================================
# Dvoufázový trychtýř:
#   1) PRESCREEN: levný technický sken celého revíru (make_chart, flow=False) →
#      shortlist nejsilnějších setupů.
#   2) HLOUBKA: jen na shortlistu pustí plnou fúzi (gather_genius: technika+flow+
#      news+earnings) a Edge backtest (backtest_setups) → reálná historická
#      úspěšnost JEHO setupu. Z toho se spočítá Agent Conviction a projdou jen
#      picky nad prahem (max 3). Každý pick se zaloguje pro výsledkovku.

AGENT_FILE = "genius_agent.json"            # odběratelé (chat_id)
AGENT_PICKS_FILE = "genius_picks.json"      # log picků pro výsledkovku
AGENT_MIN_CONVICTION = int(os.getenv("AGENT_MIN_CONVICTION", "68"))
AGENT_MAX_PICKS = int(os.getenv("AGENT_MAX_PICKS", "3"))
AGENT_PRESCREEN_SCORE = int(os.getenv("AGENT_PRESCREEN_SCORE", "60"))
AGENT_SHORTLIST = int(os.getenv("AGENT_SHORTLIST", "12"))
AGENT_DH_CHUNK = int(os.getenv("AGENT_DH_CHUNK", "50"))      # rotující dávka Russellu / cyklus
AGENT_INTERVAL = int(os.getenv("AGENT_INTERVAL", "7200"))   # intraday cyklus (s) = 2 h
AGENT_EVAL_INTERVAL = int(os.getenv("AGENT_EVAL_INTERVAL", "3600"))  # vyhodnocení picků
AGENT_PICK_MAX_HOLD = int(os.getenv("AGENT_PICK_MAX_HOLD", "20"))    # dní na vyřešení picku
AGENT_DEDUP_HOURS = int(os.getenv("AGENT_DEDUP_HOURS", "20"))        # stejný ticker neopakovat dřív

agent_chats: set = set()        # odběratelé — naplní se v main()
_agent_seen: dict = {}          # {ticker: ISO ts posledního odeslání} — dedup
_agent_dh_idx = 0               # rotující ukazatel do Russell univerza
_agent_run_lock = asyncio.Lock()

# ── Perzistence ───────────────────────────────────────────────────────────────
def load_agent_chats() -> set:
    try:
        with open(AGENT_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.error("Chyba při načítání agent odběrů: %s", e)
        return set()

def save_agent_chats() -> None:
    try:
        with open(AGENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(agent_chats), f)
    except Exception as e:
        log.error("Nepodařilo se uložit agent odběry: %s", e)

def load_agent_picks() -> list:
    try:
        with open(AGENT_PICKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.error("Chyba při načítání agent picků: %s", e)
        return []

def save_agent_picks(picks: list) -> None:
    try:
        with open(AGENT_PICKS_FILE, "w", encoding="utf-8") as f:
            json.dump(picks, f, ensure_ascii=False)
    except Exception as e:
        log.error("Nepodařilo se uložit agent picky: %s", e)

# ── Univerzum & prescreen ─────────────────────────────────────────────────────
def _agent_universe() -> list:
    """Likvidní jádro (NASDAQ-100 + smallcapy) vždy celé + rotující dávka Russellu."""
    global _agent_dh_idx
    core = list(WHALE_UNIVERSE)
    russ = load_russell_watchlist()
    chunk = []
    if russ:
        start = _agent_dh_idx % len(russ)
        chunk = russ[start:start + AGENT_DH_CHUNK]
        if len(chunk) < AGENT_DH_CHUNK:
            chunk += russ[:AGENT_DH_CHUNK - len(chunk)]
        _agent_dh_idx = (start + AGENT_DH_CHUNK) % len(russ)
    return list(dict.fromkeys(core + chunk))

def _agent_prescreen(ticker: str):
    """Levný technický filtr (jen tech, bez flow/render). Vrátí kandidáta nebo None."""
    result = make_chart(ticker, "1d", False, False)
    if not result:
        return None
    _, _, data = result
    if not data:
        return None
    st = data.get("setup_type", "")
    if "No Setup" in st:
        return None
    if data.get("score", 0) < AGENT_PRESCREEN_SCORE:
        return None
    return {"ticker": ticker, "score": data["score"], "setup_type": st}

# ── Conviction model ──────────────────────────────────────────────────────────
def _agent_build_pick(g: dict, edge: dict | None) -> dict | None:
    """Z fúze (gather_genius) + Edge reportu spočítá Agent Conviction a sbalí pick."""
    tech = g.get("tech")
    if not tech:
        return None
    tk = g.get("ticker")
    setup_type = tech.get("setup_type", "")
    direction = g.get("direction", "")
    bullish = "BULLISH" in direction
    base = int(g.get("score", 0))
    last = g.get("last") or tech.get("last")

    wr = n = exp_r = years = None
    if edge and not edge.get("insufficient") and edge.get("by_setup"):
        years = edge.get("years")
        bs = edge["by_setup"].get(setup_type)
        if bs:
            wr, n, exp_r = bs.get("wr"), bs.get("n", 0), bs.get("exp_r")

    # EDGE GATE: s historickým vzorkem blenduj, bez něj strop 70.
    if wr is not None and n >= EDGE_MIN_SAMPLE:
        conviction = round(0.6 * base + 0.4 * wr * 100)
        edge_ok = True
    else:
        conviction = min(base, 70)
        edge_ok = False

    if g.get("agree"):
        conviction += 4
    if g.get("conflict"):
        conviction -= 10
    ed = g.get("earn_days")
    earn_soon = ed is not None and 0 <= ed <= GENIUS_EARN_RISK_DAYS
    if earn_soon:
        conviction = round(conviction * 0.88)
    conviction = max(0, min(100, conviction))

    return {
        "ticker": tk, "setup_type": setup_type, "direction": direction, "bullish": bullish,
        "base": base, "conviction": conviction, "last": last,
        "entry": tech.get("entry"), "entry_bot": tech.get("entry_bot"), "entry_top": tech.get("entry_top"),
        "stop": tech.get("stop"), "t1": tech.get("t1"), "t2": tech.get("t2"), "rr_zone": tech.get("rr_zone"),
        "wr": wr, "n": n, "exp_r": exp_r, "years": years, "edge_ok": edge_ok,
        "agree": g.get("agree"), "conflict": g.get("conflict"),
        "earn_days": ed, "earn_soon": earn_soon,
        "pro": g.get("pro", []), "risk": g.get("risk", []),
    }

async def _agent_scan(manual: bool = False) -> list:
    """Spustí celý trychtýř a vrátí TOP picky (bez odeslání). Nepouští dva běhy naráz."""
    async with _agent_run_lock:
        universe = await asyncio.to_thread(_agent_universe)
        pre = await scan_universe(universe, _agent_prescreen, delay=SCAN_DELAY_CHART)
        cands = sorted([c for c in pre if c], key=lambda x: x["score"], reverse=True)[:AGENT_SHORTLIST]
        if not cands:
            return []

        sem = asyncio.Semaphore(3)
        async def deep(c):
            tk = c["ticker"]
            async with sem:
                try:
                    g = await asyncio.wait_for(gather_genius(tk), timeout=60.0)
                except Exception as e:
                    log.debug("[AGENT] fúze %s chyba: %s", tk, e)
                    return None
                if not g or not g.get("tech"):
                    return None
                try:
                    edge = await asyncio.wait_for(
                        asyncio.to_thread(backtest_setups, tk, EDGE_DEFAULT_YEARS), timeout=90.0)
                except Exception as e:
                    log.debug("[AGENT] edge %s chyba: %s", tk, e)
                    edge = None
                return _agent_build_pick(g, edge)

        built = await asyncio.gather(*[deep(c) for c in cands])
        picks = [p for p in built if p and p["bullish"] and p["conviction"] >= AGENT_MIN_CONVICTION]
        picks.sort(key=lambda x: x["conviction"], reverse=True)
        await asyncio.to_thread(save_flow_history)
        return picks[:AGENT_MAX_PICKS]

# ── Teze (grounded LLM) + formát karty ────────────────────────────────────────
def _conv_bar(score: int) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "▰" * filled + "▱" * (10 - filled)

async def _agent_thesis(p: dict) -> str:
    """LLM jen narativně poskládá tezi z DODANÝCH čísel. Fallback bez LLM."""
    if p["edge_ok"]:
        edge_line = (f"Historická úspěšnost setupu {p['setup_type']}: "
                     f"{p['wr'] * 100:.0f} % z {p['n']} obchodů za {p['years']} let "
                     f"(expectancy {p['exp_r']:+.2f}R).")
    else:
        edge_line = f"Setup {p['setup_type']} zatím nemá dost historických obchodů pro spolehlivý edge."
    earn = (f"Earnings za {p['earn_days']} dní (binární riziko)." if p["earn_soon"]
            else "Žádné earnings v dohledné době.")
    last = p["last"]
    lastfmt = f"${last:.2f}" if isinstance(last, (int, float)) else "N/A"
    pros = "; ".join(p["pro"][:4]) or "Technický setup s příznivým poměrem zisk/riziko."
    risks = "; ".join(p["risk"][:3]) or "Standardní tržní riziko."

    brief = (
        f"Ticker: {p['ticker']}\n"
        f"Aktuální cena: {lastfmt}\n"
        f"Setup: {p['setup_type']} ({p['direction']})\n"
        f"Vstupní zóna: {p['entry']}\n"
        f"Stop: {p['stop']} | Cíl 1: {p['t1']} | Cíl 2: {p['t2']} | R:R v zóně: 1:{p['rr_zone']}\n"
        f"Genius conviction: {p['base']}/100; shoda techniky a flow: {p['agree']}\n"
        f"{edge_line}\n"
        f"Pro-argumenty: {pros}\n"
        f"Rizika: {risks}\n"
        f"{earn}\n"
        f"Finální Agent conviction: {p['conviction']}/100"
    )
    prompt = (
        "Jsi disciplinovaný obchodní analytik. Na základě VÝHRADNĚ těchto faktů napiš "
        "stručnou českou tezi (3–4 věty), proč je to teď příležitost, a zakonči přesně "
        "jednou větou začínající '⚠️ Klíčové riziko:'. Nevymýšlej ŽÁDNÁ nová čísla, používej "
        "jen dodaná. Bez pozdravu, bez disclaimeru, bez investičního doporučení.\n\n"
        f"FAKTA:\n{brief}"
    )
    out = await ask_groq(prompt, temperature=0.3, model=GROQ_MODEL)
    if out and out.strip():
        return out.strip()
    return f"{pros}. {edge_line}\n⚠️ Klíčové riziko: {risks}."

def _p(x) -> str:
    return f"${x:.2f}" if isinstance(x, (int, float)) else "N/A"

def format_agent_pick(p: dict, rank: int, thesis: str) -> str:
    if p["edge_ok"]:
        edge_str = (f"📈 Úspěšnost setupu: *{p['wr'] * 100:.0f} %* "
                    f"_(n={p['n']}, {p['years']} let, exp {p['exp_r']:+.2f}R)_")
    else:
        edge_str = "📈 Úspěšnost setupu: _bez dostatečného historického vzorku_"
    lines = [
        f"*#{rank}  {p['ticker']}* — {p['direction']}",
        f"🧠 Agent conviction: *{p['conviction']}/100*  {_conv_bar(p['conviction'])}",
        f"🎯 Setup: `{p['setup_type']}`  ·  cena {_p(p['last'])}",
        "",
        thesis,
        "",
        "📋 *Plán obchodu*",
        f"📍 Vstup: `{p['entry']}`",
        f"🔴 Stop: `{_p(p['stop'])}`  ·  🟢 Cíl 1: `{_p(p['t1'])}`  ·  🟢 Cíl 2: `{_p(p['t2'])}`",
        f"⚖️ R:R v zóně: `1:{p['rr_zone']:.1f}`" if isinstance(p["rr_zone"], (int, float)) else "",
        edge_str,
    ]
    if p["earn_soon"]:
        lines.append(f"⚠️ _Earnings za {p['earn_days']} d — zvaž velikost pozice._")
    return "\n".join(l for l in lines if l != "")

# ── Výsledkovka: logování a vyhodnocení picků ────────────────────────────────
def _agent_log_pick(p: dict) -> None:
    picks = load_agent_picks()
    now = datetime.now(timezone.utc)
    picks.append({
        "id": f"{p['ticker']}-{now.strftime('%Y%m%d%H%M%S')}",
        "ticker": p["ticker"], "ts": now.isoformat(),
        "setup": p["setup_type"], "direction": p["direction"],
        "entry_ref": p["last"], "stop": p["stop"], "t1": p["t1"], "t2": p["t2"],
        "conviction": p["conviction"], "wr": p["wr"], "n": p["n"],
        "status": "open", "result": None, "ret_pct": None, "closed_ts": None,
    })
    save_agent_picks(picks)

def _agent_eval_one(rec: dict) -> dict | None:
    """Vyhodnotí jeden otevřený pick proti reálnému vývoji ceny. None = stále otevřený."""
    now_iso = datetime.now(timezone.utc).isoformat()
    entry, stop, t1 = rec.get("entry_ref"), rec.get("stop"), rec.get("t1")
    if not (entry and stop and t1) or entry <= 0:
        return {"status": "void", "result": "void", "ret_pct": None, "closed_ts": now_iso}
    try:
        start_date = datetime.fromisoformat(rec["ts"]).date().isoformat()
        hist = yf_ticker(rec["ticker"]).history(start=start_date)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    highs, lows, closes = hist["High"].tolist(), hist["Low"].tolist(), hist["Close"].tolist()
    bars = len(highs)
    for j in range(bars):
        if lows[j] <= stop:   # konzervativně: stop má přednost před cílem ve stejné svíčce
            return {"status": "stop", "result": "stop",
                    "ret_pct": (stop / entry - 1) * 100, "closed_ts": now_iso}
        if highs[j] >= t1:
            return {"status": "target", "result": "target",
                    "ret_pct": (t1 / entry - 1) * 100, "closed_ts": now_iso}
    if bars >= AGENT_PICK_MAX_HOLD:
        return {"status": "timeout", "result": "timeout",
                "ret_pct": (closes[-1] / entry - 1) * 100, "closed_ts": now_iso}
    return None

async def agent_eval_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pravidelně dovyhodnotí otevřené picky proti ceně (cíl/stop/timeout)."""
    picks = await asyncio.to_thread(load_agent_picks)
    open_picks = [p for p in picks if p.get("status") == "open"]
    if not open_picks:
        return
    changed = False
    for rec in open_picks:
        res = await asyncio.to_thread(_agent_eval_one, rec)
        if res:
            rec.update(res)
            changed = True
    if changed:
        await asyncio.to_thread(save_agent_picks, picks)

# ── Odeslání picků + orchestrace běhu ────────────────────────────────────────
async def _agent_send_card(bot, chat_id: int, text: str, keyboard) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown",
                               reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id,
                                   text=text.replace("*", "").replace("`", "").replace("_", ""),
                                   reply_markup=keyboard)
        except Exception as e:
            log.error("[AGENT] odeslání karty selhalo: %s", e)

async def _agent_deliver(bot, chat_ids: list, picks: list, manual: bool) -> None:
    """Pošle hlavičku + karty picků do daných chatů. Loguje picky jednou (pro výsledkovku)."""
    session = us_market_session()
    sess_lbl = {"pre": "před US open", "regular": "US trh otevřený",
                "after": "after-hours", "closed": "US trh zavřený"}.get(session, "")
    header = (f"🧠 *GENIUS AGENT*  _( {sess_lbl} )_\n"
              f"━━━━━━━━━━━━━━━━━━━━━━\n"
              f"Dnes mám pro tebe *{len(picks)}* "
              f"{'příležitost' if len(picks) == 1 else 'příležitosti' if len(picks) < 5 else 'příležitostí'}. "
              f"Pořadí podle přesvědčení 👇")
    # Teze paralelně (LLM), karty sériově kvůli pořadí.
    theses = await asyncio.gather(*[_agent_thesis(p) for p in picks])
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")
        except Exception:
            pass
        for i, p in enumerate(picks, 1):
            card = format_agent_pick(p, i, theses[i - 1])
            await _agent_send_card(bot, chat_id, card, nav_keyboard(p["ticker"]))
    # Výsledkovka: zaloguj každý pick jednou (ne per-chat).
    for p in picks:
        await asyncio.to_thread(_agent_log_pick, p)
        _agent_seen[p["ticker"]] = datetime.now(timezone.utc).isoformat()

def _agent_dedup_filter(picks: list) -> list:
    """Vyřadí tickery odeslané během posledních AGENT_DEDUP_HOURS hodin."""
    out = []
    now = datetime.now(timezone.utc)
    for p in picks:
        ts = _agent_seen.get(p["ticker"])
        if ts:
            try:
                age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
                if age_h < AGENT_DEDUP_HOURS:
                    continue
            except Exception:
                pass
        out.append(p)
    return out

async def agent_intraday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Automatický intraday běh: skenuje za tržních hodin a posílá odběratelům."""
    if not agent_chats:
        return
    if us_market_session() == "closed":
        return
    try:
        picks = await _agent_scan()
    except Exception as e:
        log.error("[AGENT] sken selhal: %s", e)
        return
    picks = _agent_dedup_filter(picks)
    if not picks:
        return  # automaticky mlčíme, když nic neprojde branou (žádný spam)
    await _agent_deliver(context.bot, list(agent_chats), picks, manual=False)

async def agent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/agent on|off|now|status — autonomní lov příležitostí."""
    chat_id = update.effective_chat.id
    arg = ctx.args[0].lower() if ctx.args else "status"

    if arg == "on":
        agent_chats.add(chat_id)
        save_agent_chats()
        await update.message.reply_text(
            "🤖 *GENIUS AGENT ZAPNUT*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Skenuji trh každé ~{AGENT_INTERVAL // 3600} h (jen za US tržních hodin) a pošlu ti "
            f"jen TOP {AGENT_MAX_PICKS} setupy nad prahem přesvědčení *{AGENT_MIN_CONVICTION}/100* — "
            "s plnou tezí, plánem obchodu a historickou úspěšností setupu.\n\n"
            "• `/agent now` – spustit lov hned\n"
            "• `/genius_score` – výsledkovka agenta\n"
            "• `/agent off` – vypnout",
            parse_mode="Markdown")
    elif arg == "off":
        agent_chats.discard(chat_id)
        save_agent_chats()
        await update.message.reply_text("🔕 *Genius Agent vypnut.*", parse_mode="Markdown")
    elif arg == "now":
        loading = await update.message.reply_text(
            "🤖 _Spouštím lov... skenuji revír, fúzuji signály a validuju Edgem. "
            "Chvíli to potrvá._", parse_mode="Markdown")
        try:
            picks = await _agent_scan(manual=True)
        except Exception as e:
            await loading.edit_text(f"❌ Agent selhal: {e}")
            return
        if not picks:
            await loading.edit_text(
                "🧠 *GENIUS AGENT*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "Dnes nic nestojí za upozornění — trh bez jasných setupů nad prahem. "
                "Radši mlčím, než abych tě zahltil šumem.", parse_mode="Markdown")
            return
        try:
            await loading.delete()
        except Exception:
            pass
        await _agent_deliver(ctx.bot, [chat_id], picks, manual=True)
    else:
        stav = "🟢 ZAPNUTÝ" if chat_id in agent_chats else "🔴 VYPNUTÝ"
        await update.message.reply_text(
            f"🤖 *Genius Agent:* {stav}\n"
            "Použij `/agent on`, `/agent off`, `/agent now` nebo `/genius_score`.",
            parse_mode="Markdown")

async def genius_score_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/genius_score — reálný track record agenta z uzavřených picků."""
    picks = await asyncio.to_thread(load_agent_picks)
    if not picks:
        await update.message.reply_text(
            "🎯 *VÝSLEDKOVKA AGENTA*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "Agent zatím nevydal žádný pick. Zapni ho přes `/agent on` nebo spusť `/agent now`.",
            parse_mode="Markdown")
        return
    closed = [p for p in picks if p.get("status") in ("target", "stop", "timeout")]
    open_picks = [p for p in picks if p.get("status") == "open"]
    n = len(closed)
    lines = ["🎯 *VÝSLEDKOVKA AGENTA*", "━━━━━━━━━━━━━━━━━━━━━━"]
    if n:
        wins = [p for p in closed if (p.get("ret_pct") or 0) > 0]
        rets = [p.get("ret_pct") or 0 for p in closed]
        wr = len(wins) / n * 100
        avg = sum(rets) / n
        avg_win = sum(r for r in rets if r > 0) / max(1, len(wins))
        losers = [r for r in rets if r <= 0]
        avg_loss = sum(losers) / max(1, len(losers))
        targets = sum(1 for p in closed if p["status"] == "target")
        stops = sum(1 for p in closed if p["status"] == "stop")
        timeouts = sum(1 for p in closed if p["status"] == "timeout")
        best = max(closed, key=lambda p: p.get("ret_pct") or -999)
        worst = min(closed, key=lambda p: p.get("ret_pct") or 999)
        lines += [
            f"📊 Uzavřených picků: *{n}*  ·  otevřených: *{len(open_picks)}*",
            f"✅ Win-rate: *{wr:.0f} %*  ({len(wins)}/{n})",
            f"💰 Ø výsledek: *{avg:+.1f} %*  (Ø zisk {avg_win:+.1f} % / Ø ztráta {avg_loss:+.1f} %)",
            f"🎯 Cíl: `{targets}`  ·  🔴 Stop: `{stops}`  ·  ⏳ Timeout: `{timeouts}`",
            f"🏆 Nej: *{best['ticker']}* {best.get('ret_pct', 0):+.1f} %  ·  "
            f"💩 Nej-: *{worst['ticker']}* {worst.get('ret_pct', 0):+.1f} %",
        ]
    else:
        lines.append(f"📊 Zatím *0* uzavřených picků  ·  otevřených: *{len(open_picks)}*")
        lines.append("_Výsledky se objeví, jakmile picky zasáhnou cíl/stop nebo vyprší._")
    if open_picks:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("📂 *Otevřené picky*")
        for p in sorted(open_picks, key=lambda x: x.get("ts", ""), reverse=True)[:6]:
            d = (p.get("ts") or "")[:10]
            lines.append(f"  • *{p['ticker']}* `{p.get('setup', '')}` "
                         f"(conv {p.get('conviction', '?')}, {d})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HOME_TEXT, parse_mode="Markdown", reply_markup=home_keyboard())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *AKCIOVÝ GENIUS 2.0 — PŘEHLED PŘÍKAZŮ*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🧭 Investor (akcie)*\n"
        "• `/profil AAPL` – investiční profil + fundamentální scorecard\n"
        "• `/genius AAPL` – fúze techniky + flow + news (0–100)\n"
        "• `/edge AAPL` – backtest historické úspěšnosti setupů\n"
        "• `/news AAPL` – zprávy + AI sentiment\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🌊 Flow & Whales*\n"
        "• `/flow AAPL` – hloubkový opční flow tickeru (whale bloky, akumulace)\n"
        "• `/flow` – plošný whale sken trhu (bez tickeru)\n"
        "• `/akumulace` – 🧲 strikes, co se nabalují víc dní (volitelně `/akumulace PLTR`)\n"
        "• `/whaleradar on` – 🐋 živý radar velkých opčních bloků\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*📈 Grafy & setupy*\n"
        "• `AAPL` nebo `RKLB 4h` – graf + S/R úrovně (TF: 1m,5m,15m,30m,1h,4h,1d,1wk,1mo)\n"
        "• `/smc ASTS` – Smart Money Concepts (Order Blocks, FVG, sweepy)\n"
        "• `/sniper ASTS` – alert na zásah OB zóny (vypnutí: `/sniper off ASTS`)\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🔍 Skenery & makro*\n"
        "• `/nasdaq` – TOP 10 setupů z NASDAQ-100\n"
        "• `/darkhorse` – skryté příležitosti z Russell 2000\n"
        "• `/walter` – makro market alerty\n"
        "• `/ai` (s PDF) – tvrdý výtah z prezentace/reportu\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*🤖 Genius Agent (autonomní lov)*\n"
        "• `/agent on` – bot sám skenuje trh a posílá TOP picky s tezí\n"
        "• `/agent now` – spustit lov hned  ·  `/agent off` – vypnout\n"
        "• `/genius_score` – reálná výsledkovka agenta (win-rate, Ø zisk)\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 _Tip: napiš `/start` pro proklikávací menu. Pod každým výstupem máš "
        "tlačítka pro přepnutí analýzy na stejném tickeru._",
        parse_mode="Markdown", reply_markup=home_keyboard())

last_market_text = ""

def get_smc_zones(df):
    """SMC zóny: Order Blocks (oddělené od FVG) + FVG, s realistickou mitigací.

    OB = poslední opačná svíčka před displacement (impulzní) svíčkou.
    FVG = 3-svíčkový gap.
    Mitigace = pozdější svíčka UZAVŘE přes 50 % zóny (ne pouhý dotyk knotem),
    což odpovídá tomu, jak se zóny reálně „spotřebují". Mitigované zóny se
    nezahazují — označí se flagem `mitigated`, aby šly vykreslit jako slabší.
    Vrací 4-tuple dictů s klíči: top, bot, start_idx, mitigated (+ OB má `vol`).
    """
    bull_fvg, bear_fvg = [], []
    bull_ob, bear_ob = [], []

    n = len(df)
    if n < 5:
        return bull_fvg, bear_fvg, bull_ob, bear_ob

    opens = df['Open'].astype(float).values
    highs = df['High'].astype(float).values
    lows = df['Low'].astype(float).values
    closes = df['Close'].astype(float).values
    vols = df['Volume'].astype(float).values if 'Volume' in df.columns else np.zeros(n)
    idx = df.index

    body = np.abs(closes - opens)
    avg_body = float(body.mean()) if n else 0.0
    current_price = float(closes[-1])

    def zone_status(top, bot, start_pos, direction):
        """Stav zóny od jejího vzniku:
        - 'broken' = cena UZAVŘELA úplně skrz (bull pod bot / bear nad top) → neplatná
        - 'tapped' = cena se dotkla/uzavřela přes 50 % zóny, ale neprolomila
        - 'fresh'  = zóna ještě netknutá
        """
        mid = (top + bot) / 2.0
        tapped = False
        for j in range(start_pos + 1, n):
            if direction == 'bull':
                if closes[j] < bot:
                    return 'broken'
                if closes[j] <= mid:
                    tapped = True
            else:
                if closes[j] > top:
                    return 'broken'
                if closes[j] >= mid:
                    tapped = True
        return 'tapped' if tapped else 'fresh'

    # --- ORDER BLOCKS (přes displacement, nezávisle na FVG) ---
    for i in range(1, n - 1):
        if avg_body <= 0 or body[i] < avg_body * 1.5:
            continue  # i = impulzní (displacement) svíčka
        if closes[i] > opens[i]:
            # bullish OB = poslední bearish svíčka před up-impulzem
            for k in range(i - 1, max(-1, i - 6), -1):
                if closes[k] < opens[k]:
                    ob_t, ob_b = float(highs[k]), float(lows[k])
                    if highs[i] <= ob_t:
                        break  # impulz neprorazil nad OB → neplatné
                    status = zone_status(ob_t, ob_b, i, 'bull')
                    bull_ob.append({'top': ob_t, 'bot': ob_b, 'start_idx': idx[k], 'pos': k,
                                    'vol': float(vols[k]), 'status': status,
                                    'mitigated': status == 'tapped'})
                    break
        elif closes[i] < opens[i]:
            # bearish OB = poslední bullish svíčka před down-impulzem
            for k in range(i - 1, max(-1, i - 6), -1):
                if closes[k] > opens[k]:
                    ob_t, ob_b = float(highs[k]), float(lows[k])
                    if lows[i] >= ob_b:
                        break
                    status = zone_status(ob_t, ob_b, i, 'bear')
                    bear_ob.append({'top': ob_t, 'bot': ob_b, 'start_idx': idx[k], 'pos': k,
                                    'vol': float(vols[k]), 'status': status,
                                    'mitigated': status == 'tapped'})
                    break

    # --- FVG (3-svíčkový gap) ---
    for i in range(2, n):
        if lows[i] > highs[i - 2]:  # bullish FVG
            top, bot = float(lows[i]), float(highs[i - 2])
            status = zone_status(top, bot, i, 'bull')
            bull_fvg.append({'top': top, 'bot': bot, 'start_idx': idx[i - 1], 'pos': i - 1,
                             'status': status, 'mitigated': status == 'tapped'})
        if highs[i] < lows[i - 2]:  # bearish FVG
            top, bot = float(lows[i - 2]), float(highs[i])
            status = zone_status(top, bot, i, 'bear')
            bear_fvg.append({'top': top, 'bot': bot, 'start_idx': idx[i - 1], 'pos': i - 1,
                             'status': status, 'mitigated': status == 'tapped'})

    def merge_overlap(zones):
        """Sloučí překrývající se zóny stejného typu (odstraní skoro-duplicity).
        Výsledná zóna pokrývá sjednocení rozsahů, origin = ta novější svíčka."""
        zones = sorted(zones, key=lambda z: z['bot'])
        out = []
        for z in zones:
            if out and z['bot'] <= out[-1]['top']:  # překryv s poslední
                m = out[-1]
                m['top'] = max(m['top'], z['top'])
                m['bot'] = min(m['bot'], z['bot'])
                if z['pos'] > m['pos']:                      # ponech novější origin
                    m['pos'], m['start_idx'] = z['pos'], z['start_idx']
                if m.get('status') == 'tapped' and z.get('status') == 'fresh':
                    m['status'], m['mitigated'] = 'fresh', False
            else:
                out.append(dict(z))
        return out

    # Filtry: zahoď prolomené (broken) i příliš staré (stale) zóny — obojí jen
    # zaneřádí graf. Pak slouč překryvy a vyber nejrelevantnější (fresh + blízké).
    def rank(zones, edge):
        zones = [z for z in zones
                 if z.get('status') != 'broken' and (n - z['pos']) <= SMC_ZONE_MAX_AGE]
        zones = merge_overlap(zones)
        return sorted(zones, key=lambda z: (z.get('mitigated', False),
                                            abs(current_price - z[edge])))[:3]

    bull_ob = rank(bull_ob, 'top')
    bear_ob = rank(bear_ob, 'bot')
    bull_fvg = rank(bull_fvg, 'top')
    bear_fvg = rank(bear_fvg, 'bot')

    return bull_fvg, bear_fvg, bull_ob, bear_ob

async def smc_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Použití: `/smc ONDS`", parse_mode="Markdown")
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Kompletuji Premium SMC Profil pro *{ticker}*...", parse_mode="Markdown")
    
    try:
        df = await asyncio.to_thread(cached_yf_download, ticker, "5d", "15m")
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
                used = z.get('mitigated', False)
                # Zasažené (mitigated) zóny kreslíme slabší a přerušovaně, aby čerstvé vynikly.
                fill = color.replace("0.25", "0.08").replace("0.1", "0.04") if used else color
                lbl = f"{name}·" if used else name
                fig.add_shape(type="rect", x0=start_str, x1=end_str, y0=z['bot'], y1=z['top'],
                              fillcolor=fill,
                              line=dict(color=border, width=1, dash="dot" if used else "solid"),
                              layer="below")
                fig.add_annotation(x=start_str, y=(z['top']+z['bot'])/2, text=lbl, showarrow=False,
                                   font=dict(color=border, size=10), xanchor="left",
                                   opacity=0.45 if used else 1.0)

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

        # --- AKČNÍ VÝSTUP: nejbližší čerstvé OB + entry/stop/target + RR ---
        fresh_bull = [z for z in bull_ob if not z.get('mitigated')]
        fresh_bear = [z for z in bear_ob if not z.get('mitigated')]

        # Je cena PRÁVĚ TEĎ v nějaké čerstvé zóně?
        in_zone = None
        for z in fresh_bull:
            if z['bot'] <= current_price <= z['top']:
                in_zone = ("LONG 🟢", z); break
        if not in_zone:
            for z in fresh_bear:
                if z['bot'] <= current_price <= z['top']:
                    in_zone = ("SHORT 🔴", z); break

        # Nejbližší poptávkový OB pod cenou (LONG) a nabídkový nad cenou (SHORT)
        demand = sorted([z for z in fresh_bull if z['top'] <= current_price],
                        key=lambda z: current_price - z['top'])
        supply = sorted([z for z in fresh_bear if z['bot'] >= current_price],
                        key=lambda z: z['bot'] - current_price)

        akce = []
        if in_zone:
            side, z = in_zone
            akce.append(f"⚡ *Cena je TEĎ v {side} OB* `${z['bot']:.2f}–{z['top']:.2f}`")
        if demand:
            z = demand[0]
            entry, stop, target = z['top'], z['bot'], recent_high
            rr = (target - entry) / (entry - stop) if (entry - stop) > 0 else 0
            akce.append(f"🟢 *LONG OB:* `${z['bot']:.2f}–{z['top']:.2f}` → 🎯 `${target:.2f}` _(RR {rr:.1f})_")
        if supply:
            z = supply[0]
            entry, stop, target = z['bot'], z['top'], recent_low
            rr = (entry - target) / (stop - entry) if (stop - entry) > 0 else 0
            akce.append(f"🔴 *SHORT OB:* `${z['bot']:.2f}–{z['top']:.2f}` → 🎯 `${target:.2f}` _(RR {rr:.1f})_")
        akce_text = "\n".join(akce) if akce else "_Žádné čerstvé OB poblíž ceny._"

        text_zpravy = (
            f"🎯 *Premium SMC Profil: {ticker} (15m)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 *Cena:* `${current_price:.2f}`  |  ⚖️ *P/D:* `{pd_status}`\n"
            f"🎯 *Čerstvé OB:* `Bull {len(fresh_bull)} | Bear {len(fresh_bear)}` "
            f"_(celkem {len(bull_ob)}/{len(bear_ob)})_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{akce_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _Slabší přerušované zóny = už zasažené. RR cílí na BSL/SSL likviditu._\n"
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
                    asyncio.to_thread(yf_download, ticker, period="3d", interval="15m"),
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
                    if ob.get('mitigated'):
                        continue  # už zasažený OB nealertujeme
                    if current_low <= ob['top'] and current_price >= ob['bot']:
                        alert_msg = f"🟢 *LONG ALERT ({ticker})*\nCena propíchla čerstvý Bullish Order Block (`${ob['top']:.2f}`). Hledej long!"
                        break
                for ob in bear_ob:
                    if ob.get('mitigated'):
                        continue
                    if current_high >= ob['bot'] and current_price <= ob['top']:
                        alert_msg = f"🔴 *SHORT ALERT ({ticker})*\nCena zasáhla čerstvý Bearish Order Block (`${ob['bot']:.2f}`). Hledej short!"
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
        
        # Detekce = jednoduchá klasifikace → levný 8b model (šetří 70b denní kvótu).
        ai_raw = await ask_groq(prompt_detekce, temperature=0, model=GROQ_FAST_MODEL)
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
                        # Poslední kline z Binance je právě se tvořící svíčka → pro objem
                        # ber poslední UZAVŘENOU (předposlední), cenu z té nejnovější.
                        closed_btc = df_btc.iloc[:-1]
                        if len(closed_btc) >= 11:
                            aktualni_cena = float(df_btc["Close"].iloc[-1])
                            aktualni_vol = float(closed_btc["Volume"].iloc[-1])
                            prumer_vol_10m = float(closed_btc["Volume"].iloc[-11:-1].mean())
                            atr = compute_atr(closed_btc)
                            ma_data = True
            except Exception as e:
                log.error("Chyba Binance API: %s", e)

        else:
            data_1m = await asyncio.to_thread(yf_download, target_ticker, period="1d", interval="1m")
            if not data_1m.empty and len(data_1m) > 10:
                if isinstance(data_1m.columns, pd.MultiIndex):
                    if 'Close' in data_1m.columns.get_level_values(0):
                        data_1m.columns = data_1m.columns.get_level_values(0)
                    else:
                        data_1m.columns = data_1m.columns.get_level_values(-1)

                data_1m.columns = [str(c).strip() for c in data_1m.columns]

                if 'Close' in data_1m.columns and 'Volume' in data_1m.columns:
                    # Nejnovější cena (i z právě se tvořící svíčky).
                    last_price = float(data_1m['Close'].dropna().iloc[-1]) if data_1m['Close'].notna().any() else 0.0
                    # Objem počítej JEN z uzavřených svíček s reálným objemem —
                    # poslední 1m bar z yfinance bývá rozpracovaný/nulový (proto „0x normál").
                    closed = data_1m.dropna(subset=['Close'])
                    closed = closed[closed['Volume'].fillna(0) > 0]
                    if last_price > 0 and len(closed) >= 11:
                        aktualni_cena = last_price
                        aktualni_vol = float(closed['Volume'].iloc[-1])
                        prumer_vol_10m = float(closed['Volume'].iloc[-11:-1].mean())
                        atr = compute_atr(closed)
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

            await safe_send(context.bot, context.job.chat_id, zprava)
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

        await safe_send(context.bot, context.job.chat_id, zprava_makro)
        
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

async def news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/news AAPL`", parse_mode="Markdown")
        return
        
    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Stahuji zprávy pro {ticker} přes nezávislý RSS kanál...")

    text, items = await produce_news(ticker)
    if not text:
        await msg.edit_text(f"❌ Žádné zprávy pro '{ticker}' (Nebo je špatný ticker).")
        return

    try:
        await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True,
                            reply_markup=news_keyboard(ticker))
    except Exception:
        await msg.edit_text(text, disable_web_page_preview=True, reply_markup=news_keyboard(ticker))

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
        await query.edit_message_text(final_text, parse_mode="Markdown",
                                      reply_markup=nav_keyboard(ticker, exclude="news"))

    except Exception as e:
        await query.edit_message_text(f"❌ Chyba AI analýzy: {str(e)}")

async def nasdaq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"⏳ Spouštím masivní skener pro NASDAQ-100...\n"
        f"Stahuji data a analyzuji {len(NASDAQ_100)} akcií. Může to trvat 1-2 minuty.",
        parse_mode="Markdown",
    )
    text = await produce_nasdaq()
    try: await msg.edit_text(text, parse_mode="Markdown")
    except Exception: await msg.edit_text(text.replace("*", "").replace("`", ""))

async def darkhorse_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"⏳ *Skenuji temné koně trhu...*\n"
        f"Analyzuji akcie z Russell 2000.",
        parse_mode="Markdown",
    )
    text = await produce_darkhorse()
    try: await msg.edit_text(text, parse_mode="Markdown")
    except Exception: await msg.edit_text(text.replace("*", "").replace("`", ""))

async def flow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🌊 Flow & Whales — s tickerem hloubkový opční flow, bez tickeru sken trhu.
    Sloučení původních /unusual + /whales do jednoho příkazu."""
    if ctx.args:
        ticker = ctx.args[0].upper()
        msg = await update.message.reply_text(f"⏳ Skenuji opční trh pro *{ticker}*...", parse_mode="Markdown")
        text = await produce_flow_ticker(ticker)
        await deliver_result(msg, update.message, text, nav_keyboard(ticker, exclude="flow"))
    else:
        msg = await update.message.reply_text(
            f"⏳ Spouštím plošný whale sken ({len(WHALE_SMALLCAPS)} tickerů)...",
            parse_mode="Markdown",
        )
        text = await produce_whales_scan()
        await deliver_result(msg, update.message, text, None)

async def akumulace_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Co se právě nabaluje: strikes s vícedenní akumulací napříč pamětí enginu.
    Volitelně filtruj na ticker: `/akumulace PLTR`."""
    load_flow_history()
    ticker_filter = ctx.args[0].upper() if ctx.args else None

    with _flow_lock:
        items = [(k, dict(v)) for k, v in _flow_history.items()]

    rows = []
    for _key, rec in items:
        if ticker_filter and rec.get("ticker") != ticker_filter:
            continue
        accum = _accum_from_history(rec.get("history", []))
        if not accum or not accum["is_accum"]:
            continue
        rows.append((rec, accum))

    if not rows:
        scope = f" pro *{ticker_filter}*" if ticker_filter else ""
        await update.message.reply_text(
            f"🧲 *Akumulace{scope}*\n"
            f"Zatím nic, co by se nabalovalo víc dní po sobě.\n"
            f"_Paměť se plní průběžně, jak běží whale radar a skenery — vrať se za pár dní._",
            parse_mode="Markdown")
        return

    rows.sort(key=lambda r: r[1]["cum_premium"], reverse=True)

    scope = f" — {ticker_filter}" if ticker_filter else ""
    lines = [
        f"🧲 *AKUMULACE{scope}* — co se právě nabaluje",
        "_(strikes s rostoucím OI/prémií víc dní po sobě = někdo staví pozici)_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for rec, accum in rows[:12]:
        ot = "📞 C" if rec["opt_type"] == "call" else "📉 P"
        lines.append(
            f"*{rec['ticker']}* {ot} `${rec['strike']:.0f}` | exp {rec['exp']}\n"
            f"  {accum['label']} `{accum['days']}` dní | OI ×{accum['oi_growth']:.1f} | "
            f"prémie ×{accum['prem_growth']:.1f} | Σ {fmt_usd(accum['cum_premium'])}\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 _Vícedenní akumulace > jednorázový blok. Sleduj, kam plynou peníze opakovaně._")
    await reply_long(update.message, "\n".join(lines))

async def whaleradar_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Zapne/vypne proaktivní Whale Radar pro tento chat."""
    chat_id = update.effective_chat.id
    arg = ctx.args[0].lower() if ctx.args else "status"

    if arg == "on":
        whale_radar_chats.add(chat_id)
        save_whale_chats()
        await update.message.reply_text(
            f"🐋 *WHALE RADAR ZAPNUT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Skenuji `{len(WHALE_UNIVERSE)}` tickerů na velké agresivní opční bloky (na/u asku):\n"
            f"  • velké akcie ≥ `{fmt_usd(WHALE_MIN_PREMIUM)}`\n"
            f"  • smallcapy ≥ `{fmt_usd(WHALE_MIN_PREMIUM_SMALL)}`\n"
            f"Pingnu tě, jakmile někdo vsadí velké peníze. 🐳\n"
            f"_Vypnutí:_ `/whaleradar off`",
            parse_mode="Markdown")
    elif arg == "off":
        whale_radar_chats.discard(chat_id)
        save_whale_chats()
        await update.message.reply_text("🔕 *Whale Radar vypnut.*", parse_mode="Markdown")
    else:
        stav = "🟢 ZAPNUTÝ" if chat_id in whale_radar_chats else "🔴 VYPNUTÝ"
        await update.message.reply_text(
            f"🐋 *Whale Radar:* {stav}\n"
            f"Použij `/whaleradar on` nebo `/whaleradar off`.",
            parse_mode="Markdown")

async def earnings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Použití: `/profil AAPL` (investiční profil + fundamentální scorecard)", parse_mode="Markdown")
        return

    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(f"⏳ Skládám investiční profil *{ticker}*...", parse_mode="Markdown")
    text = await produce_profil(ticker)
    await deliver_result(msg, update.message, text, nav_keyboard(ticker, exclude="profil"))

# Ticker = 1–6 písmen, volitelně přípona jako -USD (BTC-USD) nebo .B (BRK.B).
_TICKER_RE = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,4})?$")
# Častá česká/anglická chatová slova, co vypadají jako ticker (ASCII, ≤6 písmen).
_NON_TICKER_WORDS = {
    "AHOJ", "DIKY", "DIK", "CO", "ANO", "NE", "JAK", "ALE", "PROC", "KDE", "KDY",
    "JO", "JJ", "OK", "OKEJ", "DOBRE", "SUPER", "DALE", "DAL", "HELP", "TEST",
    "HI", "HELLO", "THX", "YES", "NO", "WHY", "WHAT", "NICE", "COOL",
    "TOHLE", "TADY", "CAU", "AHOJTE", "MOC", "VIC", "TAKE", "PAK", "UZ", "TEDY",
}

async def handle_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if not parts:
        return
    ticker = parts[0].upper()
    interval = parts[1].lower() if len(parts) > 1 else "1d"

    # Filtr: běžná věta („ahoj", „díky") není ticker → tiše ignoruj, ať bot
    # nespouští marné stahování grafu a neodpovídá chybou na každou zprávu.
    if not _TICKER_RE.match(ticker) or ticker in _NON_TICKER_WORDS:
        return

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
    await update.message.reply_text("↔️ _Přepnout analýzu na_ "
                                    f"*{ticker}*:", parse_mode="Markdown",
                                    reply_markup=nav_keyboard(ticker, exclude="chart"))

async def genius_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🧠 Genius Score — sloučí techniku, options flow a news do 1 přesvědčení."""
    if not ctx.args:
        await update.message.reply_text("Použití: `/genius AAPL`", parse_mode="Markdown")
        return

    ticker = ctx.args[0].upper()
    msg = await update.message.reply_text(
        f"🧠 Skládám *Genius Score* pro *{ticker}* (technika + flow + news)...",
        parse_mode="Markdown",
    )
    text = await produce_genius(ticker)
    await deliver_result(msg, update.message, text, nav_keyboard(ticker, exclude="genius"))

async def edge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🔬 Edge Lab — backtestuje úspěšnost setupů na historii (`/edge AAPL [roky]`)."""
    if not ctx.args:
        await update.message.reply_text("Použití: `/edge AAPL` (volitelně počet let: `/edge AAPL 3`)", parse_mode="Markdown")
        return

    ticker = ctx.args[0].upper()
    years = EDGE_DEFAULT_YEARS
    if len(ctx.args) > 1:
        try: years = max(1, min(10, int(ctx.args[1])))
        except ValueError: pass

    msg = await update.message.reply_text(
        f"🔬 Backtestuji setupy pro *{ticker}* na {years} letech historie... _(chvíli to trvá)_",
        parse_mode="Markdown",
    )
    text = await produce_edge(ticker, years)
    await deliver_result(msg, update.message, text, nav_keyboard(ticker, exclude="edge"))

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
async def error_handler(update, context):
    """Globální zachytávač chyb — ať jedna výjimka neshodí bota ani nespamuje traceback."""
    err = context.error
    if isinstance(err, Conflict):
        # Dvě instance bota se stejným tokenem se perou o getUpdates.
        # Bývá to přechodné (rolling deploy) — loguj stručně, neřeš tracebackem.
        log.warning("⚠️ Conflict: běží jiná instance bota se stejným tokenem (getUpdates). "
                    "Zkontroluj, že běží jen JEDNA instance.")
        return
    if isinstance(err, NetworkError):
        log.warning("Síťová chyba (přechodná): %s", err)
        return
    log.error("Neošetřená výjimka v handleru: %s", err, exc_info=err)

def main():
    if not TOKEN:
        log.error("CHYBA: Chybí TELEGRAM_TOKEN! Nastav ho v .env souboru.")
        return

    # Obnov aktivní snipery z minulého běhu
    global active_snipers, whale_radar_chats, agent_chats
    active_snipers = load_snipers()
    if active_snipers:
        log.info("Obnoveno %d chatů s aktivními snipery.", len(active_snipers))

    # Obnov odběratele Whale Radaru
    whale_radar_chats = load_whale_chats()
    if whale_radar_chats:
        log.info("Obnoveno %d chatů s aktivním Whale Radarem.", len(whale_radar_chats))

    # Obnov odběratele Genius Agenta
    agent_chats = load_agent_chats()
    if agent_chats:
        log.info("Obnoveno %d chatů s aktivním Genius Agentem.", len(agent_chats))

    # Obnov flow paměť (akumulace/distribuce) z minulého běhu
    load_flow_history()
    if _flow_history:
        log.info("Obnoveno %d strike-záznamů ve flow paměti.", len(_flow_history))
    atexit.register(save_flow_history)   # při vypnutí dolož poslední stav na disk

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler(["flow", "unusual", "whales"], flow_cmd))
    app.add_handler(CommandHandler(["genius", "g"], genius_cmd))
    app.add_handler(CommandHandler(["edge", "backtest"], edge_cmd))
    app.add_handler(CommandHandler(["profil", "investice", "earnings"], earnings_cmd))
    app.add_handler(CallbackQueryHandler(ai_news_callback, pattern="^ainews_"))
    app.add_handler(CallbackQueryHandler(nav_callback, pattern="^nav:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CommandHandler("nasdaq", nasdaq_cmd))
    app.add_handler(CommandHandler("walter", cmd_walter))
    app.add_handler(CommandHandler("smc", smc_cmd))
    app.add_handler(CommandHandler("sniper", sniper_cmd))
    app.add_handler(CommandHandler("darkhorse", darkhorse_cmd))
    app.add_handler(CommandHandler(["akumulace", "accumulation"], akumulace_cmd))
    app.add_handler(CommandHandler("whaleradar", whaleradar_cmd))
    app.add_handler(CommandHandler("agent", agent_cmd))
    app.add_handler(CommandHandler(["genius_score", "skore", "vysledkovka"], genius_score_cmd))
    app.add_handler(CommandHandler("ai", ai_pdf_cmd))
    app.add_handler(MessageHandler(filters.Document.PDF & filters.CaptionRegex(r'^/ai'), ai_pdf_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticker))

    # Globální error handler (mj. tiší spam z Conflict při překryvu instancí)
    app.add_error_handler(error_handler)

    # Naplánuj SMC Sniper skener na pozadí (každou minutu), pokud je JobQueue dostupná
    if app.job_queue:
        app.job_queue.run_repeating(sniper_background_task, interval=60, first=15, name="sniper_job")
        app.job_queue.run_repeating(whale_radar_loop, interval=WHALE_RADAR_INTERVAL, first=30, name="whale_radar_job")
        app.job_queue.run_repeating(flow_history_flush_job, interval=120, first=120, name="flow_flush_job")
        app.job_queue.run_repeating(agent_intraday_job, interval=AGENT_INTERVAL, first=90, name="agent_job")
        app.job_queue.run_repeating(agent_eval_job, interval=AGENT_EVAL_INTERVAL, first=300, name="agent_eval_job")
    else:
        log.warning("JobQueue není dostupná – SMC Sniper poběží jen po /sniper. Nainstaluj python-telegram-bot[job-queue].")

    log.info("✅ Bot běží. Zastav pomocí Ctrl+C.")
    # drop_pending_updates=True → po restartu zahodí nahromaděný backlog (čistší start)
    app.run_polling(drop_pending_updates=True)
    
if __name__ == "__main__":
    main()
