"""
OKX 캔들 신호 백테스트 스크립트
- bot.py에 있는 신호 판단 로직(check_inverted_hammer_bearish, check_hammer_bullish)을
  그대로 가져와서 씁니다. 로직을 따로 베끼지 않기 때문에, bot.py의 조건을 바꾸면
  백테스트에도 자동으로 반영됩니다.
- 최근 BACKTEST_MONTHS 개월치 캔들에서 신호가 발생했던 모든 지점을 찾고, 두 가지
  방식으로 결과를 판정합니다.

  [방식 1] 고정 N봉 방식 (FOLLOW_CANDLES_LIST)
    신호 후 정확히 N개 캔들이 지난 시점의 종가로 손익을 계산.
    5봉, 10봉처럼 여러 개를 넣으면 각각 결과가 나옵니다.

  [방식 2] TP/SL 방식 (TP_PCT / SL_PCT)
    진입가 대비 진입 방향으로 TP_PCT%만큼 가면 익절, 반대로 SL_PCT%만큼 가면
    손절로 보고, 신호 이후 캔들을 하나씩 따라가며 먼저 닿는 쪽으로 판정.
    한 캔들 안에서 TP/SL이 동시에 걸릴 수 있는 경우(그 캔들의 고가는 TP 위,
    저가는 SL 아래) OHLC 데이터만으로는 어느 게 먼저 찍혔는지 알 수 없어서,
    보수적으로 SL이 먼저 걸린 것으로 간주합니다. TP_SL_MAX_FOLLOW 캔들
    안에 둘 다 안 걸리면 "미결정"으로 분류해서 승률 계산에서 제외합니다.

  두 방식 모두 같은 신호 지점에서 계산되고, 결과 표에는 "추적기준" 컬럼으로
  구분됩니다 (예: "5봉", "10봉", "TP5%/SL2.5%").
- 결과는 엑셀 파일(backtest_result.xlsx)로 저장하고, 요약은 텔레그램으로도 전송합니다.

[타임프레임 관련]
- 감시할 타임프레임(TIMEFRAMES)은 실전 봇(bot.py)이 쓰는 값이라 여기서는 가져오지
  않습니다. 대신 아래 BACKTEST_TIMEFRAMES를 따로 두고 백테스트는 이걸 사용합니다.
  → 백테스트에서 12h/1d처럼 실전에는 안 쓰는 타임프레임의 승률만 확인해보고
  싶을 때, bot.py(실전 봇)는 전혀 건드리지 않고 이 파일에서만 추가/제거하면 됩니다.

[수익률(%) 컬럼 관련]
- "수익률(%)"은 진입 방향(롱/숏) 기준으로 계산됩니다. 신호가 기대한 방향으로
  가격이 움직였으면 +, 반대 방향으로 움직였으면 - 입니다.
  (역망치 음봉=숏 기대: 가격 하락 시 +, 망치 양봉=롱 기대: 가격 상승 시 +)
"""

import datetime
import time
import logging

import pandas as pd
import requests

from bot import (
    build_exchange, resolve_symbols, RAW_SYMBOLS, SYMBOL_RANK,
    RSI_PERIOD, RSI_WARMUP_BARS,
    compute_rsi, SIGNALS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KST,
)

# ============================== CONFIG ==============================

BACKTEST_MONTHS = 6      # 몇 개월치 과거 데이터를 볼지

# [방식 1] 신호 발생 이후 몇 개 캔들까지의 움직임으로 성공/실패를 판정할지.
# 여러 개를 넣으면 각각에 대한 결과를 한 번의 백테스트로 같이 뽑습니다.
FOLLOW_CANDLES_LIST = [5, 10]

# [방식 2] TP/SL 방식 설정
TP_PCT = 5.0    # 진입 방향으로 이만큼(%) 가면 익절
SL_PCT = 2.5    # 진입 반대 방향으로 이만큼(%) 가면 손절
TP_SL_MAX_FOLLOW = 50   # 이 캔들 수 안에 TP/SL 둘 다 안 걸리면 "미결정"으로 분류

OUTPUT_XLSX = "backtest_result.xlsx"

# 백테스트에서 확인해볼 타임프레임 목록.
# 실전 봇(bot.py)의 TIMEFRAMES와 별개로 관리되므로, 여기서 12h/1d를
# 추가하거나 빼도 실전 감시 대상에는 영향이 없습니다.
BACKTEST_TIMEFRAMES = [
    {"tf": "15m", "ms": 15 * 60 * 1000, "lookback": 30},
    {"tf": "1h", "ms": 60 * 60 * 1000, "lookback": 30},
    {"tf": "4h", "ms": 4 * 60 * 60 * 1000, "lookback": 30},
    {"tf": "12h", "ms": 12 * 60 * 60 * 1000, "lookback": 30},
    {"tf": "1d", "ms": 24 * 60 * 60 * 1000, "lookback": 30},
]

TP_SL_LABEL = f"TP{TP_PCT:g}%/SL{SL_PCT:g}%"

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ============================== TELEGRAM (파일 첨부) ==============================


def send_telegram_text(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("텔레그램 텍스트 전송 실패: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("텔레그램 텍스트 전송 중 예외: %s", e)


def send_telegram_document(file_path: str, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=60,
            )
        if resp.status_code != 200:
            log.error("텔레그램 파일 전송 실패: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("텔레그램 파일 전송 중 예외: %s", e)


# ============================== DATA FETCH ==============================


def fetch_full_history(exchange, symbol, timeframe, timeframe_ms, months):
    """OKX API의 1회 조회 개수 제한(약 300개)을 우회해, 지정 기간치를 전부 모아온다."""
    since = exchange.milliseconds() - months * 30 * 24 * 60 * 60 * 1000
    all_candles = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=300)
        if not batch:
            break
        all_candles.extend(batch)
        next_since = batch[-1][0] + timeframe_ms
        if next_since <= since or len(batch) < 300:
            break
        since = next_since
        time.sleep(exchange.rateLimit / 1000)

    dedup = {c[0]: c for c in all_candles}
    return [dedup[k] for k in sorted(dedup.keys())]


# ============================== OUTCOME 계산 (방식 1: 고정 N봉) ==============================


def evaluate_outcome(candles, signal_idx, direction, follow_n):
    """signal_idx 캔들의 종가를 진입가로 보고, 이후 follow_n개 캔들 동안의 결과를 계산.
    데이터가 아직 부족(최근 신호라 미래 캔들이 안 쌓임)하면 None을 반환.

    수익률(pnl_pct)은 진입 방향 기준입니다: 신호가 기대한 방향대로 가격이
    움직였으면 +, 반대로 움직였으면 - 입니다."""
    entry_price = candles[signal_idx][4]
    future = candles[signal_idx + 1: signal_idx + 1 + follow_n]
    if len(future) < follow_n:
        return None

    final_price = future[-1][4]
    highs = [c[2] for c in future]
    lows = [c[3] for c in future]

    if direction == "short":  # 역망치 음봉 = 하락 기대
        pnl_pct = (entry_price - final_price) / entry_price * 100
        best_pct = (entry_price - min(lows)) / entry_price * 100
        worst_pct = (entry_price - max(highs)) / entry_price * 100
    else:  # 망치 양봉 = 상승 기대
        pnl_pct = (final_price - entry_price) / entry_price * 100
        best_pct = (max(highs) - entry_price) / entry_price * 100
        worst_pct = (min(lows) - entry_price) / entry_price * 100

    return {
        "pnl_pct": pnl_pct,
        "best_pct": best_pct,
        "worst_pct": worst_pct,
        "success": pnl_pct > 0,
    }


# ============================== OUTCOME 계산 (방식 2: TP/SL) ==============================


def evaluate_tp_sl_outcome(candles, signal_idx, direction, tp_pct, sl_pct, max_follow):
    """signal_idx 캔들의 종가를 진입가로 보고, 이후 캔들을 하나씩 따라가며
    TP(익절)와 SL(손절) 중 어느 쪽을 먼저 건드리는지 판정.

    - 롱(direction="long"): 진입가 대비 +tp_pct%면 익절, -sl_pct%면 손절
    - 숏(direction="short"): 진입가 대비 -tp_pct%면 익절, +sl_pct%면 손절
    - 한 캔들 안에서 TP/SL이 동시에 걸릴 수 있는 경우(고가는 TP 이상, 저가는
      SL 이하) 캔들 내부 체결 순서는 알 수 없으므로 보수적으로 SL이 먼저
      걸린 것으로 간주합니다.
    - max_follow 캔들 안에 TP도 SL도 안 걸리면 result=None (미결정) 반환.
    - 데이터 자체가 부족해서 판정 불가한 경우도 result=None."""
    entry_price = candles[signal_idx][4]

    if direction == "long":
        tp_price = entry_price * (1 + tp_pct / 100)
        sl_price = entry_price * (1 - sl_pct / 100)
    else:
        tp_price = entry_price * (1 - tp_pct / 100)
        sl_price = entry_price * (1 + sl_pct / 100)

    future = candles[signal_idx + 1: signal_idx + 1 + max_follow]

    for offset, cd in enumerate(future, start=1):
        high, low = cd[2], cd[3]

        if direction == "long":
            hit_tp = high >= tp_price
            hit_sl = low <= sl_price
        else:
            hit_tp = low <= tp_price
            hit_sl = high >= sl_price

        if hit_tp and hit_sl:
            # 같은 캔들 안에서 둘 다 걸림 → 보수적으로 SL 먼저 걸린 것으로 간주
            return {"result": "SL", "pnl_pct": -sl_pct, "bars_to_result": offset, "success": False}
        if hit_sl:
            return {"result": "SL", "pnl_pct": -sl_pct, "bars_to_result": offset, "success": False}
        if hit_tp:
            return {"result": "TP", "pnl_pct": tp_pct, "bars_to_result": offset, "success": True}

    return None  # max_follow 안에 미결정 (또는 데이터 부족)


# ============================== 메인 백테스트 루프 ==============================


def run_backtest() -> pd.DataFrame:
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)
    max_fixed_follow = max(FOLLOW_CANDLES_LIST)

    rows = []
    tpsl_undetermined_count = 0

    for symbol in symbols:
        raw_symbol = symbol.split("/")[0]
        rank = SYMBOL_RANK.get(raw_symbol)

        for tf_conf in BACKTEST_TIMEFRAMES:
            timeframe = tf_conf["tf"]
            timeframe_ms = tf_conf["ms"]
            lookback = tf_conf["lookback"]

            log.info("수집 중: %s [%s] (최근 %d개월)", raw_symbol, timeframe, BACKTEST_MONTHS)
            try:
                candles = fetch_full_history(exchange, symbol, timeframe, timeframe_ms, BACKTEST_MONTHS)
            except Exception as e:
                log.error("[%s|%s] 캔들 수집 실패: %s", raw_symbol, timeframe, e)
                continue

            closes = [c[4] for c in candles]
            rsi_series = compute_rsi(closes, RSI_PERIOD)

            min_needed = lookback + RSI_PERIOD + RSI_WARMUP_BARS
            # TP/SL 판정에 필요한 여유분까지 감안해서 뒤쪽 여백을 잡음
            tail_margin = max(max_fixed_follow, TP_SL_MAX_FOLLOW)
            for i in range(min_needed, len(candles) - tail_margin):
                window = candles[i - lookback + 1: i + 1]
                rsi_window = rsi_series[i - lookback + 1: i + 1]

                for sig in SIGNALS:
                    ok, detail = sig["fn"](window, rsi_window, lookback)
                    if not ok:
                        continue

                    direction = "short" if sig["id"] == "inv_hammer_bear" else "long"
                    candle_time_str = datetime.datetime.fromtimestamp(
                        candles[i][0] / 1000, tz=KST
                    ).strftime("%Y-%m-%d %H:%M")

                    base_row = {
                        "종목": raw_symbol,
                        "순번": rank,
                        "타임프레임": timeframe,
                        "신호": sig["name"],
                        "마감시각(KST)": candle_time_str,
                        "진입가": candles[i][4],
                    }

                    # 방식 1: 고정 N봉
                    for follow_n in FOLLOW_CANDLES_LIST:
                        outcome = evaluate_outcome(candles, i, direction, follow_n)
                        if outcome is None:
                            continue
                        rows.append({
                            **base_row,
                            "추적기준": f"{follow_n}봉",
                            "수익률(%)": round(outcome["pnl_pct"], 2),
                            "최대유리(%)": round(outcome["best_pct"], 2),
                            "최대불리(%)": round(outcome["worst_pct"], 2),
                            "체결까지_걸린봉수": None,
                            "성공여부": "성공" if outcome["success"] else "실패",
                        })

                    # 방식 2: TP/SL
                    tpsl = evaluate_tp_sl_outcome(candles, i, direction, TP_PCT, SL_PCT, TP_SL_MAX_FOLLOW)
                    if tpsl is None:
                        tpsl_undetermined_count += 1
                    else:
                        rows.append({
                            **base_row,
                            "추적기준": TP_SL_LABEL,
                            "수익률(%)": round(tpsl["pnl_pct"], 2),
                            "최대유리(%)": None,
                            "최대불리(%)": None,
                            "체결까지_걸린봉수": tpsl["bars_to_result"],
                            "성공여부": "성공" if tpsl["success"] else "실패",
                        })

            log.info("[%s|%s] 누적 결과행 %d건", raw_symbol, timeframe, len(rows))

    if tpsl_undetermined_count:
        log.info(
            "TP/SL 방식에서 %d건은 %d봉 안에 결정 안 나서 미결정 처리(승률 계산 제외)",
            tpsl_undetermined_count, TP_SL_MAX_FOLLOW,
        )

    return pd.DataFrame(rows)


# ============================== 요약 생성 ==============================


def build_summary(df: pd.DataFrame) -> str:
    if df.empty:
        return f"📊 백테스트 결과: 지난 {BACKTEST_MONTHS}개월 동안 조건을 만족한 신호가 하나도 없었습니다."

    labels = [f"{n}봉" for n in FOLLOW_CANDLES_LIST] + [TP_SL_LABEL]

    lines = [
        f"📊 백테스트 결과 요약",
        f"(최근 {BACKTEST_MONTHS}개월, 수익률은 진입방향 기준 +/-)",
    ]

    for label in labels:
        sub = df[df["추적기준"] == label]
        lines.append("")
        lines.append(f"════ {label} 기준 ════")
        if sub.empty:
            lines.append("해당 없음")
            continue

        total = len(sub)
        win_rate = (sub["성공여부"] == "성공").mean() * 100
        avg_pnl = sub["수익률(%)"].mean()

        lines.append(f"전체 신호 수: {total}건")
        lines.append(f"전체 승률: {win_rate:.1f}%")
        lines.append(f"전체 평균 수익률: {avg_pnl:.2f}%")

        lines.append("── 타임프레임별 ──")
        for tf, g in sub.groupby("타임프레임"):
            lines.append(
                f"[{tf}] {len(g)}건 / 승률 {(g['성공여부']=='성공').mean()*100:.1f}% / 평균 {g['수익률(%)'].mean():.2f}%"
            )

        lines.append("── 신호 유형별 ──")
        for sig, g in sub.groupby("신호"):
            lines.append(
                f"{sig}: {len(g)}건 / 승률 {(g['성공여부']=='성공').mean()*100:.1f}% / 평균 {g['수익률(%)'].mean():.2f}%"
            )

    lines.append("")
    lines.append(f"※ {TP_SL_LABEL} 기준은 진입방향 기준 +{TP_PCT:g}% 도달 시 익절, -{SL_PCT:g}% 도달 시 손절로 판정했습니다.")
    lines.append("(종목·시각별 상세 내역은 첨부된 엑셀 파일 참고)")
    return "\n".join(lines)


# ============================== MAIN ==============================


def build_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """종목 x 타임프레임 x 신호유형 x 추적기준 조합별로 승률/평균 수익률을 집계한 표"""
    if df.empty:
        return pd.DataFrame(columns=[
            "종목", "순번", "타임프레임", "신호", "추적기준",
            "신호건수", "승률(%)", "평균수익률(%)",
        ])

    rows = []
    for (symbol, rank, tf, sig, label), g in df.groupby(["종목", "순번", "타임프레임", "신호", "추적기준"]):
        rows.append({
            "종목": symbol,
            "순번": rank,
            "타임프레임": tf,
            "신호": sig,
            "추적기준": label,
            "신호건수": len(g),
            "승률(%)": round((g["성공여부"] == "성공").mean() * 100, 1),
            "평균수익률(%)": round(g["수익률(%)"].mean(), 2),
        })

    breakdown = pd.DataFrame(rows)
    # 추적기준 -> 신호건수 많은 순 -> 승률 높은 순으로 정렬해서 보기 편하게
    return breakdown.sort_values(
        by=["추적기준", "신호건수", "승률(%)"], ascending=[True, False, False]
    ).reset_index(drop=True)


def main():
    log.info(
        "백테스트 시작 (최근 %d개월, 고정봉: %s, TP/SL: +%.1f%%/-%.1f%%)",
        BACKTEST_MONTHS, FOLLOW_CANDLES_LIST, TP_PCT, SL_PCT,
    )
    df = run_backtest()

    breakdown_df = build_breakdown(df)
    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
        breakdown_df.to_excel(writer, sheet_name="종목별_요약", index=False)
        df.to_excel(writer, sheet_name="상세내역", index=False)
    log.info("엑셀 저장 완료: %s (상세 %d행, 요약 %d행)", OUTPUT_XLSX, len(df), len(breakdown_df))

    summary = build_summary(df)
    log.info("텔레그램으로 요약 + 엑셀 파일 전송")
    send_telegram_text(summary)
    if not df.empty:
        send_telegram_document(OUTPUT_XLSX, caption="백테스트 상세 결과")


if __name__ == "__main__":
    main()
