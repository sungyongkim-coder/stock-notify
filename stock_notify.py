"""
코스피/코스닥 관심 종목의 당일 시가·종가 및 5% 이상 변동 여부를 텔레그램으로 알림.

필요한 환경변수:
  - TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  - TELEGRAM_CHAT_ID   : 알림을 받을 chat id
  - KRX_OPENAPI_KEY    : KRX 공식 Open API 인증키 (openapi.krx.co.kr 에서 발급)
  - STOCK_CODES        : 쉼표로 구분된 6자리 종목코드 (예: "005930,035720,000660")

동작:
  장 마감 후 그날 확정된 일별매매정보를 받아
  1) 시초가, 2) 종가, 3) 시가 대비 등락률을 정리하고,
  등락률이 ±5% 이상이면 별도 강조하여 알림을 보낸다.

주의:
  이 KRX Open API는 '장중 실시간' 시세를 제공하지 않으므로,
  '장중 5% 변동 즉시 알림'은 불가능하다. 여기서의 5% 판정은
  '그날 시가 대비 종가 변동'을 장 마감 후에 계산한 것이다.

라이브러리 응답 형태: {"OutBlock_1": [ {필드명: 값, ...}, ... ]}
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests
from pykrx_openapi import KRXOpenAPI, KRXAuthenticationError, KRXNetworkError

# 요청 타임아웃(초)과 재시도 횟수
REQUEST_TIMEOUT = 120
MAX_RETRIES = 2
# 5% 이상 변동을 '주목' 대상으로 표시
ALERT_THRESHOLD = 5.0

CODE_KEYS = ["ISU_SRT_CD", "ISU_CD", "SHORT_CD", "단축코드", "종목코드"]
NAME_KEYS = ["ISU_ABBRV", "ISU_NM", "ISU_NAME", "종목명"]
OPEN_KEYS = ["TDD_OPNPRC", "OPNPRC", "시가"]
CLOSE_KEYS = ["TDD_CLSPRC", "CLSPRC", "종가"]
VOLUME_KEYS = ["ACC_TRDVOL", "TRDVOL", "거래량"]
VALUE_KEYS = ["ACC_TRDVAL", "TRDVAL", "거래대금"]


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[에러] 환경변수 {name} 가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    return value


def parse_codes(codes_raw: str):
    """
    STOCK_CODES 를 (코드, 사용자지정이름) 리스트로 파싱.
    형식: "005930:삼성전자,035720:카카오,000660"
    이름을 생략하면 None (→ KRX 응답의 종목명을 사용).
    """
    result = []
    for token in codes_raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            code, name = token.split(":", 1)
            result.append((code.strip(), name.strip() or None))
        else:
            result.append((token, None))
    return result


def pick(record: dict, candidates):
    for key in candidates:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def get_records(resp) -> list:
    if isinstance(resp, dict):
        return resp.get("OutBlock_1", []) or []
    if isinstance(resp, list):
        return resp
    return []


def call_with_retry(func, bas_dd):
    """타임아웃/네트워크 오류에 대비해 몇 번 재시도한다."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(bas_dd=bas_dd)
        except KRXNetworkError as e:
            print(f"[경고] {bas_dd} 조회 {attempt}/{MAX_RETRIES} 실패: {e}",
                  file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(3)
    return None


def find_record(records: list, code: str):
    for rec in records:
        val = pick(rec, CODE_KEYS)
        if val is not None and code in str(val):
            return rec
    return None


def build_message(pairs, date_str, kospi_records, kosdaq_records) -> str:
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    header = [f"📈 {pretty_date} 관심종목 시가/종가"]
    body = []
    alerts = []

    for code, user_name in pairs:
        rec = find_record(kospi_records, code)
        market = "KOSPI"
        if rec is None:
            rec = find_record(kosdaq_records, code)
            market = "KOSDAQ"
        if rec is None:
            label = f"{user_name}({code})" if user_name else code
            body.append(f"\n[{label}] 데이터 없음 (휴장일이거나 종목코드 확인 필요)")
            continue

        # 사용자가 이름을 지정했으면 그걸 우선, 없으면 KRX 응답의 종목명 사용
        name = user_name or pick(rec, NAME_KEYS) or code
        open_price = pick(rec, OPEN_KEYS)
        close_price = pick(rec, CLOSE_KEYS)
        volume = pick(rec, VOLUME_KEYS)
        value = pick(rec, VALUE_KEYS)

        if open_price is None or close_price is None:
            body.append(
                f"\n[{name}({code})] 시가/종가 필드를 찾지 못했습니다.\n"
                f"  (원본 필드: {list(rec.keys())})"
            )
            continue

        open_price, close_price = float(open_price), float(close_price)
        change_rate = (close_price - open_price) / open_price * 100 if open_price else 0
        arrow = "🔺" if change_rate > 0 else ("🔻" if change_rate < 0 else "➖")

        entry = (
            f"\n[{name}({code}) · {market}]\n"
            f"  시초가: {int(open_price):,}원\n"
            f"  종가: {int(close_price):,}원\n"
            f"  등락: {arrow} {change_rate:+.2f}%"
        )
        if volume is not None:
            entry += f"\n  거래량: {int(float(volume)):,}주"
        if value is not None:
            # 거래대금은 억 단위로 환산해 보기 쉽게 표시
            eok = float(value) / 1_0000_0000
            entry += f"\n  거래대금: {eok:,.1f}억원"
        body.append(entry)

        if abs(change_rate) >= ALERT_THRESHOLD:
            alerts.append(f"  {arrow} {name}({code}) {change_rate:+.2f}%")

    lines = header + body
    if alerts:
        lines.append(f"\n⚠️ 시가 대비 {ALERT_THRESHOLD:.0f}% 이상 변동")
        lines.extend(alerts)

    return "\n".join(lines)


def send_telegram_message(token, chat_id, text) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


def main():
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_env("TELEGRAM_CHAT_ID")
    api_key = get_env("KRX_OPENAPI_KEY")
    codes_raw = get_env("STOCK_CODES")
    pairs = parse_codes(codes_raw)

    if not pairs:
        print("[에러] STOCK_CODES 에 유효한 종목코드가 없습니다.", file=sys.stderr)
        sys.exit(1)

    try:
        client = KRXOpenAPI(api_key=api_key, timeout=REQUEST_TIMEOUT)
    except KRXAuthenticationError:
        print("[에러] KRX_OPENAPI_KEY 가 유효하지 않습니다.", file=sys.stderr)
        sys.exit(1)

    # 오늘부터 최대 5일 과거로 거슬러 올라가며 가장 최근 영업일 데이터를 찾는다.
    date_str = None
    kospi_records, kosdaq_records = [], []
    for i in range(5):
        candidate = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        kospi_resp = call_with_retry(client.get_stock_daily_trade, candidate)
        kospi_records = get_records(kospi_resp)
        if kospi_records:
            date_str = candidate
            kosdaq_resp = call_with_retry(client.get_kosdaq_stock_daily_trade, candidate)
            kosdaq_records = get_records(kosdaq_resp)
            break

    if date_str is None:
        print("[에러] 최근 5일 내 거래 데이터를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    message = build_message(pairs, date_str, kospi_records, kosdaq_records)
    send_telegram_message(token, chat_id, message)
    print("전송 완료:\n", message)


if __name__ == "__main__":
    main()
