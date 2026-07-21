"""
코스피/코스닥 관심 종목의 당일 시가·종가를 텔레그램으로 알림.

필요한 환경변수:
  - TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  - TELEGRAM_CHAT_ID   : 알림을 받을 chat id
  - STOCK_CODES        : 쉼표로 구분된 6자리 종목코드 (예: "005930,035720,000660")

사용 라이브러리: pykrx (KRX 정보데이터시스템 크롤링 래퍼), requests
"""

import os
import sys
from datetime import datetime

import requests
from pykrx import stock


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[에러] 환경변수 {name} 가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    return value


def fetch_today_ohlcv(code: str, date_str: str):
    """해당 종목의 date_str(YYYYMMDD) 기준 최근 거래일 OHLCV 한 줄을 반환."""
    df = stock.get_market_ohlcv(date_str, date_str, code)
    if df.empty:
        # 휴장일이거나 아직 데이터가 없는 경우, 최근 거래일 기준으로 재시도
        df = stock.get_market_ohlcv(
            (datetime.now().replace(day=1)).strftime("%Y%m%d"), date_str, code
        )
        if df.empty:
            return None
        df = df.tail(1)
    return df.iloc[-1]


def build_message(codes: list[str], date_str: str) -> str:
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    lines = [f"📈 {pretty_date} 관심종목 시가/종가"]

    for code in codes:
        name = stock.get_market_ticker_name(code) or code
        row = fetch_today_ohlcv(code, date_str)
        if row is None:
            lines.append(f"\n[{name}({code})] 데이터 없음 (휴장일이거나 조회 실패)")
            continue

        open_price = int(row["시가"])
        close_price = int(row["종가"])
        change_rate = (close_price - open_price) / open_price * 100
        arrow = "🔺" if change_rate > 0 else ("🔻" if change_rate < 0 else "➖")

        lines.append(
            f"\n[{name}({code})]\n"
            f"  시가: {open_price:,}원\n"
            f"  종가: {close_price:,}원\n"
            f"  등락: {arrow} {change_rate:+.2f}%"
        )

    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()


def main():
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_env("TELEGRAM_CHAT_ID")
    codes_raw = get_env("STOCK_CODES")
    codes = [c.strip() for c in codes_raw.split(",") if c.strip()]

    if not codes:
        print("[에러] STOCK_CODES 에 유효한 종목코드가 없습니다.", file=sys.stderr)
        sys.exit(1)

    date_str = datetime.now().strftime("%Y%m%d")
    message = build_message(codes, date_str)
    send_telegram_message(token, chat_id, message)
    print("전송 완료:\n", message)


if __name__ == "__main__":
    main()
