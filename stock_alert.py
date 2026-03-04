"""
📈 매수 알람 시스템 v4
- 한국 주식: pykrx (해외 서버에서도 작동, 원화 기준 정확)
- 미국/암호화폐: yfinance
- 종목명 검색: pykrx 종목 목록에서 한글 검색
"""

import time, threading, logging, json, os
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
import requests
from pykrx import stock as krx
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
    return f"{name} ({ticker})" if name else ticker

current_watchlist = load_json(WATCHLIST_FILE, list(WATCHLIST))
ticker_names      = load_json(NAMES_FILE, {})
krx_name_map      = {}

def build_krx_map():
    global krx_name_map
    cached = load_json(KRX_MAP_FILE, {})
    if cached:
        krx_name_map = cached
        log.info(f"KRX 종목 맵 로드: {len(krx_name_map)}개")
        return
    log.info("KRX 종목 맵 생성 중...")
    try:
        today = datetime.now().strftime("%Y%m%d")
        for market in ["KOSPI", "KOSDAQ"]:
            for code in krx.get_market_ticker_list(today, market=market):
                try:
                    name = krx.get_market_ticker_name(code)
                    krx_name_map[name] = code
                except: pass
        save_json(KRX_MAP_FILE, krx_name_map)
        log.info(f"KRX 맵 완료: {len(krx_name_map)}개")
    except Exception as e:
        log.error(f"KRX 맵 오류: {e}")

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
        t = q + ".KS"
        try: name = krx.get_market_ticker_name(q)
        except: name = t
        return (t, name)
    if qu.replace("-","").isalpha():
        return (qu, qu)
    return search_krx(q)

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

def fetch_krx(ticker):
    try:
        code  = ticker.split(".")[0]
        end   = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        df    = krx.get_market_ohlcv(start, end, code)
        if df is None or len(df) < 20: return None
        df = df.rename(columns={"시가":"Open","고가":"High","저가":"Low","종가":"Close","거래량":"Volume"})
        df.index = pd.to_datetime(df.index)
        log.info(f"[{ticker}] pykrx 수집 ({len(df)}일) 최근가: {df['Close'].iloc[-1]:,.0f}원")
        return df[["Open","High","Low","Close","Volume"]]
    except Exception as e:
        log.error(f"[{ticker}] pykrx 오류: {e}")
        return None

def fetch_yf(ticker):
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20: return None
        df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.error(f"[{ticker}] yfinance 오류: {e}")
        return None

def fetch(ticker):
    return fetch_krx(ticker) if is_korean(ticker) else fetch_yf(ticker)

def tg(msg, cid=None):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cid or TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"TG 오류: {e}")

def get_updates(offset=None):
    try:
        params = {"timeout": 30}
        if offset: params["offset"] = offset
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=35)
        if r.status_code == 200: return r.json().get("result", [])
    except: pass
    return []

def handle(text, cid):
    global current_watchlist, ticker_names
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/add" and arg:
        tg(f"⏳ *{arg}* 검색 중...", cid)
        res = find_ticker(arg)
        if not res:
            tg(f"❌ *{arg}* 를 찾을 수 없어요.\n예: `/add 삼성전자` `/add AAPL` `/add BTC-USD`", cid)
            return
        ticker, name = res
        if ticker in current_watchlist:
            tg(f"⚠️ *{get_display(ticker)}* 는 이미 모니터링 중!", cid); return
        if fetch(ticker) is None:
            tg(f"❌ `{ticker}` 데이터를 가져올 수 없어요.", cid); return
        current_watchlist.append(ticker)
        ticker_names[ticker] = name
        save_json(WATCHLIST_FILE, current_watchlist)
        save_json(NAMES_FILE, ticker_names)
        tg(f"✅ *{name} ({ticker})* 추가!\n종목 수: {len(current_watchlist)}개 | /list", cid)

    elif cmd == "/remove" and arg:
        target = arg.upper() if arg.upper() in current_watchlist else next(
            (t for t in current_watchlist if ticker_names.get(t,"").replace(" ","") == arg.replace(" ","")), None)
        if target:
            current_watchlist.remove(target)
            save_json(WATCHLIST_FILE, current_watchlist)
            tg(f"🗑 *{get_display(target)}* 삭제!\n남은 종목: {len(current_watchlist)}개", cid)
        else:
            tg(f"⚠️ *{arg}* 는 목록에 없어요. /list 확인", cid)

    elif cmd == "/list":
        if not current_watchlist:
            tg("📋 종목 없음\n`/add 종목명` 으로 추가!", cid)
        else:
            items = "\n".join([f"  • {get_display(t)}" for t in current_watchlist])
            tg(f"📋 *모니터링 종목* ({len(current_watchlist)}개)\n━━━━━━━━━━━━━━\n{items}", cid)

    elif cmd == "/check" and arg:
        tg(f"⏳ *{arg}* 확인 중...", cid)
        res = find_ticker(arg)
        if not res: tg(f"❌ *{arg}* 를 찾을 수 없어요.", cid); return
        ticker, name = res
        df = fetch(ticker)
        if df is None: tg(f"❌ `{ticker}` 데이터 없음", cid); return
        df = calc_rsi(calc_stoch(df, STOCH_K_PERIOD, STOCH_D_PERIOD), RSI_PERIOD)
        l  = df.iloc[-1]
        pr = f"{l['Close']:,.0f}원" if is_korean(ticker) else f"{l['Close']:.2f}"
        sig = "🔴 *매수 신호!*" if buy_signal(df) else "⚪ 신호 없음"
        tg(f"📊 *{name} ({ticker})*\n━━━━━━━━━━━━━━\n"
           f"💰 `{pr}`\n📊 %K: `{l['%K']:.1f}` (≤{STOCH_OVERSOLD})\n"
           f"📉 RSI: `{l['RSI']:.1f}` (≤{RSI_OVERSOLD})\n━━━━━━━━━━━━━━\n{sig}", cid)

    elif cmd == "/status":
        if not current_watchlist: tg("📋 종목 없음", cid); return
        tg(f"⏳ {len(current_watchlist)}개 확인 중...", cid)
        lines = []
        for t in current_watchlist:
            df = fetch(t)
            if df is None: lines.append(f"• {get_display(t)}: 데이터 없음"); continue
            df = calc_rsi(calc_stoch(df, STOCH_K_PERIOD, STOCH_D_PERIOD), RSI_PERIOD)
            l  = df.iloc[-1]
            lines.append(f"{'🔴' if buy_signal(df) else '⚪'} {get_display(t)} | %K:{l['%K']:.1f} RSI:{l['RSI']:.1f}")
        tg(f"📊 *전체 현황*\n━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n━━━━━━━━━━━━━━\n🔴 매수 | ⚪ 대기", cid)

    elif cmd == "/help":
        tg("📖 *명령어*\n━━━━━━━━━━━━━━\n"
           "➕ `/add 삼성전자` - 한국주식\n"
           "➕ `/add AAPL` - 미국주식\n"
           "➕ `/add BTC-USD` - 암호화폐\n"
           "➖ `/remove 삼성전자`\n"
           "📋 `/list`\n🔍 `/check 삼성전자`\n📊 `/status`", cid)
    else:
        tg("❓ 알 수 없는 명령어\n/help 확인", cid)

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
            log.error(f"리스너 오류: {e}"); time.sleep(5)

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
            log.info(f"[{get_display(ticker)}] %K={l['%K']:.1f} RSI={l['RSI']:.1f}")
            if buy_signal(df):
                pr = f"{l['Close']:,.0f}원" if is_korean(ticker) else f"{l['Close']:.2f}"
                tg(f"🔔 *매수 신호!*\n━━━━━━━━━━━━━━\n📌 *{get_display(ticker)}*\n"
                   f"💰 `{pr}`\n📊 %K:`{l['%K']:.1f}` 📉 RSI:`{l['RSI']:.1f}`\n"
                   f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n━━━━━━━━━━━━━━\n⚠️ 투자 판단은 본인 책임")
                alerted[ticker] = today
        alerted = {k: v for k, v in alerted.items() if v == today}
        log.info(f"⏳ {CHECK_INTERVAL_MINUTES}분 대기")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)

def run():
    threading.Thread(target=build_krx_map, daemon=True).start()
    tg(f"🚀 *v4 시작!* 종목: {len(current_watchlist)}개\n"
       "한국주식 원화 정확!\n`/add 삼성전자` `/add AAPL`\n/help")
    threading.Thread(target=listener, daemon=True).start()
    alert_loop()

if __name__ == "__main__":
    run()
