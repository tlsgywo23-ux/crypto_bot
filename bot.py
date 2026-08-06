"""
OKX 무기한 선물(perpetual swap) 캔들 신호 감시 봇
- 타임프레임: 15분봉 / 1시간봉 / 4시간봉
- 신호 패턴: 역망치형 음봉(고점 갱신 실패형 매도세) / 망치형 양봉(저점 갱신 후 매수세)
- 추가 필터: RSI 다이버전스
    * 역망치 음봉: 신고가 갱신 + RSI는 이전 고점보다 낮음 (하락 다이버전스)
    * 망치 양봉  : 신저가 갱신 + RSI는 이전 저점보다 높음 (상승 다이버전스)
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

# 감시할 타임프레임 목록 (lookback: 극값/다이버전스 판단에 사용하는 캔들 개수)
TIMEFRAMES = [
    {"tf": "15m", "ms": 15 * 60 * 1000, "lookback": 30},
    {"tf": "1h", "ms": 60 * 60 * 1000, "lookback": 30},
    {"tf": "4h", "ms": 4 * 60 * 60 * 1000, "lookback": 30},
]

# RSI 설정
RSI_PERIOD = 14          # RSI 계산 기간 (표준 14)
RSI_WARMUP_BARS = 100    # RSI 값이 안정화되도록 lookback 앞에 추가로 확보하는 캔들 수

# 캔들 모양 설정
# 반대쪽 꼬리(신호 방향이 아닌 쪽)는 캔들 전체 범위(고가-저가) 대비 이 비율 이내로 짧아야 함
MAX_OPPOSITE_WICK_RATIO = 0.15

# 루프 모드에서 깨어나는 주기(초). 가장 짧은 타임프레임(15m) 기준.
TICK_INTERVAL_SEC = 15 * 60
CLOSE_BUFFER_SEC = 12

STATE_FILE = os.environ.get("STATE_FILE", "alert_state.json")

RAW_SYMBOLS = [
    "BTC", "ETH", "ZEC", "MU", "BCH", "LINK", "BEAT", "SOL", "SOXL", "LAB",
    "NEAR", "XRP", "SUI", "ONDO", "WLD", "ALLO", "H", "OPN", "CRV", "DOGE",
    "BSB", "HOME", "SAHARA", "HMSTR", "TRUMP", "EDGE", "PEPE", "XPL", "SPACE",
    "COAI", "RE", "ADA", "O", "BASED", "HYPE", "SLX", "NES", "CAP", "LIT", "BNB",
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


def state_key(symbol: str, timeframe: str, signal_id: str) -> str:
    return f"{symbol}|{timeframe}|{signal_id}"


# ============================== RSI ==============================


def compute_rsi(closes, period: int = RSI_PERIOD):
    """Wilder 방식 RSI. closes와 같은 길이의 리스트를 반환하며,
    워밍업 구간(index < period)은 None으로 채워집니다.
    rsi[i]는 closes[i] 시점의 RSI 값입니다."""
    n = len(closes)
    rsi = [None] * n
    if n <= period:
        return rsi

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return rsi


# ============================== SIGNAL DEFINITIONS ==============================
#
# 두 신호 모두 "직전 캔들들(현재 캔들 제외) 극값 대비 현재 캔들이 극값을 갱신 +
# 반대 방향 마감 + 갱신 방향 꼬리가 몸통보다 크거나 같음 + RSI 다이버전스"라는
# 동일한 뼈대를 공유하고, 방향만 반대입니다.
#
#  - 역망치 음봉(inverted_hammer_bearish):
#      직전 구간 대비 신고가 + 음봉(종가<시가) + 윗꼬리 >= 몸통
#      + RSI 하락 다이버전스(현재 RSI < 직전 고점 캔들의 RSI) → 상단에서 매도세 유입 신호
#  - 망치 양봉(hammer_bullish):
#      직전 구간 대비 신저가 + 양봉(종가>시가) + 아랫꼬리 >= 몸통
#      + RSI 상승 다이버전스(현재 RSI > 직전 저점 캔들의 RSI) → 하단에서 매수세 유입 신호


def check_inverted_hammer_bearish(candles, rsi_series, lookback: int):
    if len(candles) < lookback or len(rsi_series) < lookback:
        return False, None

    window = candles[-lookback:]
    rsi_window = rsi_series[-lookback:]
    if any(r is None for r in rsi_window):
        return False, None  # RSI 워밍업 미완료

    prior_window = window[:-1]
    prior_rsi = rsi_window[:-1]
    if not prior_window:
        return False, None

    _, o, h, l, c, v = window[-1]
    cur_rsi = rsi_window[-1]

    prior_max_high = max(cd[2] for cd in prior_window)
    is_new_extreme = h > prior_max_high  # 직전 구간 대비 신고가

    is_directional = c < o  # 음봉

    body = abs(c - o)
    wick = h - max(o, c)  # 윗꼬리 (신호 방향 꼬리)
    opposite_wick = min(o, c) - l  # 밑꼬리 (반대쪽 꼬리)
    candle_range = h - l

    is_wick_ok = wick >= body  # 윗꼬리는 몸통보다 크거나 같아야 함
    is_opposite_wick_ok = opposite_wick <= candle_range * MAX_OPPOSITE_WICK_RATIO  # 밑꼬리는 없거나 매우 짧아야 함
    is_shape_ok = is_wick_ok and is_opposite_wick_ok

    # 직전 구간에서 고점을 찍었던 캔들의 RSI
    peak_idx = max(range(len(prior_window)), key=lambda i: prior_window[i][2])
    prior_peak_rsi = prior_rsi[peak_idx]
    prior_peak_high = prior_window[peak_idx][2]
    is_bearish_divergence = cur_rsi < prior_peak_rsi

    ok = is_new_extreme and is_directional and is_shape_ok and is_bearish_divergence
    extreme_diff_pct = ((h - c) / h * 100) if h != 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "wick": wick, "wick_label": "윗꼬리",
        "opposite_wick": opposite_wick, "opposite_wick_label": "밑꼬리",
        "extreme_label": "신고가",
        "extreme_diff_pct": extreme_diff_pct,
        "extreme_diff_label": "고가 대비 종가 하락률",
        "condition_label": "신고가 갱신 + 음봉 + 윗꼬리≥몸통 + 밑꼬리 짧음 + RSI 하락다이버전스",
        "cur_rsi": cur_rsi,
        "ref_rsi": prior_peak_rsi,
        "ref_price": prior_peak_high,
        "ref_label": "직전 고점",
    }
    return ok, detail


def check_hammer_bullish(candles, rsi_series, lookback: int):
    if len(candles) < lookback or len(rsi_series) < lookback:
        return False, None

    window = candles[-lookback:]
    rsi_window = rsi_series[-lookback:]
    if any(r is None for r in rsi_window):
        return False, None  # RSI 워밍업 미완료

    prior_window = window[:-1]
    prior_rsi = rsi_window[:-1]
    if not prior_window:
        return False, None

    _, o, h, l, c, v = window[-1]
    cur_rsi = rsi_window[-1]

    prior_min_low = min(cd[3] for cd in prior_window)
    is_new_extreme = l < prior_min_low  # 직전 구간 대비 신저가

    is_directional = c > o  # 양봉

    body = abs(c - o)
    wick = min(o, c) - l  # 아랫꼬리 (신호 방향 꼬리)
    opposite_wick = h - max(o, c)  # 윗꼬리 (반대쪽 꼬리)
    candle_range = h - l

    is_wick_ok = wick >= body  # 아랫꼬리는 몸통보다 크거나 같아야 함
    is_opposite_wick_ok = opposite_wick <= candle_range * MAX_OPPOSITE_WICK_RATIO  # 윗꼬리는 없거나 매우 짧아야 함
    is_shape_ok = is_wick_ok and is_opposite_wick_ok

    # 직전 구간에서 저점을 찍었던 캔들의 RSI
    trough_idx = min(range(len(prior_window)), key=lambda i: prior_window[i][3])
    prior_trough_rsi = prior_rsi[trough_idx]
    prior_trough_low = prior_window[trough_idx][3]
    is_bullish_divergence = cur_rsi > prior_trough_rsi

    ok = is_new_extreme and is_directional and is_shape_ok and is_bullish_divergence
    extreme_diff_pct = ((c - l) / l * 100) if l != 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "wick": wick, "wick_label": "아랫꼬리",
        "opposite_wick": opposite_wick, "opposite_wick_label": "윗꼬리",
        "extreme_label": "신저가",
        "extreme_diff_pct": extreme_diff_pct,
        "extreme_diff_label": "저가 대비 종가 상승률",
        "condition_label": "신저가 갱신 + 양봉 + 아랫꼬리≥몸통 + 윗꼬리 짧음 + RSI 상승다이버전스",
        "cur_rsi": cur_rsi,
        "ref_rsi": prior_trough_rsi,
        "ref_price": prior_trough_low,
        "ref_label": "직전 저점",
    }
    return ok, detail


SIGNALS = [
    {"id": "inv_hammer_bear", "emoji": "🔻", "name": "역망치 음봉", "fn": check_inverted_hammer_bearish},
    {"id": "hammer_bull", "emoji": "🔨", "name": "망치 양봉", "fn": check_hammer_bullish},
]

# ============================== CORE LOGIC ==============================


def fetch_closed_candles(exchange: ccxt.okx, symbol: str, timeframe: str, timeframe_ms: int, count: int):
    now_ms = exchange.milliseconds()
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=count + 2)
    closed = [c for c in raw if c[0] + timeframe_ms <= now_ms]
    return closed[-count:]


def check_symbol_timeframe(exchange, symbol, state, tf_conf, candles_cache):
    """한 심볼 x 한 타임프레임에 대해 캔들/RSI를 가져와서 등록된 모든 신호를 체크"""
    timeframe = tf_conf["tf"]
    timeframe_ms = tf_conf["ms"]
    lookback = tf_conf["lookback"]

    # RSI가 lookback 구간 내내 안정된 값을 갖도록 앞쪽에 워밍업 구간을 더 받아온다.
    fetch_count = lookback + RSI_PERIOD + RSI_WARMUP_BARS

    cache_key = (symbol, timeframe)
    if cache_key not in candles_cache:
        candles = fetch_closed_candles(exchange, symbol, timeframe, timeframe_ms, fetch_count)
        closes = [cd[4] for cd in candles]
        rsi_series = compute_rsi(closes, RSI_PERIOD)
        candles_cache[cache_key] = (candles, rsi_series)

    candles, rsi_series = candles_cache[cache_key]
    if not candles:
        return

    latest_candle_ts = candles[-1][0]
    candle_time = datetime.datetime.fromtimestamp(
        latest_candle_ts / 1000, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    for sig in SIGNALS:
        key = state_key(symbol, timeframe, sig["id"])
        if state.get(key) == latest_candle_ts:
            continue

        ok, detail = sig["fn"](candles, rsi_series, lookback)
        if ok:
            msg = (
                f"{sig['emoji']} <b>{symbol}</b> [{timeframe}] {sig['name']} 신호 발생\n"
                f"캔들 마감시각: {candle_time}\n"
                f"시가: {detail['open']:.6g}\n"
                f"고가: {detail['high']:.6g}\n"
                f"저가: {detail['low']:.6g}\n"
                f"종가: {detail['close']:.6g}\n"
                f"몸통: {detail['body']:.6g} / {detail['wick_label']}: {detail['wick']:.6g} / {detail['opposite_wick_label']}: {detail['opposite_wick']:.6g}\n"
                f"{detail['extreme_diff_label']}: {detail['extreme_diff_pct']:.2f}%\n"
                f"RSI(현재): {detail['cur_rsi']:.2f} / RSI({detail['ref_label']}, {detail['ref_price']:.6g}): {detail['ref_rsi']:.2f}\n"
                f"조건: 최근 {lookback}개 [{timeframe}] 캔들 중 {detail['condition_label']}"
            )
            log.info("신호 발생: %s [%s] %s", symbol, timeframe, sig["name"])
            send_telegram(msg)

        state[key] = latest_candle_ts


def check_all_symbols(exchange: ccxt.okx, symbols, state: dict):
    """모든 타임프레임 x 모든 신호 조합을 심볼별로 체크

    타임프레임 하나에서 오류(네트워크 순단 등)가 나도 같은 심볼의
    나머지 타임프레임 체크는 계속 진행되도록, try/except를 타임프레임
    단위로 감쌉니다. (심볼 단위로 감싸면 15m에서 에러 시 1h/4h까지
    이번 회차에서 통째로 스킵되는 문제가 있었음)
    """
    for symbol in symbols:
        candles_cache = {}
        for tf_conf in TIMEFRAMES:
            try:
                check_symbol_timeframe(exchange, symbol, state, tf_conf, candles_cache)
            except ccxt.BaseError as e:
                log.error("[%s|%s] ccxt 오류: %s", symbol, tf_conf["tf"], e)
            except Exception as e:
                log.error("[%s|%s] 알 수 없는 오류: %s", symbol, tf_conf["tf"], e)


# ============================== SCHEDULER ==============================


def seconds_until_next_close():
    interval_sec = TICK_INTERVAL_SEC
    now_epoch = time.time()
    next_close_epoch = (int(now_epoch // interval_sec) + 1) * interval_sec
    wait = next_close_epoch - now_epoch
    return wait + CLOSE_BUFFER_SEC


def run_once():
    # 외부 스케줄러(cron-job.org)가 정각(0,15,30,45분)에 정확히 이 실행을
    # 트리거하므로, 캔들이 거래소에 완전히 반영될 시간을 벌기 위해
    # 여기서 CLOSE_BUFFER_SEC(12초)만큼 대기한 뒤 체크를 시작한다.
    log.info("정각 트리거 감지 → %d초 대기 후 체크 시작", CLOSE_BUFFER_SEC)
    time.sleep(CLOSE_BUFFER_SEC)

    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state()
    log.info(
        "1회성 체크 실행 (%d개 심볼, 타임프레임: %s, 신호: %s)",
        len(symbols),
        ", ".join(t["tf"] for t in TIMEFRAMES),
        ", ".join(s["name"] for s in SIGNALS),
    )
    check_all_symbols(exchange, symbols, state)
    save_state(state)


def run_loop():
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state()
    log.info(
        "캔들 감시를 시작합니다. (%d개 심볼, 타임프레임: %s, 신호: %s)",
        len(symbols),
        ", ".join(t["tf"] for t in TIMEFRAMES),
        ", ".join(s["name"] for s in SIGNALS),
    )

    while True:
        wait_sec = seconds_until_next_close()
        log.info("다음 체크 틱까지 %.0f초 대기", wait_sec)
        time.sleep(wait_sec)
        log.info("틱 발생 → 전 타임프레임/전 신호/전 종목 조건 체크 시작")
        check_all_symbols(exchange, symbols, state)
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="OKX 캔들 신호 감시 봇 (15m/1h/4h, 역망치음봉/망치양봉 + RSI 다이버전스)")
    parser.add_argument("--once", action="store_true", help="1회만 체크하고 종료")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_loop()


if __name__ == "__main__":
    main()
