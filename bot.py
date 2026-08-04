"""
OKX 무기한 선물(perpetual swap) 15분봉 캔들 신호 감시 봇
----------------------------------------------------------
[신호 조건] 15분봉이 마감된 직후, 그 캔들이
  1) 직전 14개 캔들 + 자기 자신, 총 15개 캔들 중 고가(꼬리 포함)가 가장 높고
  2) 음봉(종가 < 시가)이며
  3) 윗꼬리 길이 >= 몸통 길이 (역망치형 모양)
인 경우 텔레그램으로 알람을 보낸다.

필요 패키지 설치:
    pip install ccxt requests --break-system-packages

실행 전 아래 CONFIG 섹션의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 채워 넣을 것.
(이미 세팅된 텔레그램 봇의 토큰과, 알람을 받을 chat_id)

실행:
    python3 okx_candle_alert.py
    (계속 떠 있어야 하는 프로세스이므로 서버에서는 nohup / systemd / screen / tmux 등으로 상시 구동 권장)
"""

import ccxt
import requests
import time
import datetime
import logging

# ============================== CONFIG ==============================

TELEGRAM_BOT_TOKEN = "여기에_봇_토큰_입력"
TELEGRAM_CHAT_ID = "여기에_챗ID_입력"

TIMEFRAME = "15m"
TIMEFRAME_MS = 15 * 60 * 1000

# 최근 몇 개 캔들 중 "신고점"인지 판단할지 (요청: 직전 15개 캔들, 자기 자신 포함)
LOOKBACK = 15

# 캔들 마감 후 거래소 데이터 반영 대기 시간(초). 너무 짧으면 OKX가 아직
# 해당 봉을 확정하지 않은 상태로 조회될 수 있어 여유를 둔다.
CLOSE_BUFFER_SEC = 12

# 감시할 티커 목록 (OKX 무기한 선물 기준 base 심볼, USDT 마진)
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
    """RAW_SYMBOLS를 ccxt 통합 심볼(BASE/USDT:USDT)로 매핑하고,
    실제 OKX에 없는 티커는 걸러서 경고만 남긴다."""
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
            "OKX 무기한선물(USDT 마진)에서 찾지 못한 티커 (심볼명이 다르거나 상장되지 않음): %s",
            ", ".join(missing),
        )
    log.info("감시 대상 %d개 심볼 확정: %s", len(resolved), ", ".join(resolved))
    return resolved


# ============================== CORE LOGIC ==============================


def fetch_closed_candles(exchange: ccxt.okx, symbol: str, count: int):
    """마감이 완료된 캔들만 최신순으로 count개 반환 (가장 마지막 원소가 가장 최근 마감봉)."""
    now_ms = exchange.milliseconds()
    raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=count + 2)
    # raw candle: [timestamp, open, high, low, close, volume]
    closed = [c for c in raw if c[0] + TIMEFRAME_MS <= now_ms]
    return closed[-count:]


def check_signal(candles):
    """candles: 마감된 캔들 리스트, 마지막 원소가 방금 마감된 캔들.
    len(candles) >= LOOKBACK 이어야 함."""
    if len(candles) < LOOKBACK:
        return False, None

    window = candles[-LOOKBACK:]
    current = window[-1]
    _, o, h, l, c, v = current

    # 1) 최근 LOOKBACK개(자기 자신 포함) 중 고가 최고
    max_high = max(cd[2] for cd in window)
    is_new_high = h >= max_high  # 부동소수 오차 대비 >=

    # 2) 음봉
    is_bearish = c < o

    # 3) 윗꼬리 >= 몸통
    body = abs(c - o)
    upper_wick = h - max(o, c)
    is_inverted_hammer_shape = upper_wick >= body

    ok = is_new_high and is_bearish and is_inverted_hammer_shape

    # 고가 대비 종가가 몇 % 아래에서 마감했는지 (고가 기준)
    high_close_diff_pct = ((h - c) / h * 100) if h != 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "upper_wick": upper_wick,
        "is_new_high": is_new_high,
        "high_close_diff_pct": high_close_diff_pct,
    }
    return ok, detail


def check_all_symbols(exchange: ccxt.okx, symbols):
    for symbol in symbols:
        try:
            candles = fetch_closed_candles(exchange, symbol, LOOKBACK)
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
        except ccxt.BaseError as e:
            log.error("[%s] ccxt 오류: %s", symbol, e)
        except Exception as e:
            log.error("[%s] 알 수 없는 오류: %s", symbol, e)


# ============================== SCHEDULER ==============================


def seconds_until_next_close():
    """UTC 기준 다음 15분 봉 마감 시각(:00, :15, :30, :45)까지 남은 초 + 여유버퍼."""
    interval_sec = 15 * 60
    now_epoch = time.time()
    next_close_epoch = (int(now_epoch // interval_sec) + 1) * interval_sec
    wait = next_close_epoch - now_epoch
    return wait + CLOSE_BUFFER_SEC


def main():
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다. RAW_SYMBOLS를 확인하세요.")
        return

    log.info("15분봉 감시를 시작합니다. (%d개 심볼)", len(symbols))

    while True:
        wait_sec = seconds_until_next_close()
        log.info("다음 15분봉 마감까지 %.0f초 대기", wait_sec)
        time.sleep(wait_sec)
        log.info("캔들 마감 감지 → 전 종목 조건 체크 시작")
        check_all_symbols(exchange, symbols)


if __name__ == "__main__":
    main()
