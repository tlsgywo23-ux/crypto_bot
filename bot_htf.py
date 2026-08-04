import os
import ccxt
import pandas as pd
import requests

# 텔레그램 알림 전송 함수
def send_telegram_message(message):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("Telegram environment variables not set!")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send telegram message: {e}")

def check_market():
    # OKX 거래소 객체 생성 (영구선물 swap 마켓 지정 및 레이트 리밋 활성화)
    exchange = ccxt.okx({
        'options': {'defaultType': 'swap'},
        'enableRateLimit': True
    })
    
    # 1. OKX 선물 마켓 정보 미리 로드
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Failed to load OKX markets: {e}")
        return

    # 감시할 원본 코인 심볼 리스트 (동일한 40개 코인)
    raw_symbols = [
        'btc', 'eth', 'zec', 'mu', 'bch', 'link', 'beat', 'sol', 'soxl', 'lab', 
        'near', 'xrp', 'sui', 'ondo', 'wld', 'allo', 'h', 'opn', 'crv', 'doge', 
        'bsb', 'home', 'sahara', 'hmstr', 'trump', 'edge', 'pepe', 'xpl', 'space', 'coai', 
        're', 'ada', 'o', 'based', 'hype', 'slx', 'nes', 'cap', 'litu', 'bnb'
    ]
    
    for s in raw_symbols:
        symbol = f"{s.upper()}/USDT:USDT"
        
        if symbol not in exchange.markets:
            continue

        try:
            # [변경점] 1시간봉(1h) 데이터 가져오기
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
            if not ohlcv or len(ohlcv) < 20:
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 직전 완성된 캔들 기준 (인덱스 -2 봉)
            idx = -2
            c_open = df['open'].iloc[idx]
            c_high = df['high'].iloc[idx]
            c_low = df['low'].iloc[idx]
            c_close = df['close'].iloc[idx]
            
            # 1. 음봉 확인
            is_bearish = c_close < c_open
            
            # 2. 몸통과 꼬리 계산
            body_size = abs(c_close - c_open)
            upper_wick = c_high - c_open
            lower_wick = c_close - c_low
            candle_total_range = c_high - c_low
            
            if body_size == 0:
                continue

            # 역망치 조건
            is_inverted_hammer = (upper_wick >= body_size * 2) and (lower_wick <= body_size * 0.5)
            
            # 3. 캔들 전체 길이 0.5% 이상 확인
            current_price = c_close
            min_range_pct = 0.005
            has_enough_range = (candle_total_range / current_price) >= min_range_pct
            
            # 4. 직전 10개 봉 중에서 가장 고점인지 확인
            recent_highs = df['high'].iloc[idx-10:idx]
            is_highest = c_high >= recent_highs.max()
            
            # 조건 만족 시 텔레그램 알림 발송
            if is_bearish and is_inverted_hammer and has_enough_range and is_highest:
                wick_pct = ((c_high - c_close) / c_close) * 100
                
                msg = (
                    f"🚨 *[OKX 선물] 1시간봉 역망치 포착!*\n"
                    f"• 코인: `{symbol}`\n"
                    f"• 가격(종가): `{current_price}`\n"
                    f"• 윗꼬리 크기: `종가 ~ 최고가까지 +{wick_pct:.2f}%`\n"
                    f"• 특징: 직전 10개 봉 중 최고점 / 윗꼬리 2배 이상 / 변동성 0.5% 초과 음봉"
                )
                send_telegram_message(msg)
                
        except Exception as e:
            print(f"Error processing {symbol}: {e}")

if __name__ == "__main__":
    check_market()
