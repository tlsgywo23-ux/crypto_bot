"""
OKX 캔들 신호 백테스트 스크립트
- bot.py에 있는 신호 판단 로직(check_inverted_hammer_bearish, check_hammer_bullish)을
  그대로 가져와서 씁니다. 로직을 따로 베끼지 않기 때문에, bot.py의 조건을 바꾸면
  백테스트에도 자동으로 반영됩니다.
- 최근 BACKTEST_MONTHS 개월치 15m/1h/4h 캔들에서 신호가 발생했던 모든 지점을 찾고,
  신호 발생 이후 FOLLOW_CANDLES개 캔들 동안의 가격 움직임으로 성공/실패를 판정합니다.
- 결과는 엑셀 파일(backtest_result.xlsx)로 저장하고, 요약은 텔레그램으로도 전송합니다.
"""

import datetime
import time
import logging

import pandas as pd
import requests

from bot import (
    build_exchange, resolve_symbols, RAW_SYMBOLS, SYMBOL_RANK,
    TIMEFRAMES, RSI_PERIOD, RSI_WARMUP_BARS,
    compute_rsi, SIGNALS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KST,
)

# ============================== CONFIG ==============================

BACKTEST_MONTHS = 6      # 몇 개월치 과거 데이터를 볼지
FOLLOW_CANDLES = 5        # 신호 발생 이후 몇 개 캔들까지의 움직임으로 성공/실패 판정할지
OUTPUT_XLSX = "backtest_result.xlsx"

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


# ============================== OUTCOME 계산 ==============================


def evaluate_outcome(candles, signal_idx, direction, follow_n):
    """signal_idx 캔들의 종가를 진입가로 보고, 이후 follow_n개 캔들 동안의 결과를 계산.
    데이터가 아직 부족(최근 신호라 미래 캔들이 안 쌓임)하면 None을 반환."""
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


# ============================== 메인 백테스트 루프 ==============================


def run_backtest() -> pd.DataFrame:
    exchange = build_exchange()
    symbols = resolve_symbols(exchange, RAW_SYMBOLS)

    rows = []
    for symbol in symbols:
        raw_symbol = symbol.split("/")[0]
        rank = SYMBOL_RANK.get(raw_symbol)

        for tf_conf in TIMEFRAMES:
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
            for i in range(min_needed, len(candles) - FOLLOW_CANDLES):
                window = candles[i - lookback + 1: i + 1]
                rsi_window = rsi_series[i - lookback + 1: i + 1]

                for sig in SIGNALS:
                    ok, detail = sig["fn"](window, rsi_window, lookback)
                    if not ok:
                        continue

                    direction = "short" if sig["id"] == "inv_hammer_bear" else "long"
                    outcome = evaluate_outcome(candles, i, direction, FOLLOW_CANDLES)
                    if outcome is None:
                        continue

                    rows.append({
                        "종목": raw_symbol,
                        "순번": rank,
                        "타임프레임": timeframe,
                        "신호": sig["name"],
                        "마감시각(KST)": datetime.datetime.fromtimestamp(
                            candles[i][0] / 1000, tz=KST
                        ).strftime("%Y-%m-%d %H:%M"),
                        "진입가": candles[i][4],
                        "수익률(%)": round(outcome["pnl_pct"], 2),
                        "최대유리(%)": round(outcome["best_pct"], 2),
                        "최대불리(%)": round(outcome["worst_pct"], 2),
                        "성공여부": "성공" if outcome["success"] else "실패",
                    })

            log.info("[%s|%s] 누적 신호 %d건", raw_symbol, timeframe, len(rows))

    return pd.DataFrame(rows)


# ============================== 요약 생성 ==============================


def build_summary(df: pd.DataFrame) -> str:
    if df.empty:
        return f"📊 백테스트 결과: 지난 {BACKTEST_MONTHS}개월 동안 조건을 만족한 신호가 하나도 없었습니다."

    total = len(df)
    win_rate = (df["성공여부"] == "성공").mean() * 100
    avg_pnl = df["수익률(%)"].mean()

    lines = [
        f"📊 백테스트 결과 요약",
        f"(최근 {BACKTEST_MONTHS}개월, 신호 후 {FOLLOW_CANDLES}개 캔들 기준)",
        "",
        f"전체 신호 수: {total}건",
        f"전체 승률: {win_rate:.1f}%",
        f"전체 평균 수익률: {avg_pnl:.2f}%",
        "",
        "── 타임프레임별 ──",
    ]
    for tf, g in df.groupby("타임프레임"):
        lines.append(
            f"[{tf}] {len(g)}건 / 승률 {(g['성공여부']=='성공').mean()*100:.1f}% / 평균 {g['수익률(%)'].mean():.2f}%"
        )

    lines.append("")
    lines.append("── 신호 유형별 ──")
    for sig, g in df.groupby("신호"):
        lines.append(
            f"{sig}: {len(g)}건 / 승률 {(g['성공여부']=='성공').mean()*100:.1f}% / 평균 {g['수익률(%)'].mean():.2f}%"
        )

    lines.append("")
    lines.append("── 신호 많은 상위 5개 종목 ──")
    top_symbols = df.groupby("종목").size().sort_values(ascending=False).head(5)
    for sym, cnt in top_symbols.items():
        g = df[df["종목"] == sym]
        lines.append(f"{sym}: {cnt}건 / 승률 {(g['성공여부']=='성공').mean()*100:.1f}%")

    lines.append("")
    lines.append("(종목·시각별 상세 내역은 첨부된 엑셀 파일 참고)")
    return "\n".join(lines)


# ============================== MAIN ==============================


def build_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """종목 x 타임프레임 x 신호유형 조합별로 승률/평균 수익률을 집계한 표"""
    if df.empty:
        return pd.DataFrame(columns=["종목", "순번", "타임프레임", "신호", "신호건수", "승률(%)", "평균수익률(%)"])

    rows = []
    for (symbol, rank, tf, sig), g in df.groupby(["종목", "순번", "타임프레임", "신호"]):
        rows.append({
            "종목": symbol,
            "순번": rank,
            "타임프레임": tf,
            "신호": sig,
            "신호건수": len(g),
            "승률(%)": round((g["성공여부"] == "성공").mean() * 100, 1),
            "평균수익률(%)": round(g["수익률(%)"].mean(), 2),
        })

    breakdown = pd.DataFrame(rows)
    # 신호건수 많은 순 -> 승률 높은 순으로 정렬해서 보기 편하게
    return breakdown.sort_values(by=["신호건수", "승률(%)"], ascending=[False, False]).reset_index(drop=True)


def main():
    log.info("백테스트 시작 (최근 %d개월, 신호 후 %d개 캔들 추적)", BACKTEST_MONTHS, FOLLOW_CANDLES)
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
