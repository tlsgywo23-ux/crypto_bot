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
            # 4시간봉(4h) 데이터 가져오기
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
            if not ohlcv or len(ohlcv) < 20:
                continue
                
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # 직전 완성된 캔들 기준 (인덱스 -2 봉)
            idx = -2
            c_open = df['open'].iloc[idx]
            c_high = df['high'].iloc[idx]
            c_low = df['low'].iloc[idx]
            c_close = df['close'].iloc[idx]
            
            # 1. 음봉 확인 (종가가 시가보다 낮음)
            is_bearish = c_close < c_open
            
            # 2. 몸통과 꼬리 정밀 계산 (음봉이므로 몸통 상단은 c_open, 하단은 c_close)
            body_top = c_open
            body_bottom = c_close
            body_size = body_top - body_bottom
            
            if body_size <= 0:
                continue

            upper_wick = c_high - body_top
            lower_wick = body_bottom - c_low
            candle_total_range = c_high - c_low
            
            # [조건 수정] 역망치 정의 강화: 윗꼬리가 몸통보다 길거나 같고(>=), 아랫꼬리는 몸통의 0.5배 이하
            # (만약 윗꼬리가 몸통보다 확실히 길어야 하는 조건을 주려면 upper_wick >= body_size * 1.0 등으로 조절 가능)
            is_inverted_hammer = (upper_wick >= body_size) and (lower_wick <= body_size * 0.5)
            
            # 3. 캔들 전체 길이 0.5% 이상 확인
            current_price = c_close
            min_range_pct = 0.005
            has_enough_range = (candle_total_range / current_price) >= min_range_pct
            
            # 4. 직전 10개 봉 중에서 가장 고점인지 확인 (고가 기준)
            recent_highs = df['high'].iloc[idx-10:idx+1]
            is_highest = c_high >= recent_highs.max()
            
            # 디버깅용 로그 (EDGE 심볼 등의 상태를 확인하기 위함)
            if symbol == 'EDGE/USDT:USDT':
                print(f"[{symbol}] Bearish:{is_bearish}, Hammer:{is_inverted_hammer}, Range:{has_enough_range}, Highest:{is_highest}")
                print(f"-> 윗꼬리:{upper_wick:.4f}, 몸통:{body_size:.4f}, 아랫꼬리:{lower_wick:.4f}")

            # 조건 만족 시 텔레그램 알림 발송
            if is_bearish and is_inverted_hammer and has_enough_range and is_highest:
                wick_pct = ((c_high - c_close) / c_close) * 100
                
                msg = (
                    f"🚨 *[OKX 선물] 4시간봉 역망치 포착!*\n"
                    f"• 코인: `{symbol}`\n"
                    f"• 가격(종가): `{current_price}`\n"
                    f"• 윗꼬리 크기: `종가 ~ 최고가까지 +{wick_pct:.2f}%`\n"
                    f"• 특징: 직전 10개 봉 중 최고점 / 역망치 음봉 / 변동성 0.5% 초과"
                )
                send_telegram_message(msg)
                
        except Exception as e:
            print(f"Error processing {symbol}: {e}")

if __name__ == "__main__":
    check_market()
