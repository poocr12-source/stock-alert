"""
📈 매수 알람 시스템 v3 (종목 이름 검색 지원)
- 스토캐스틱 패스트 + RSI 과매도 구간 동시 진입 시 텔레그램 알림
- 한국 주식은 이름으로 검색 가능 (/add 삼성전자)
- 한국 주식 가격은 네이버 금융 기준 (정확한 원화)

명령어:
  /add 삼성전자       - 이름으로 한국 주식 추가
  /add AAPL          - 코드로 미국 주식 추가
  /add BTC-USD       - 암호화폐 추가
  /remove 삼성전자    - 이름 또는 코드로 삭제
  /list              - 종목 목록
  /check 삼성전자     - 즉시 지표 확인
  /status            - 전체 종목 현황
  /help              - 도움말
"""

import time
import threading
import logging
import json
import os
from datetime import datetime
import pandas as pd
import yfinance as yf
import requests
from io import StringIO
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WATCHLIST,
    STOCH_K_PERIOD,
    STOCH_D_PERIOD,
    STOCH_OVERSOLD,
    RSI_PERIOD,
    RSI_OVERSOLD,
    CHECK_INTERVAL_MINUTES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alert.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 종목 리스트 & 이름 관리
# ─────────────────────────────────────────

WATCHLIST_FILE = "watchlist.json"
NAMES_FILE     = "ticker_names.json"

def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return list(WATCHLIST)

def save_watchlist(wl: list):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False)

def load_names() -> dict:
    if os.path.exists(NAMES_FILE):
        try:
            with open(NAMES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_names(names: dict):
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False)

def get_display(ticker: str) -> str:
    name = ticker_names.get(ticker)
    return f"{name} ({ticker})" if name else ticker

current_watchlist = load_watchlist()
ticker_names      = load_names()


# ─────────────────────────────────────────
# 네이버 종목명 검색
# ─────────────────────────────────────────

def search_naver(query: str) -> tuple | None:
    try:
        url = "https://ac.finance.naver.com/ac"
        params = {
            "q": query, "q_enc": "UTF-8", "st": "111",
            "sug": "false", "mdl": "stock", "callback": ""
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        resp.encoding = "UTF-8"
        text = resp.text.strip().lstrip("(").rstrip(")")
        if not text:
            return None
        data  = json.loads(text)
        items = data.get("items", [[]])
        if items and items[0]:
            item   = items[0][0]
            name   = item[0]
            code   = item[1]
            market = item[2] if len(item) > 2 else "1"
            suffix = ".KQ" if market == "2" else ".KS"
            return (code + suffix, name)
    except Exception as e:
        log.error(f"네이버 검색 실패: {e}")
    return None

def is_korean_stock(ticker: str) -> bool:
    return ticker.upper().endswith(".KS") or ticker.upper().endswith(".KQ")

def find_ticker(query: str) -> tuple | None:
    """입력값 → (ticker, name). 한글이면 네이버 검색, 영문이면 그대로"""
    q = query.strip()
    qu = q.upper()
    # 한국 코드 (.KS/.KQ)
    if qu.endswith(".KS") or qu.endswith(".KQ"):
        return (qu, ticker_names.get(qu, qu))
    # 숫자 6자리 → 한국 코드
    if q.isdigit() and len(q) == 6:
        ticker = qu + ".KS"
        return (ticker, ticker_names.get(ticker, ticker))
    # 영문/숫자 → 미국 주식 or 암호화폐
    if qu.replace("-", "").isalpha() or (qu.replace("-", "").isalnum() and qu[0].isalpha()):
        return (qu, qu)
    # 한글 포함 → 네이버 검색
    return search_naver(q)


# ─────────────────────────────────────────
# 지표 계산
# ─────────────────────────────────────────

def calc_stochastic_fast(df, k_period, d_period):
    low_min  = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    df = df.copy()
    df["%K"] = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["%D"] = df["%K"].rolling(d_period).mean()
    return df

def calc_rsi(df, period):
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    df    = df.copy()
    df["RSI"] = 100 - (100 / (1 + rs))
    return df

def is_buy_signal(df) -> bool:
    latest = df.iloc[-1]
    return bool(latest["%K"] <= STOCH_OVERSOLD and latest["RSI"] <= RSI_OVERSOLD)


# ─────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────

def fetch_naver(ticker: str):
    try:
        code    = ticker.split(".")[0]
        headers = {"User-Agent": "Mozilla/5.0"}
        frames  = []
        for page in range(1, 4):
            r = requests.get(
                f"https://finance.naver.com/item/siseday.naver?code={code}&page={page}",
                headers=headers, timeout=10
            )
            r.encoding = "euc-kr"
            t = pd.read_html(StringIO(r.text))[0].dropna()
            t.columns = ["Date", "Close", "Change", "Open", "High", "Low", "Volume"]
            t["Date"] = pd.to_datetime(t["Date"])
            for col in ["Close", "Open", "High", "Low", "Volume"]:
                t[col] = t[col].astype(str).str.replace(",", "").astype(float)
            frames.append(t)
        df = pd.concat(frames).sort_values("Date").drop_duplicates("Date").tail(60).reset_index(drop=True)
        return df if len(df) >= 20 else None
    except Exception as e:
        log.error(f"[{ticker}] 네이버 수집 실패: {e}")
        return None

def fetch_yfinance(ticker: str):
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.error(f"[{ticker}] yfinance 실패: {e}")
        return None

def fetch_data(ticker: str):
    return fetch_naver(ticker) if is_korean_stock(ticker) else fetch_yfinance(ticker)


# ─────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────

def send_telegram(message: str, chat_id: str = None):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"텔레그램 오류: {resp.text}")
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")

def get_updates(offset: int = None) -> list:
    url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=35)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except:
        pass
    return []


# ─────────────────────────────────────────
# 명령어 처리
# ─────────────────────────────────────────

def handle_command(text: str, chat_id: str):
    global current_watchlist, ticker_names
    parts = text.strip().split(maxsplit=1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    # /add
    if cmd == "/add" and arg:
        send_telegram(f"⏳ *{arg}* 검색 중...", chat_id)
        result = find_ticker(arg)
        if result is None:
            send_telegram(
                f"❌ *{arg}* 를 찾을 수 없어요.\n"
                f"종목명 또는 코드를 확인해주세요.\n"
                f"예: `삼성전자` / `KODEX200` / `AAPL` / `BTC-USD`",
                chat_id
            )
            return
        ticker, name = result
        if ticker in current_watchlist:
            send_telegram(f"⚠️ *{get_display(ticker)}* 는 이미 모니터링 중이에요!", chat_id)
            return
        if fetch_data(ticker) is None:
            send_telegram(f"❌ `{ticker}` 데이터를 가져올 수 없어요. 코드를 확인해주세요.", chat_id)
            return
        current_watchlist.append(ticker)
        ticker_names[ticker] = name
        save_watchlist(current_watchlist)
        save_names(ticker_names)
        send_telegram(
            f"✅ *{name} ({ticker})* 추가 완료!\n"
            f"현재 모니터링 종목: {len(current_watchlist)}개\n"
            f"목록 확인: /list",
            chat_id
        )

    # /remove
    elif cmd == "/remove" and arg:
        target    = None
        arg_upper = arg.strip().upper()
        if arg_upper in current_watchlist:
            target = arg_upper
        else:
            for t in current_watchlist:
                if ticker_names.get(t, "").replace(" ", "") == arg.replace(" ", ""):
                    target = t
                    break
        if target:
            current_watchlist.remove(target)
            save_watchlist(current_watchlist)
            send_telegram(
                f"🗑 *{get_display(target)}* 삭제 완료!\n"
                f"남은 모니터링 종목: {len(current_watchlist)}개",
                chat_id
            )
        else:
            send_telegram(f"⚠️ *{arg}* 는 목록에 없어요.\n/list 로 현재 종목을 확인해보세요.", chat_id)

    # /list
    elif cmd == "/list":
        if not current_watchlist:
            send_telegram("📋 모니터링 종목이 없어요.\n`/add 종목명` 으로 추가해보세요!", chat_id)
        else:
            items = "\n".join([f"  • {get_display(t)}" for t in current_watchlist])
            send_telegram(
                f"📋 *현재 모니터링 종목* ({len(current_watchlist)}개)\n"
                f"━━━━━━━━━━━━━━\n{items}",
                chat_id
            )

    # /check
    elif cmd == "/check" and arg:
        send_telegram(f"⏳ *{arg}* 지표 확인 중...", chat_id)
        result = find_ticker(arg)
        if result is None:
            send_telegram(f"❌ *{arg}* 를 찾을 수 없어요.", chat_id)
            return
        ticker, name = result
        df = fetch_data(ticker)
        if df is None:
            send_telegram(f"❌ `{ticker}` 데이터를 가져올 수 없어요.", chat_id)
            return
        df        = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
        df        = calc_rsi(df, RSI_PERIOD)
        latest    = df.iloc[-1]
        signal    = "🔴 *매수 신호!*" if is_buy_signal(df) else "⚪ 신호 없음"
        price_str = f"{latest['Close']:,.0f}원" if is_korean_stock(ticker) else f"{latest['Close']:.2f}"
        send_telegram(
            f"📊 *{name} ({ticker})* 현재 지표\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 현재가: `{price_str}`\n"
            f"📊 Stoch %K: `{latest['%K']:.1f}` (기준 ≤ {STOCH_OVERSOLD})\n"
            f"📉 RSI({RSI_PERIOD}): `{latest['RSI']:.1f}` (기준 ≤ {RSI_OVERSOLD})\n"
            f"━━━━━━━━━━━━━━\n{signal}",
            chat_id
        )

    # /status
    elif cmd == "/status":
        if not current_watchlist:
            send_telegram("📋 모니터링 종목이 없어요.", chat_id)
            return
        send_telegram(f"⏳ 전체 {len(current_watchlist)}개 종목 확인 중...", chat_id)
        lines = []
        for ticker in current_watchlist:
            df = fetch_data(ticker)
            if df is None:
                lines.append(f"• {get_display(ticker)}: 데이터 없음")
                continue
            df     = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
            df     = calc_rsi(df, RSI_PERIOD)
            latest = df.iloc[-1]
            flag   = "🔴" if is_buy_signal(df) else "⚪"
            lines.append(f"{flag} {get_display(ticker)} | %K:{latest['%K']:.1f} | RSI:{latest['RSI']:.1f}")
        send_telegram(
            f"📊 *전체 종목 현황*\n━━━━━━━━━━━━━━\n" +
            "\n".join(lines) +
            "\n━━━━━━━━━━━━━━\n🔴 매수신호 | ⚪ 대기중",
            chat_id
        )

    # /help
    elif cmd == "/help":
        send_telegram(
            "📖 *사용 가능한 명령어*\n"
            "━━━━━━━━━━━━━━\n"
            "➕ `/add 종목명 또는 코드`\n"
            "    🇰🇷 `/add 삼성전자`\n"
            "    🇰🇷 `/add KODEX200`\n"
            "    🇺🇸 `/add AAPL`\n"
            "    ₿ `/add BTC-USD`\n\n"
            "➖ `/remove 종목명`\n"
            "    예: `/remove 삼성전자`\n\n"
            "📋 `/list` - 종목 목록\n\n"
            "🔍 `/check 종목명` - 즉시 지표 확인\n"
            "    예: `/check 삼성전자`\n\n"
            "📊 `/status` - 전체 종목 현황",
            chat_id
        )

    else:
        send_telegram("❓ 알 수 없는 명령어예요.\n/help 로 명령어 목록을 확인해보세요!", chat_id)


# ─────────────────────────────────────────
# 봇 리스너
# ─────────────────────────────────────────

def bot_listener():
    log.info("📨 텔레그램 명령어 수신 시작")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset  = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    log.info(f"명령어: {text}")
                    handle_command(text, chat_id)
        except Exception as e:
            log.error(f"봇 리스너 오류: {e}")
            time.sleep(5)


# ─────────────────────────────────────────
# 알람 & 메인 루프
# ─────────────────────────────────────────

def build_alert_message(ticker: str, df) -> str:
    latest    = df.iloc[-1]
    now       = datetime.now().strftime("%Y-%m-%d %H:%M")
    price_str = f"{latest['Close']:,.0f}원" if is_korean_stock(ticker) else f"{latest['Close']:.2f}"
    return (
        f"🔔 *매수 신호 감지!*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 종목: *{get_display(ticker)}*\n"
        f"💰 현재가: `{price_str}`\n"
        f"📊 Stoch %K: `{latest['%K']:.1f}` (기준 ≤ {STOCH_OVERSOLD})\n"
        f"📉 RSI({RSI_PERIOD}): `{latest['RSI']:.1f}` (기준 ≤ {RSI_OVERSOLD})\n"
        f"🕐 시각: {now}\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ 투자 판단은 본인 책임입니다."
    )

def alert_loop():
    alerted_today: dict = {}
    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        for ticker in list(current_watchlist):
            if alerted_today.get(ticker) == today:
                continue
            df = fetch_data(ticker)
            if df is None:
                continue
            df     = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
            df     = calc_rsi(df, RSI_PERIOD)
            latest = df.iloc[-1]
            log.info(f"[{get_display(ticker)}] %K={latest['%K']:.1f} | RSI={latest['RSI']:.1f}")
            if is_buy_signal(df):
                send_telegram(build_alert_message(ticker, df))
                alerted_today[ticker] = today
        alerted_today = {k: v for k, v in alerted_today.items() if v == today}
        log.info(f"⏳ {CHECK_INTERVAL_MINUTES}분 후 재체크")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)

def run():
    log.info("🚀 매수 알람 시스템 v3 시작")
    send_telegram(
        "🚀 *매수 알람 시스템 v3 시작!*\n"
        f"모니터링 종목: {len(current_watchlist)}개\n"
        "━━━━━━━━━━━━━━\n"
        "이제 종목명으로 바로 추가 가능!\n"
        "예: `/add 삼성전자`  `/add AAPL`\n"
        "/help 로 전체 명령어 확인"
    )
    threading.Thread(target=bot_listener, daemon=True).start()
    alert_loop()

if __name__ == "__main__":
    run()
