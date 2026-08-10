"""
OKX 무기한 선물(perpetual swap) 캔들 신호 감시 봇
- 타임프레임: 15분봉 / 1시간봉 / 4시간봉
- 신호 패턴: 역망치형 음봉(고점 갱신 실패형 매도세) / 망치형 양봉(저점 갱신 후 매수세)
- 추가 필터: RSI 다이버전스 (진짜 스윙 피벗 기준)
    * 역망치 음봉: 신고가 갱신 + RSI는 "최소 MIN_PIVOT_DISTANCE개 이전의 스윙 고점"보다 낮음
    * 망치 양봉  : 신저가 갱신 + RSI는 "최소 MIN_PIVOT_DISTANCE개 이전의 스윙 저점"보다 높음
    * 거래량은 신호 판정 조건에서 제외됨 (참고용으로 메시지에만 표시).
      신호 발생 시 거래량 수준은 사용자가 직접 보고 진입 여부를 판단.

[2026-08 수정사항]
- 다이버전스 비교 기준을 "lookback 구간 내 단순 최고/최저 캔들"에서
  "좌우 캔들(PIVOT_LEFT/RIGHT개)보다 실제로 튀어나온 스윙 피벗(pivot high/low)"
  으로 변경. → 트렌드 도중의 캔들이 잘못 기준점으로 잡혀서 엉뚱한 곳에서
  신호가 뜨던 문제를 해결.
- 비교 기준 스윙 피벗은 현재 캔들로부터 최소 MIN_PIVOT_DISTANCE(=7)개
  이상 떨어져 있어야만 후보로 인정. → 너무 최근(바로 몇 캔들 전)의
  피벗을 기준 삼아 생기던 노이즈성 신호 방지.
- 캔들 꼬리/몸통 모양 조건은 원래 기준(꼬리>=몸통) 그대로 유지.
- 감시 타임프레임에서 12h, 1d 제거 (15m/1h/4h만 감시).
- [제거] 거래량 상대순위(백분위) 필터를 신호 판정 조건에서 제외함.
  → 캔들 모양/다이버전스 조건만 맞으면 거래량 수준과 무관하게 신호가 발생.
  거래량(현재값/직전 구간 중앙값/백분위)은 여전히 계산되어 텔레그램
  메시지에 참고용으로 표시되며, 진입 여부 판단은 사용자가 직접 함.
- [신규] 상태 파일(STATE_FILE)을 --group 값에 따라 자동으로 분리
  (alert_state_short.json / alert_state_long.json). 15분마다 도는
  워크플로우와 1시간마다 도는 워크플로우가 서로 다른 파일을 커밋하게
  되어, 두 워크플로우가 같은 alert_state.json을 동시에 건드리다가
  git merge conflict가 나던 문제를 방지. 환경변수 STATE_FILE을 명시적으로
  지정하면 그 값이 항상 최우선.
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
# group: 실행 그룹 구분용. 각 그룹은 해당 타임프레임의 캔들 마감 주기에 맞춰
#        딱 그만큼만 자주 체크하도록 나눠서, 불필요한 API 호출/Actions 사용량을 줄인다.
#   "short" = 15분마다 도는 워크플로우에서 체크 (15m 캔들은 15분마다 마감되므로
#             이 주기로 체크해야만 놓치지 않음)
#   "long"  = 1시간마다 도는 워크플로우에서 체크 (1h/4h는 어차피 캔들이 1시간
#             단위 이상으로만 마감되므로, 15분마다 체크해봤자 대부분 "아직
#             그대로"라 API 호출만 낭비. 1시간 주기로 체크해도 놓치는 캔들 없음)
TIMEFRAMES = [
    {"tf": "15m", "ms": 15 * 60 * 1000, "lookback": 30, "group": "short"},
    {"tf": "1h", "ms": 60 * 60 * 1000, "lookback": 30, "group": "long"},
    {"tf": "4h", "ms": 4 * 60 * 60 * 1000, "lookback": 30, "group": "long"},
    {"tf": "12h", "ms": 12 * 60 * 60 * 1000, "lookback": 30, "group": "long"},
]

# RSI 설정
RSI_PERIOD = 14          # RSI 계산 기간 (표준 14)
RSI_WARMUP_BARS = 100    # RSI 값이 안정화되도록 lookback 앞에 추가로 확보하는 캔들 수

# --- 캔들 모양 설정 (원래 기준 유지) ---
# 반대쪽 꼬리(신호 방향이 아닌 쪽)는 캔들 전체 범위(고가-저가) 대비 이 비율 이내로 짧아야 함
MAX_OPPOSITE_WICK_RATIO = 0.15

# --- 스윙 피벗(다이버전스 비교 기준점) 탐지 설정 ---
# 어떤 캔들이 "스윙 고점/저점"으로 인정되려면 좌우 이만큼의 캔들보다 더 튀어나와야 함
PIVOT_LEFT = 2
PIVOT_RIGHT = 2

# 다이버전스 비교 기준으로 쓸 스윙 피벗은 현재 캔들로부터 최소 이만큼(개수) 떨어져 있어야 함
# → 너무 가까운(직전 몇 개 캔들 안의) 피벗을 기준으로 삼아서 생기는 노이즈성 신호 방지
MIN_PIVOT_DISTANCE = 7

# --- 거래량 참고 지표 설정 (신호 판정에는 더 이상 사용하지 않음, 메시지 표시용) ---
# 상대순위/중앙값을 계산할 때 볼 직전 캔들 개수 (현재 캔들 제외, 가장 최근 것부터 이만큼).
# None이면 lookback 구간 전체(prior_window)를 다 사용.
VOLUME_AVG_LOOKBACK = 20

# 루프 모드에서 깨어나는 주기(초). 가장 짧은 타임프레임(15m) 기준.
TICK_INTERVAL_SEC = 15 * 60
CLOSE_BUFFER_SEC = 12

# 캔들 마감시각을 텔레그램 메시지에 표시할 때 쓰는 타임존 (한국시간)
KST = datetime.timezone(datetime.timedelta(hours=9))

# 상태 파일(중복 알람 방지용) 기본 이름. group="all"이거나 group을 안 쓸 때 사용.
DEFAULT_STATE_FILE = "alert_state.json"

RAW_SYMBOLS = [
    "BTC", "ETH", "ZEC", "MU", "BCH", "LINK", "BEAT", "SOL", "SOXL", "LAB",
    "NEAR", "XRP", "SUI", "ONDO", "WLD", "ALLO", "H", "OPN", "CRV", "DOGE",
    "BSB", "HOME", "SAHARA", "HMSTR", "TRUMP", "EDGE", "PEPE", "XPL", "SPACE",
    "COAI", "RE", "ADA", "O", "BASED", "HYPE", "SLX", "NES", "CAP", "LIT", "BNB",
    "BICO", "KAITO", "MMT", "SNDK", "PUMP", "MUBARAK", "GIGGLE", "GRVT", "OKX", "UB",
]

# 심볼 이름 -> RAW_SYMBOLS 안에서의 순번(1부터 시작) 조회용
SYMBOL_RANK = {sym: idx + 1 for idx, sym in enumerate(RAW_SYMBOLS)}

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


def resolve_state_file(group: str) -> str:
    """이번 실행에서 쓸 상태 파일 경로를 결정.

    우선순위:
    1) 환경변수 STATE_FILE이 명시적으로 지정되어 있으면 그 값을 그대로 사용
       (워크플로우 yml에서 직접 지정하고 싶을 때를 위한 탈출구).
    2) 지정 안 되어 있으면 group에 따라 자동 분기:
       - group == "all" → alert_state.json (기존 단일 워크플로우 방식과 동일)
       - group == "short"/"long" 등 → alert_state_{group}.json

    이렇게 group별로 파일을 분리하면, 15분마다 도는 워크플로우와 1시간마다
    도는 워크플로우가 서로 다른 파일을 커밋하게 되어 동시 실행 시 같은
    파일을 두고 git merge conflict가 나는 문제를 원천적으로 막을 수 있다.
    """
    env_override = os.environ.get("STATE_FILE")
    if env_override:
        return env_override
    if group == "all":
        return DEFAULT_STATE_FILE
    return f"alert_state_{group}.json"


def load_state(state_file: str):
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning("상태 파일 로드 실패, 빈 상태로 시작: %s", e)
    return {}


def save_state(state, state_file: str):
    try:
        with open(state_file, "w") as f:
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


# ============================== PIVOT (스윙 고점/저점) ==============================


def find_pivot_highs(window, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT):
    """window 안에서 좌우 각각 left/right개 캔들보다 고가가 높거나 같은
    '진짜' 스윙 고점 캔들들의 인덱스 리스트를 반환. 그냥 구간 내 최댓값이
    아니라, 실제로 양옆보다 튀어나온 지점만 피벗으로 인정한다."""
    pivots = []
    n = len(window)
    for i in range(left, n - right):
        h = window[i][2]
        if all(window[j][2] <= h for j in range(i - left, i)) and \
           all(window[j][2] <= h for j in range(i + 1, i + 1 + right)):
            pivots.append(i)
    return pivots


def find_pivot_lows(window, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT):
    """find_pivot_highs와 대칭. 좌우보다 저가가 낮거나 같은 스윙 저점만 인정."""
    pivots = []
    n = len(window)
    for i in range(left, n - right):
        l = window[i][3]
        if all(window[j][3] >= l for j in range(i - left, i)) and \
           all(window[j][3] >= l for j in range(i + 1, i + 1 + right)):
            pivots.append(i)
    return pivots


# ============================== VOLUME (참고용 지표, 신호 판정에는 미사용) ==============================


def compute_median(values):
    """정렬 후 가운데 값(짝수개면 가운데 두 값의 평균)을 반환. 이상치 캔들 하나에
    쉽게 끌려가는 평균과 달리, 중앙값은 그런 이상치에 강건해서 "평소 거래량"을
    더 대표성 있게 보여줌 (참고 표시용)."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def compute_percentile_rank(ref_values, current_value):
    """ref_values(직전 캔들들의 거래량 목록) 중 current_value보다 작거나 같은
    값의 비율을 0~100 백분위로 반환. 예: 반환값 80 → 직전 구간 캔들의 80%보다
    현재 거래량이 크거나 같다는 뜻 (=상위 20%). 신호 판정에는 사용하지 않고
    메시지 표시용 참고 지표로만 사용."""
    if not ref_values:
        return 0.0
    count_le = sum(1 for v in ref_values if v <= current_value)
    return (count_le / len(ref_values)) * 100.0


def compute_volume_stats(prior_window, current_volume, avg_lookback=VOLUME_AVG_LOOKBACK):
    """현재 캔들 거래량의 직전 구간 대비 백분위 순위와 중앙값을 계산.
    신호 판정 조건에는 더 이상 쓰이지 않고, 텔레그램 메시지에 참고용으로만
    표시됨 (진입 여부는 사용자가 직접 판단). avg_lookback이 주어지면 prior_window
    중 가장 최근 그만큼만 기준 구간으로 사용, None이면 prior_window 전체 사용.
    반환값: (백분위순위, 중앙값)"""
    if not prior_window:
        return 0.0, 0.0
    ref_candles = prior_window[-avg_lookback:] if avg_lookback else prior_window
    if not ref_candles:
        return 0.0, 0.0
    ref_volumes = [cd[5] for cd in ref_candles]
    percentile = compute_percentile_rank(ref_volumes, current_volume)
    median_volume = compute_median(ref_volumes)
    return percentile, median_volume


# ============================== SIGNAL DEFINITIONS ==============================
#
# 두 신호 모두 "직전 스윙 극값 대비 현재 캔들이 극값을 갱신 +
# 반대 방향 마감 + 갱신 방향 꼬리가 몸통보다 크거나 같음 +
# RSI가 직전 스윙 피벗 대비 다이버전스"라는 동일한 뼈대를 공유하고,
# 방향만 반대입니다. 거래량은 판정 조건이 아니며 참고용으로만 계산/표시됩니다.
#
#  - 역망치 음봉(inverted_hammer_bearish):
#      직전 스윙고점 대비 신고가 + 음봉(종가<시가) + 윗꼬리>=몸통
#      + RSI 하락 다이버전스(현재 RSI가 직전 스윙고점 RSI보다 낮음)
#      → 상단에서 강한 매도세 유입 신호
#  - 망치 양봉(hammer_bullish):
#      직전 스윙저점 대비 신저가 + 양봉(종가>시가) + 아랫꼬리>=몸통
#      + RSI 상승 다이버전스(현재 RSI가 직전 스윙저점 RSI보다 높음)
#      → 하단에서 강한 매수세 유입 신호


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

    # 직전 구간에서 "진짜 스윙 고점"(양옆 캔들보다 확실히 높은 캔들)이었던 것들 중,
    # 현재 캔들로부터 최소 MIN_PIVOT_DISTANCE개 이상 떨어진 것만 후보로 삼고,
    # 그중 가장 높은 것을 비교 기준으로 사용
    pivot_idxs = find_pivot_highs(prior_window)
    pivot_idxs = [i for i in pivot_idxs if (len(prior_window) - i) >= MIN_PIVOT_DISTANCE]
    if not pivot_idxs:
        return False, None  # 참조할 스윙 고점이 없으면 판단 불가 → 신호 없음
    peak_idx = max(pivot_idxs, key=lambda i: prior_window[i][2])
    prior_peak_rsi = prior_rsi[peak_idx]
    prior_peak_high = prior_window[peak_idx][2]

    is_bearish_divergence = cur_rsi < prior_peak_rsi

    # 거래량은 참고용 지표로만 계산 (판정에는 반영하지 않음)
    volume_percentile, median_volume = compute_volume_stats(prior_window, v)

    ok = is_new_extreme and is_directional and is_shape_ok and is_bearish_divergence
    extreme_diff_pct = ((h - c) / h * 100) if h != 0 else 0.0
    volume_ratio_vs_median = (v / median_volume) if median_volume > 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "wick": wick, "wick_label": "윗꼬리",
        "opposite_wick": opposite_wick, "opposite_wick_label": "밑꼬리",
        "extreme_label": "신고가",
        "extreme_diff_pct": extreme_diff_pct,
        "extreme_diff_label": "고가 대비 종가 하락률",
        "condition_label": "신고가 갱신 + 음봉 + 윗꼬리≥몸통 + 밑꼬리 짧음 + RSI 하락다이버전스(스윙피벗 기준, 최소 %d개 이전)" % MIN_PIVOT_DISTANCE,
        "cur_rsi": cur_rsi,
        "ref_rsi": prior_peak_rsi,
        "ref_price": prior_peak_high,
        "ref_label": "직전 스윙고점",
        "cur_volume": v,
        "median_volume": median_volume,
        "volume_percentile": volume_percentile,
        "volume_ratio_vs_median": volume_ratio_vs_median,
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

    # 직전 구간에서 "진짜 스윙 저점"(양옆 캔들보다 확실히 낮은 캔들)이었던 것들 중,
    # 현재 캔들로부터 최소 MIN_PIVOT_DISTANCE개 이상 떨어진 것만 후보로 삼고,
    # 그중 가장 낮은 것을 비교 기준으로 사용
    pivot_idxs = find_pivot_lows(prior_window)
    pivot_idxs = [i for i in pivot_idxs if (len(prior_window) - i) >= MIN_PIVOT_DISTANCE]
    if not pivot_idxs:
        return False, None  # 참조할 스윙 저점이 없으면 판단 불가 → 신호 없음
    trough_idx = min(pivot_idxs, key=lambda i: prior_window[i][3])
    prior_trough_rsi = prior_rsi[trough_idx]
    prior_trough_low = prior_window[trough_idx][3]

    is_bullish_divergence = cur_rsi > prior_trough_rsi

    # 거래량은 참고용 지표로만 계산 (판정에는 반영하지 않음)
    volume_percentile, median_volume = compute_volume_stats(prior_window, v)

    ok = is_new_extreme and is_directional and is_shape_ok and is_bullish_divergence
    extreme_diff_pct = ((c - l) / l * 100) if l != 0 else 0.0
    volume_ratio_vs_median = (v / median_volume) if median_volume > 0 else 0.0

    detail = {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "wick": wick, "wick_label": "아랫꼬리",
        "opposite_wick": opposite_wick, "opposite_wick_label": "윗꼬리",
        "extreme_label": "신저가",
        "extreme_diff_pct": extreme_diff_pct,
        "extreme_diff_label": "저가 대비 종가 상승률",
        "condition_label": "신저가 갱신 + 양봉 + 아랫꼬리≥몸통 + 윗꼬리 짧음 + RSI 상승다이버전스(스윙피벗 기준, 최소 %d개 이전)" % MIN_PIVOT_DISTANCE,
        "cur_rsi": cur_rsi,
        "ref_rsi": prior_trough_rsi,
        "ref_price": prior_trough_low,
        "ref_label": "직전 스윙저점",
        "cur_volume": v,
        "median_volume": median_volume,
        "volume_percentile": volume_percentile,
        "volume_ratio_vs_median": volume_ratio_vs_median,
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
    # 캔들 마감시각은 한국시간(KST)으로 표시
    candle_time = datetime.datetime.fromtimestamp(
        latest_candle_ts / 1000, tz=KST
    ).strftime("%Y-%m-%d %H:%M KST")

    # 심볼 순번 (RAW_SYMBOLS 안에서 몇 번째로 감시하는 종목인지)
    raw_symbol = symbol.split("/")[0]
    rank = SYMBOL_RANK.get(raw_symbol)
    rank_label = f"{rank}번째" if rank else "?번째"

    for sig in SIGNALS:
        key = state_key(symbol, timeframe, sig["id"])
        if state.get(key) == latest_candle_ts:
            continue

        ok, detail = sig["fn"](candles, rsi_series, lookback)
        if ok:
            msg = (
                f"{sig['emoji']} <b>{raw_symbol}</b> [{timeframe}] {sig['name']} 신호 발생\n"
                f"({rank_label} 종목)\n"
                f"캔들 마감시각: {candle_time}\n"
                f"{detail['wick_label']}: {detail['wick']:.6g} / {detail['opposite_wick_label']}: {detail['opposite_wick']:.6g}\n"
                f"{detail['extreme_diff_label']}: {detail['extreme_diff_pct']:.2f}%\n"
                f"RSI(현재): {detail['cur_rsi']:.2f} / RSI({detail['ref_label']}, {detail['ref_price']:.6g}): {detail['ref_rsi']:.2f}\n"
                f"거래량(참고, 현재): {detail['cur_volume']:.6g} / 중앙값({VOLUME_AVG_LOOKBACK}개): {detail['median_volume']:.6g} (x{detail['volume_ratio_vs_median']:.2f}) / 상대순위: 상위 {100 - detail['volume_percentile']:.0f}%\n"
                f"조건: 최근 {lookback}개 [{timeframe}] 캔들 중 {detail['condition_label']}\n\n"
                f"하성하리아빠 화이팅입니다! 꼭 부자되시고 힘내세요!"
            )
            log.info("신호 발생: %s [%s] %s", symbol, timeframe, sig["name"])
            send_telegram(msg)

        state[key] = latest_candle_ts


def check_all_symbols(exchange: ccxt.okx, symbols, state: dict, timeframes=None):
    """모든 타임프레임 x 모든 신호 조합을 심볼별로 체크

    timeframes: 이번 실행에서 체크할 타임프레임 목록 (기본값: 전체 TIMEFRAMES).
                --group 옵션으로 short/long 그룹만 골라서 넘길 수 있음.

    타임프레임 하나에서 오류(네트워크 순단 등)가 나도 같은 심볼의
    나머지 타임프레임 체크는 계속 진행되도록, try/except를 타임프레임
    단위로 감쌉니다. (심볼 단위로 감싸면 15m에서 에러 시 1h/4h까지
    이번 회차에서 통째로 스킵되는 문제가 있었음)
    """
    if timeframes is None:
        timeframes = TIMEFRAMES
    for symbol in symbols:
        candles_cache = {}
        for tf_conf in timeframes:
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


def resolve_timeframes(group: str):
    """--group 값에 따라 이번 실행에서 체크할 타임프레임 목록을 결정.
    "all"이면 전체, 그 외엔 group 필드가 일치하는 것만."""
    if group == "all":
        return TIMEFRAMES
    return [t for t in TIMEFRAMES if t["group"] == group]


def run_once(group: str = "all"):
    # 외부 스케줄러(cron-job.org)가 정각에 정확히 이 실행을 트리거하므로,
    # 캔들이 거래소에 완전히 반영될 시간을 벌기 위해 여기서
    # CLOSE_BUFFER_SEC(12초)만큼 대기한 뒤 체크를 시작한다.
    log.info("정각 트리거 감지 → %d초 대기 후 체크 시작", CLOSE_BUFFER_SEC)
    time.sleep(CLOSE_BUFFER_SEC)

    timeframes = resolve_timeframes(group)
    if not timeframes:
        log.error("group=%s 에 해당하는 타임프레임이 없습니다.", group)
        return

    state_file = resolve_state_file(group)
    log.info("상태 파일: %s", state_file)

    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state(state_file)
    log.info(
        "1회성 체크 실행 (%d개 심볼, group=%s, 타임프레임: %s, 신호: %s)",
        len(symbols),
        group,
        ", ".join(t["tf"] for t in timeframes),
        ", ".join(s["name"] for s in SIGNALS),
    )
    check_all_symbols(exchange, symbols, state, timeframes)
    save_state(state, state_file)


def run_loop(group: str = "all"):
    timeframes = resolve_timeframes(group)
    if not timeframes:
        log.error("group=%s 에 해당하는 타임프레임이 없습니다.", group)
        return

    state_file = resolve_state_file(group)
    log.info("상태 파일: %s", state_file)

    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    if not symbols:
        log.error("감시할 심볼이 하나도 없습니다.")
        return

    state = load_state(state_file)
    log.info(
        "캔들 감시를 시작합니다. (%d개 심볼, group=%s, 타임프레임: %s, 신호: %s)",
        len(symbols),
        group,
        ", ".join(t["tf"] for t in timeframes),
        ", ".join(s["name"] for s in SIGNALS),
    )

    while True:
        wait_sec = seconds_until_next_close()
        log.info("다음 체크 틱까지 %.0f초 대기", wait_sec)
        time.sleep(wait_sec)
        log.info("틱 발생 → 전 타임프레임/전 신호/전 종목 조건 체크 시작")
        check_all_symbols(exchange, symbols, state, timeframes)
        save_state(state, state_file)


def main():
    parser = argparse.ArgumentParser(description="OKX 캔들 신호 감시 봇 (15m/1h/4h, 역망치음봉/망치양봉 + RSI 다이버전스, 거래량은 참고용 표시만)")
    parser.add_argument("--once", action="store_true", help="1회만 체크하고 종료")
    parser.add_argument(
        "--group",
        choices=["all", "short", "long"],
        default="all",
        help="체크할 타임프레임 그룹 (short=15m, long=1h/4h, all=전체). 기본값 all",
    )
    args = parser.parse_args()

    if args.once:
        run_once(args.group)
    else:
        run_loop(args.group)


if __name__ == "__main__":
    main()
