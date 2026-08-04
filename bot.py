"""
OKX 무기한 선물(perpetual swap) 15분봉 캔들 신호 감시 봇
"""

import ccxt
import requests
import time
import datetime
import logging
import os
import json
import argparse

# ============================== CONFIG ==============================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "여기에_봇_토큰_입력")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "여기에_챗ID_입력")

TIMEFRAME = "15m"
TIMEFRAME_MS = 15 * 60 * 1000
LOOKBACK = 15
CLOSE_BUFFER_SEC = 12
STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")

RAW_SYMBOLS = [
    "BTC", "ETH", "ZEC", "MU", "BCH", "LINK", "BEAT", "SOL", "SOXL", "LAB",
    "NEAR", "XRP", "SUI", "ONDO", "WLD", "ALLO", "H", "OPN", "CRV", "DOGE",
    "BSB", "HOME", "SAHARA", "HMSTR", "TRUMP", "EDGE", "PEPE", "XPL", "SPACE",
    "COAI", "RE", "ADA", "O", "BASED", "HYPE", "SLX", "NES", "CAP", "LITU", "BNB",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("okx_candle_alert")

# ============================== TELEGRAM ==============================


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("텔레그램 전송 실패: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("텔레그램 전송 중 예외: %s", e)


# ============================== EXCHANGE ==============================


def build_exchange() -> ccxt.okx:
    exchange = ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    return exchange


def resolve_symbols(exchange: ccxt.okx, raw_symbols):
    exchange.load_markets()
    resolved = []
    missing = []
    for sym in raw_symbols:
        candidate = f"{sym}/USDT:USDT"
        if candidate in exchange.markets:
            resolved.append(candidate)
        else:
            missing.append(sym)
    if missing:
        log.warning(
            "OKX 무기한선물(USDT 마진)에서 찾지 못한 티커: %s",
            ", ".join(missing),
        )
    log.info("감시 대상 %d개 심볼 확정: %s", len(resolved), ", ".join(resolved))
    return resolved


# ============================== STATE (중복 알람 방지) ==============================


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning("상태 파일 로드 실패, 빈 상태로 시작: %s", e)
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("상태 파일 저장 실패: %s", e)


# ============================== CORE LOGIC ==============================


def fetch_closed_candles(exchange: ccxt.okx, symbol: str, count: int):
    now_ms = exchange.milliseconds()
    raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=count + 2)
    closed = [c for c in raw if c[0] + TIMEFRAME_MS <= now_ms]
    return closed[-count:]


def check_signal(candles):
    if len(candles) < LOOKBACK:
        return False, None

    window = candles[-LOOKBACK:]
    current = window[-1]
    _, o, h, l, c, v = current

    max_high = max(cd[2] for cd in window)
    is_new_high = h >= max_high

    is_bearish = c < o

    body = abs(c - o)
    upper_wick = h - max(o, c)
    is_inverted_hammer_shape = upper_wick >= body

    ok = is_new_high and is_bearish and is_inverted_hammer_shape

    high_close_diff_pct = ((h - c) / h * 100) if h != 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "upper_wick": upper_wick,
        "is_new_high": is_new_high,
        "high_close_diff_pct": high_close_diff_pct,
    }
    return ok, detail


def check_all_symbols(exchange: ccxt.okx, symbols, state: dict):
    for symbol in symbols:
        try:
            candles = fetch_closed_candles(exchange, symbol, LOOKBACK)
            if not candles:
                continue
            latest_candle_ts = candles[-1][0]

            if state.get(symbol) == latest_candle_ts:
                continue

            ok, detail = check_signal(candles)
            if ok:
                candle_time = datetime.datetime.fromtimestamp(
                    candles[-1][0] / 1000, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                msg = (
                    f"🔻 <b>{symbol}</b> 15분봉 신호 발생\n"
                    f"캔들 마감시각: {candle_time}\n"
                    f"시가: {detail['open']:.6g}\n"
                    f"고가: {detail['high']:.6g}\n"
                    f"저가: {detail['low']:.6g}\n"
                    f"종가: {detail['close']:.6g}\n"
                    f"몸통: {detail['body']:.6g} / 윗꼬리: {detail['upper_wick']:.6g}\n"
                    f"고가 대비 종가 하락률: -{detail['high_close_diff_pct']:.2f}%\n"
                    f"조건: 최근 {LOOKBACK}개 캔들 중 신고가 + 음봉 + 역망치형"
                )
                log.info("신호 발생: %s", symbol)
                send_telegram(msg)

            state[symbol] = latest_candle_ts
        except ccxt.BaseError as e:
            log.error("[%s] ccxt 오류: %s", symbol, e)
        except Exception as e:
            log.error("[%s] 알 수 없는 오류: %s", symbol, e)


# ============================== SCHEDULER ==============================


def seconds_until_next_close():
    interval_sec = 15 * 60
    now_epoch = time.time()
    next_close_epoch = (int(now_epoch // interval_sec) + 1) * interval_sec
    wait = next_close_epoch - now_epoch
    return wait + CLOSE_BUFFER_SEC


def run_once():
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state()
    log.info("1회성 체크 실행 (%d개 심볼)", len(symbols))
    check_all_symbols(exchange, symbols, state)
    save_state(state)


def run_loop():
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state()
    log.info("15분봉 감시를 시작합니다. (%d개 심볼)", len(symbols))

    while True:
        wait_sec = seconds_until_next_close()
        log.info("다음 15분봉 마감까지 %.0f초 대기", wait_sec)
        time.sleep(wait_sec)
        log.info("캔들 마감 감지 → 전 종목 조건 체크 시작")
        check_all_symbols(exchange, symbols, state)
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="OKX 15분봉 캔들 신호 감시 봇")
    parser.add_argument("--once", action="store_true", help="1회만 체크하고 종료")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_loop()


if __name__ == "__main__":
    main()
