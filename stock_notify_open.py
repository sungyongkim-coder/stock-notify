"""
[오전 개장용] 네이버 금융에서 관심 종목의 '오늘 시초가'와 현재가를 조회해
텔레그램으로 알림. 개장 직후(예: 09:05) 실행을 가정한다.

필요한 환경변수:
  - TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  - TELEGRAM_CHAT_ID   : 알림을 받을 chat id
  - STOCK_CODES        : 쉼표로 구분된 6자리 종목코드

주의:
  - 네이버 금융은 비공식 소스이므로 응답 형식이 예고 없이 바뀔 수 있다.
    형식이 바뀌면 이 스크립트만 실패하고, 오후 KRX 알림(stock_notify.py)은
    영향을 받지 않는다.
  - 여기서의 '현재가'는 실행 시점의 장중 가격이며, 개장 직후에는 시초가와
    거의 같다. 등락률은 '전일 종가 대비 현재가' 기준이다.
"""

import os
import sys
import requests

# 네이버 차트 JSON (일봉). 당일 봉은 [날짜, 시가, 고가, 저가, 현재가, 거래량, ...]
CHART_URL = (
    "https://fchart.stock.naver.com/siseJson.nhn"
    "?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}
ALERT_THRESHOLD = 5.0


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[에러] 환경변수 {name} 가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    return value


def parse_sise_json(text: str):
    """siseJson 응답(자바스크립트 배열 형태 문자열)을 파이썬 리스트로 파싱."""
    import ast

    # 응답은 대략:
    # [['날짜','시가','고가','저가','종가','거래량','외국인비율'],
    #  ["20260721", 71000, 71500, 70800, 71200, 1234567, 51.2], ... ]
    # 작은따옴표/줄바꿈이 섞여 있어 ast.literal_eval 로 안전 파싱
    cleaned = text.strip()
    try:
        rows = ast.literal_eval(cleaned)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def fetch_today_and_prev(code: str):
    """(오늘봉, 전일봉) 튜플을 반환. 실패 시 (None, None)."""
    from datetime import datetime, timedelta

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
    url = CHART_URL.format(code=code, start=start, end=end)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[경고] {code} 네이버 조회 실패: {e}", file=sys.stderr)
        return None, None

    rows = parse_sise_json(r.text)
    # 첫 행은 헤더(['날짜','시가',...])이므로 데이터 행만 추림
    data_rows = [row for row in rows if row and str(row[0]).isdigit()]
    if not data_rows:
        return None, None

    today = data_rows[-1]
    prev = data_rows[-2] if len(data_rows) >= 2 else None
    return today, prev


def parse_codes(codes_raw: str):
    """
    STOCK_CODES 를 (코드, 이름) 리스트로 파싱.
    형식: "005930:삼성전자,035720:카카오,000660"
    이름을 생략하면(콜론 없음) 이름 자리에 코드를 그대로 사용.
    """
    result = []
    for token in codes_raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            code, name = token.split(":", 1)
            result.append((code.strip(), name.strip() or code.strip()))
        else:
            result.append((token, token))
    return result


def build_message(pairs) -> str:
    from datetime import datetime

    lines = [f"🔔 {datetime.now():%Y-%m-%d} 개장 시초가 (네이버)"]
    alerts = []

    for code, name in pairs:
        today, prev = fetch_today_and_prev(code)
        label = f"{name}({code})" if name != code else code
        if today is None:
            lines.append(f"\n[{label}] 데이터 없음 (조회 실패/휴장 가능)")
            continue

        # today = [날짜, 시가, 고가, 저가, 현재가, 거래량, ...]
        try:
            open_price = float(today[1])
            cur_price = float(today[4])
        except (IndexError, ValueError, TypeError):
            lines.append(f"\n[{label}] 응답 형식이 예상과 달라 파싱 실패: {today}")
            continue

        prev_close = float(prev[4]) if prev and len(prev) > 4 else None
        base = prev_close if prev_close else open_price
        change_rate = (cur_price - base) / base * 100 if base else 0
        arrow = "🔺" if change_rate > 0 else ("🔻" if change_rate < 0 else "➖")

        entry = (
            f"\n[{label}]\n"
            f"  시초가: {int(open_price):,}원\n"
            f"  현재가: {int(cur_price):,}원\n"
            f"  전일대비: {arrow} {change_rate:+.2f}%"
        )
        lines.append(entry)

        if abs(change_rate) >= ALERT_THRESHOLD:
            alerts.append(f"  {arrow} {label} {change_rate:+.2f}%")

    if alerts:
        lines.append(f"\n⚠️ 전일 대비 {ALERT_THRESHOLD:.0f}% 이상 변동")
        lines.extend(alerts)

    return "\n".join(lines)


def send_telegram_message(token, chat_id, text) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


def main():
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_env("TELEGRAM_CHAT_ID")
    codes_raw = get_env("STOCK_CODES")
    pairs = parse_codes(codes_raw)

    if not pairs:
        print("[에러] STOCK_CODES 에 유효한 종목코드가 없습니다.", file=sys.stderr)
        sys.exit(1)

    message = build_message(pairs)
    send_telegram_message(token, chat_id, message)
    print("전송 완료:\n", message)


if __name__ == "__main__":
    main()
