"""
📈 매수 알람 시스템 v5
- 한국 주식 이름 검색: KRX 공식 REST API
- 한국 주식 데이터: yfinance .KS 티커 (원화 기준)
- 미국/암호화폐: yfinance
"""

import time, threading, logging, json, os
from datetime import datetime
import pandas as pd
import yfinance as yf
import requests
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WATCHLIST,
    STOCH_K_PERIOD, STOCH_D_PERIOD, STOCH_OVERSOLD,
    RSI_PERIOD, RSI_OVERSOLD, CHECK_INTERVAL_MINUTES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("alert.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

WATCHLIST_FILE = "watchlist.json"
NAMES_FILE     = "ticker_names.json"
KRX_MAP_FILE   = "krx_map.json"

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except: pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def get_display(ticker):
    name = ticker_names.get(ticker)
    return "%s (%s)" % (name, ticker) if name else ticker

current_watchlist = load_json(WATCHLIST_FILE, list(WATCHLIST))
ticker_names      = load_json(NAMES_FILE, {})
krx_name_map      = {}

# ── KRX 공식 REST API로 종목 맵 빌드 ──
def build_krx_map():
    global krx_name_map
    cached = load_json(KRX_MAP_FILE, {})
    if cached:
        krx_name_map = cached
        log.info("KRX 종목 맵 로드 완료: %d개" % len(krx_name_map))
        return
    log.info("KRX 종목 맵 생성 중...")
    try:
        url     = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://data.krx.co.kr/"}
        result  = {}
        for mktId in ["STK", "KSQ"]:
            payload = {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT01901",
                "mktId": mktId, "share": "1", "csvxls_isNo": "false",
            }
            r    = requests.post(url, data=payload, headers=headers, timeout=15)
            data = r.json()
            for item in data.get("OutBlock_1", []):
                name = item.get("ISU_ABBRV", "").strip()
                code = item.get("ISU_SRT_CD", "").strip()
                if name and code:
                    result[name] = code
        if result:
            krx_name_map = result
            save_json(KRX_MAP_FILE, krx_name_map)
            log.info("KRX 맵 완료: %d개" % len(krx_name_map))
        else:
            log.warning("KRX 맵 비어있음")
    except Exception as e:
        log.error("KRX 맵 오류: %s" % str(e))

def search_krx(query):
    q = query.strip()
    if q in krx_name_map:
        return (krx_name_map[q] + ".KS", q)
    for name, code in krx_name_map.items():
        if q.lower() in name.lower():
            return (code + ".KS", name)
    return None

def is_korean(ticker):
    return ticker.upper().endswith(".KS") or ticker.upper().endswith(".KQ")

def find_ticker(query):
    q, qu = query.strip(), query.strip().upper()
    if qu.endswith(".KS") or qu.endswith(".KQ"):
        return (qu, ticker_names.get(qu, qu))
    if q.isdigit() and len(q) == 6:
        return (q + ".KS", ticker_names.get(q + ".KS", q))
    if qu.replace("-", "").isalnum() and all(c.isascii() for c in q):
        return (qu, qu)
    # 한글 → KRX 맵 검색
    res = search_krx(q)
    if res: return res
    # 맵 없으면 재빌드 후 재검색
    if not krx_name_map:
        build_krx_map()
        return search_krx(q)
    return None

# ── 지표 계산 ──
def calc_stoch(df, k, d):
    lo = df["Low"].rolling(k).min()
    hi = df["High"].rolling(k).max()
    df = df.copy()
    df["%K"] = (df["Close"] - lo) / (hi - lo) * 100
    df["%D"] = df["%K"].rolling(d).mean()
    return df

def calc_rsi(df, p):
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(p).mean()
    loss  = (-delta.clip(upper=0)).rolling(p).mean()
    df    = df.copy()
    df["RSI"] = 100 - (100 / (1 + gain / loss))
    return df

def buy_signal(df):
    l = df.iloc[-1]
    return bool(l["%K"] <= STOCH_OVERSOLD and l["RSI"] <= RSI_OVERSOLD)

# ── 데이터 수집 ──
def fetch(ticker):
    try:
        df = yf.download(ticker, period="90d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20: return None
        df.columns = df.columns.get_level_values(0)
        latest = float(df["Close"].iloc[-1])
        if is_korean(ticker):
            log.info("[%s] 수집 완료 (%dd) 최근가: %s원" % (ticker, len(df), "{:,.0f}".format(latest)))
        else:
            log.info("[%s] 수집 완료 (%dd) 최근가: %.2f" % (ticker, len(df), latest))
        return df
    except Exception as e:
        log.error("[%s] 수집 오류: %s" % (ticker, str(e)))
        return None

# ── 텔레그램 ──
def tg(msg, cid=None):
    try:
        requests.post(
            "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN,
            json={"chat_id": cid or TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error("TG 오류: %s" % str(e))

def get_updates(offset=None):
    try:
        params = {"timeout": 30}
        if offset: params["offset"] = offset
        r = requests.get("https://api.telegram.org/bot%s/getUpdates" % TELEGRAM_BOT_TOKEN, params=params, timeout=35)
        if r.status_code == 200: return r.json().get("result", [])
    except: pass
    return []

# ── 명령어 처리 ──
def handle(text, cid):
    global current_watchlist, ticker_names
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/add" and arg:
        tg("⏳ *%s* 검색 중..." % arg, cid)
        res = find_ticker(arg)
        if not res:
            tg("❌ *%s* 를 찾을 수 없어요.\n예: `/add 삼성전자` `/add AAPL` `/add BTC-USD`" % arg, cid)
            return
        ticker, name = res
        if ticker in current_watchlist:
            tg("⚠️ *%s* 는 이미 모니터링 중!" % get_display(ticker), cid); return
        if fetch(ticker) is None:
            tg("❌ `%s` 데이터를 가져올 수 없어요." % ticker, cid); return
        current_watchlist.append(ticker)
        ticker_names[ticker] = name
        save_json(WATCHLIST_FILE, current_watchlist)
        save_json(NAMES_FILE, ticker_names)
        tg("✅ *%s (%s)* 추가!\n종목 수: %d개 | /list" % (name, ticker, len(current_watchlist)), cid)

    elif cmd == "/remove" and arg:
        target = arg.upper() if arg.upper() in current_watchlist else next(
            (t for t in current_watchlist if ticker_names.get(t, "").replace(" ", "") == arg.replace(" ", "")), None)
        if target:
            current_watchlist.remove(target)
            save_json(WATCHLIST_FILE, current_watchlist)
            tg("🗑 *%s* 삭제!\n남은 종목: %d개" % (get_display(target), len(current_watchlist)), cid)
        else:
            tg("⚠️ *%s* 는 목록에 없어요. /list 확인" % arg, cid)

    elif cmd == "/list":
        if not current_watchlist:
            tg("📋 종목 없음\n`/add 종목명` 으로 추가!", cid)
        else:
            items = "\n".join(["  • %s" % get_display(t) for t in current_watchlist])
            tg("📋 *모니터링 종목* (%d개)\n━━━━━━━━━━━━━━\n%s" % (len(current_watchlist), items), cid)

    elif cmd == "/check" and arg:
        tg("⏳ *%s* 확인 중..." % arg, cid)
        res = find_ticker(arg)
        if not res: tg("❌ *%s* 를 찾을 수 없어요." % arg, cid); return
        ticker, name = res
        df = fetch(ticker)
        if df is None: tg("❌ `%s` 데이터 없음" % ticker, cid); return
        df  = calc_rsi(calc_stoch(df, STOCH_K_PERIOD, STOCH_D_PERIOD), RSI_PERIOD)
        l   = df.iloc[-1]
        pr  = "%s원" % "{:,.0f}".format(float(l["Close"])) if is_korean(ticker) else "%.2f" % float(l["Close"])
        sig = "🔴 *매수 신호!*" if buy_signal(df) else "⚪ 신호 없음"
        tg("📊 *%s (%s)*\n━━━━━━━━━━━━━━\n💰 `%s`\n📊 %%K: `%.1f` (≤%d)\n📉 RSI: `%.1f` (≤%d)\n━━━━━━━━━━━━━━\n%s"
           % (name, ticker, pr, float(l["%K"]), STOCH_OVERSOLD, float(l["RSI"]), RSI_OVERSOLD, sig), cid)

    elif cmd == "/status":
        if not current_watchlist: tg("📋 종목 없음", cid); return
        tg("⏳ %d개 확인 중..." % len(current_watchlist), cid)
        lines = []
        for t in current_watchlist:
            df = fetch(t)
            if df is None: lines.append("• %s: 데이터 없음" % get_display(t)); continue
            df = calc_rsi(calc_stoch(df, STOCH_K_PERIOD, STOCH_D_PERIOD), RSI_PERIOD)
            l  = df.iloc[-1]
            lines.append("%s %s | %%K:%.1f RSI:%.1f" % ("🔴" if buy_signal(df) else "⚪", get_display(t), float(l["%K"]), float(l["RSI"])))
        tg("📊 *전체 현황*\n━━━━━━━━━━━━━━\n%s\n━━━━━━━━━━━━━━\n🔴 매수 | ⚪ 대기" % "\n".join(lines), cid)

    elif cmd == "/help":
        tg("📖 *명령어*\n━━━━━━━━━━━━━━\n"
           "➕ `/add 삼성전자` - 한국주식\n"
           "➕ `/add AAPL` - 미국주식\n"
           "➕ `/add BTC-USD` - 암호화폐\n"
           "➖ `/remove 삼성전자`\n"
           "📋 `/list`\n🔍 `/check 삼성전자`\n📊 `/status`", cid)
    else:
        tg("❓ 알 수 없는 명령어\n/help 확인", cid)

# ── 봇 리스너 ──
def listener():
    log.info("📨 수신 시작")
    offset = None
    while True:
        try:
            for u in get_updates(offset):
                offset = u["update_id"] + 1
                msg    = u.get("message", {})
                txt    = msg.get("text", "")
                cid    = str(msg.get("chat", {}).get("id", ""))
                if txt.startswith("/"): handle(txt, cid)
        except Exception as e:
            log.error("리스너 오류: %s" % str(e)); time.sleep(5)

# ── 알람 루프 ──
def alert_loop():
    alerted = {}
    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        for ticker in list(current_watchlist):
            if alerted.get(ticker) == today: continue
            df = fetch(ticker)
            if df is None: continue
            df = calc_rsi(calc_stoch(df, STOCH_K_PERIOD, STOCH_D_PERIOD), RSI_PERIOD)
            l  = df.iloc[-1]
            log.info("[%s] %%K=%.1f RSI=%.1f" % (get_display(ticker), float(l["%K"]), float(l["RSI"])))
            if buy_signal(df):
                pr  = "%s원" % "{:,.0f}".format(float(l["Close"])) if is_korean(ticker) else "%.2f" % float(l["Close"])
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                tg("🔔 *매수 신호!*\n━━━━━━━━━━━━━━\n📌 *%s*\n💰 `%s`\n📊 %%K:`%.1f` 📉 RSI:`%.1f`\n🕐 %s\n━━━━━━━━━━━━━━\n⚠️ 투자 판단은 본인 책임"
                   % (get_display(ticker), pr, float(l["%K"]), float(l["RSI"]), now))
                alerted[ticker] = today
        alerted = {k: v for k, v in alerted.items() if v == today}
        log.info("⏳ %d분 대기" % CHECK_INTERVAL_MINUTES)
        time.sleep(CHECK_INTERVAL_MINUTES * 60)

def run():
    threading.Thread(target=build_krx_map, daemon=True).start()
    tg("🚀 *v5 시작!* 종목: %d개\n한국주식 이름 검색 가능!\n`/add 삼성전자` `/add AAPL`\n/help" % len(current_watchlist))
    threading.Thread(target=listener, daemon=True).start()
    alert_loop()

if __name__ == "__main__":
    run()
