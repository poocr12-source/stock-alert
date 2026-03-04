"""
📈 매수 알람 시스템 v2 (양방향 봇)
- 스토캐스틱 패스트 + RSI 과매도 구간 동시 진입 시 텔레그램 알림
- 텔레그램에서 직접 종목 추가/삭제/조회 가능
- 한국 주식(KRX), 미국 주식(NYSE/NASDAQ), 암호화폐 지원

사용 가능한 명령어:
  /add 종목코드     - 종목 추가 (예: /add 005930.KS)
  /remove 종목코드  - 종목 삭제 (예: /remove AAPL)
  /list            - 현재 모니터링 종목 목록
  /check 종목코드   - 특정 종목 즉시 지표 확인
  /status          - 전체 종목 현재 지표 확인
  /help            - 명령어 도움말
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

# 로그 설정
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
# 종목 리스트 관리 (파일로 영구 저장)
# ─────────────────────────────────────────

WATCHLIST_FILE = "watchlist.json"

def load_watchlist() -> list:
    """저장된 종목 리스트 불러오기"""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return list(WATCHLIST)  # 기본값은 config.py의 WATCHLIST

def save_watchlist(watchlist: list):
    """종목 리스트 파일에 저장"""
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f)

# 전역 종목 리스트
current_watchlist = load_watchlist()


# ─────────────────────────────────────────
# 지표 계산
# ─────────────────────────────────────────

def calc_stochastic_fast(df: pd.DataFrame, k_period: int, d_period: int):
    low_min  = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    df = df.copy()
    df["%K"] = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["%D"] = df["%K"].rolling(d_period).mean()
    return df

def calc_rsi(df: pd.DataFrame, period: int):
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    df    = df.copy()
    df["RSI"] = 100 - (100 / (1 + rs))
    return df

def is_buy_signal(df: pd.DataFrame) -> bool:
    latest = df.iloc[-1]
    return bool(latest["%K"] <= STOCH_OVERSOLD and latest["RSI"] <= RSI_OVERSOLD)


# ─────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────

def fetch_data(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.error(f"[{ticker}] 데이터 수집 실패: {e}")
        return None

def validate_ticker(ticker: str) -> bool:
    """종목 코드가 유효한지 확인"""
    df = fetch_data(ticker)
    return df is not None


# ─────────────────────────────────────────
# 텔레그램 송수신
# ─────────────────────────────────────────

def send_telegram(message: str, chat_id: str = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"텔레그램 오류: {resp.text}")
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")

def get_updates(offset: int = None) -> list:
    """텔레그램 메시지 수신"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
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
    global current_watchlist
    text = text.strip()
    parts = text.split()
    cmd = parts[0].lower()

    # /add 종목코드
    if cmd == "/add" and len(parts) >= 2:
        ticker = parts[1].upper()
        if ticker in current_watchlist:
            send_telegram(f"⚠️ `{ticker}` 는 이미 모니터링 중이에요!", chat_id)
            return
        send_telegram(f"⏳ `{ticker}` 확인 중...", chat_id)
        if validate_ticker(ticker):
            current_watchlist.append(ticker)
            save_watchlist(current_watchlist)
            send_telegram(
                f"✅ *{ticker}* 추가 완료!\n"
                f"현재 모니터링 종목: {len(current_watchlist)}개\n"
                f"목록 확인: /list", chat_id
            )
        else:
            send_telegram(
                f"❌ `{ticker}` 를 찾을 수 없어요.\n"
                f"종목코드를 확인해주세요.\n"
                f"예시: 005930.KS / AAPL / BTC-USD", chat_id
            )

    # /remove 종목코드
    elif cmd == "/remove" and len(parts) >= 2:
        ticker = parts[1].upper()
        if ticker in current_watchlist:
            current_watchlist.remove(ticker)
            save_watchlist(current_watchlist)
            send_telegram(
                f"🗑 *{ticker}* 삭제 완료!\n"
                f"남은 모니터링 종목: {len(current_watchlist)}개", chat_id
            )
        else:
            send_telegram(f"⚠️ `{ticker}` 는 목록에 없어요.", chat_id)

    # /list
    elif cmd == "/list":
        if not current_watchlist:
            send_telegram("📋 모니터링 종목이 없어요.\n/add 종목코드 로 추가해보세요!", chat_id)
        else:
            items = "\n".join([f"  • `{t}`" for t in current_watchlist])
            send_telegram(
                f"📋 *현재 모니터링 종목* ({len(current_watchlist)}개)\n"
                f"━━━━━━━━━━━━━━\n"
                f"{items}", chat_id
            )

    # /check 종목코드
    elif cmd == "/check" and len(parts) >= 2:
        ticker = parts[1].upper()
        send_telegram(f"⏳ `{ticker}` 지표 확인 중...", chat_id)
        df = fetch_data(ticker)
        if df is None:
            send_telegram(f"❌ `{ticker}` 데이터를 가져올 수 없어요.", chat_id)
            return
        df = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
        df = calc_rsi(df, RSI_PERIOD)
        latest = df.iloc[-1]
        signal = "🔴 *매수 신호!*" if is_buy_signal(df) else "⚪ 신호 없음"
        send_telegram(
            f"📊 *{ticker}* 현재 지표\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 현재가: `{latest['Close']:.2f}`\n"
            f"📊 Stoch %K: `{latest['%K']:.1f}` (기준 ≤ {STOCH_OVERSOLD})\n"
            f"📉 RSI({RSI_PERIOD}): `{latest['RSI']:.1f}` (기준 ≤ {RSI_OVERSOLD})\n"
            f"━━━━━━━━━━━━━━\n"
            f"{signal}", chat_id
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
                lines.append(f"• `{ticker}`: 데이터 없음")
                continue
            df = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
            df = calc_rsi(df, RSI_PERIOD)
            latest = df.iloc[-1]
            flag = "🔴" if is_buy_signal(df) else "⚪"
            lines.append(
                f"{flag} `{ticker}` | %K: {latest['%K']:.1f} | RSI: {latest['RSI']:.1f}"
            )
        send_telegram(
            f"📊 *전체 종목 현황*\n"
            f"━━━━━━━━━━━━━━\n" +
            "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━\n🔴 매수신호 | ⚪ 대기중",
            chat_id
        )

    # /help
    elif cmd == "/help":
        send_telegram(
            "📖 *사용 가능한 명령어*\n"
            "━━━━━━━━━━━━━━\n"
            "➕ `/add 종목코드` - 종목 추가\n"
            "    예: `/add 005930.KS`\n"
            "    예: `/add AAPL`\n"
            "    예: `/add BTC-USD`\n\n"
            "➖ `/remove 종목코드` - 종목 삭제\n\n"
            "📋 `/list` - 종목 목록 보기\n\n"
            "🔍 `/check 종목코드` - 즉시 지표 확인\n\n"
            "📊 `/status` - 전체 종목 현황\n\n"
            "━━━━━━━━━━━━━━\n"
            "🇰🇷 한국주식: `삼성전자` → `005930.KS`\n"
            "🇺🇸 미국주식: `AAPL`, `TSLA`, `NVDA`\n"
            "₿ 암호화폐: `BTC-USD`, `ETH-USD`",
            chat_id
        )

    else:
        send_telegram(
            "❓ 알 수 없는 명령어예요.\n/help 로 명령어 목록을 확인해보세요!", chat_id
        )


# ─────────────────────────────────────────
# 텔레그램 명령어 수신 루프 (별도 스레드)
# ─────────────────────────────────────────

def bot_listener():
    """텔레그램 메시지 수신 및 명령어 처리"""
    log.info("📨 텔레그램 명령어 수신 시작")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    log.info(f"명령어 수신: {text} (from {chat_id})")
                    handle_command(text, chat_id)
        except Exception as e:
            log.error(f"봇 리스너 오류: {e}")
            time.sleep(5)


# ─────────────────────────────────────────
# 알람 메시지
# ─────────────────────────────────────────

def build_alert_message(ticker: str, df: pd.DataFrame) -> str:
    latest = df.iloc[-1]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"🔔 *매수 신호 감지!*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 종목: `{ticker}`\n"
        f"💰 현재가: `{latest['Close']:.2f}`\n"
        f"📊 Stoch %K: `{latest['%K']:.1f}` (기준 ≤ {STOCH_OVERSOLD})\n"
        f"📉 RSI({RSI_PERIOD}): `{latest['RSI']:.1f}` (기준 ≤ {RSI_OVERSOLD})\n"
        f"🕐 시각: {now}\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ 투자 판단은 본인 책임입니다."
    )


# ─────────────────────────────────────────
# 메인 알람 루프
# ─────────────────────────────────────────

def alert_loop():
    alerted_today: dict[str, str] = {}
    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        for ticker in list(current_watchlist):
            if alerted_today.get(ticker) == today:
                continue
            df = fetch_data(ticker)
            if df is None:
                continue
            df = calc_stochastic_fast(df, STOCH_K_PERIOD, STOCH_D_PERIOD)
            df = calc_rsi(df, RSI_PERIOD)
            latest = df.iloc[-1]
            log.info(
                f"[{ticker}] 현재가={latest['Close']:.2f} | "
                f"%K={latest['%K']:.1f} | RSI={latest['RSI']:.1f}"
            )
            if is_buy_signal(df):
                send_telegram(build_alert_message(ticker, df))
                alerted_today[ticker] = today
                log.info(f"🔔 [{ticker}] 매수 신호 알람 전송!")

        alerted_today = {k: v for k, v in alerted_today.items() if v == today}
        log.info(f"⏳ 다음 체크까지 {CHECK_INTERVAL_MINUTES}분 대기...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────

def run():
    log.info("🚀 매수 알람 시스템 v2 시작")
    send_telegram(
        "🚀 *매수 알람 시스템 v2 시작!*\n"
        f"모니터링 종목: {len(current_watchlist)}개\n"
        "━━━━━━━━━━━━━━\n"
        "💬 /help 로 명령어 확인\n"
        "➕ /add 종목코드 로 종목 추가"
    )

    # 명령어 수신 스레드 (백그라운드)
    listener = threading.Thread(target=bot_listener, daemon=True)
    listener.start()

    # 메인 알람 루프
    alert_loop()


if __name__ == "__main__":
    run()
