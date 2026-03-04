"""
📈 매수 알람 시스템
- 스토캐스틱 패스트 + RSI 과매도 구간 동시 진입 시 텔레그램 알림
- 한국 주식(KRX), 미국 주식(NYSE/NASDAQ), 암호화폐 지원
"""

import time
import asyncio
import logging
from datetime import datetime
import pandas as pd
import numpy as np
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
# 지표 계산
# ─────────────────────────────────────────

def calc_stochastic_fast(df: pd.DataFrame, k_period: int, d_period: int):
    """스토캐스틱 패스트 %K, %D 계산"""
    low_min  = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    df = df.copy()
    df["%K"] = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["%D"] = df["%K"].rolling(d_period).mean()
    return df


def calc_rsi(df: pd.DataFrame, period: int):
    """RSI 계산"""
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    df    = df.copy()
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def is_buy_signal(df: pd.DataFrame) -> bool:
    """
    매수 신호 조건 (AND):
      1) 스토캐스틱 %K <= STOCH_OVERSOLD (25)
      2) RSI <= RSI_OVERSOLD (30)
    """
    latest = df.iloc[-1]
    stoch_cond = latest["%K"] <= STOCH_OVERSOLD
    rsi_cond   = latest["RSI"] <= RSI_OVERSOLD
    return bool(stoch_cond and rsi_cond)


# ─────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────

def fetch_data(ticker: str) -> pd.DataFrame | None:
    """yfinance로 최근 60일 일봉 데이터 수집"""
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            log.warning(f"[{ticker}] 데이터 부족")
            return None
        df.columns = df.columns.get_level_values(0)  # 멀티인덱스 제거
        return df
    except Exception as e:
        log.error(f"[{ticker}] 데이터 수집 실패: {e}")
        return None


# ─────────────────────────────────────────
# 텔레그램 알림
# ─────────────────────────────────────────

def send_telegram(message: str):
    """텔레그램 봇으로 메시지 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("✅ 텔레그램 전송 성공")
        else:
            log.error(f"텔레그램 오류: {resp.text}")
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")


def build_alert_message(ticker: str, df: pd.DataFrame) -> str:
    latest = df.iloc[-1]
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
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
# 메인 루프
# ─────────────────────────────────────────

def run():
    log.info("🚀 매수 알람 시스템 시작")
    log.info(f"모니터링 종목: {WATCHLIST}")
    send_telegram("🚀 매수 알람 시스템이 시작되었습니다!\n모니터링 종목: " + ", ".join(WATCHLIST))

    # 중복 알람 방지: 같은 날 같은 종목 재발송 차단
    alerted_today: dict[str, str] = {}

    while True:
        today = datetime.now().strftime("%Y-%m-%d")

        for ticker in WATCHLIST:
            # 오늘 이미 알람 보낸 종목은 스킵
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
                msg = build_alert_message(ticker, df)
                send_telegram(msg)
                alerted_today[ticker] = today
                log.info(f"🔔 [{ticker}] 매수 신호 알람 전송!")

        # 날짜 바뀌면 알람 기록 초기화
        if any(v != today for v in alerted_today.values()):
            alerted_today = {k: v for k, v in alerted_today.items() if v == today}

        log.info(f"⏳ 다음 체크까지 {CHECK_INTERVAL_MINUTES}분 대기...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
