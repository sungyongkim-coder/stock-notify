"""
코스피/코스닥 관심 종목의 당일 시가·종가를 텔레그램으로 알림.

필요한 환경변수:
  - TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  - TELEGRAM_CHAT_ID   : 알림을 받을 chat id
  - KRX_OPENAPI_KEY    : KRX 공식 Open API 인증키 (openapi.krx.co.kr 에서 발급)
  - STOCK_CODES        : 쉼표로 구분된 6자리 종목코드 (예: "005930,035720,000660")

사용 라이브러리: pykrx-openapi (KRX 공식 Open API 래퍼, API 키 인증), requests

참고: get_stock_daily_trade / get_kosdaq_stock_daily_trade 는
{"OutBlock_1": [ {필드명: 값, ...}, ... ]} 형태의 dict를 반환한다.
"""

import os
import sys
from datetime import datetime, timedelta

import requests
from pykrx_openapi import KRXOpenAPI, KRXAuthenticationError, KRXNetworkError


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[에러] 환경변수 {name} 가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    return value


# 응답 레코드(dict)에서 값을 꺼낼 때 사용할 후보 필드명들.
# KRX 필드명은 대체로 대문자 스네이크. 버전/엔드포인트에 따라 조금씩 달라질 수
# 있어 여러 후보를 순서대로 확인한다.
CODE_KEYS = ["ISU_SRT_CD", "ISU_CD", "SHORT_CD", "단축코드", "종목코드"]
NAME_KEYS = ["ISU_ABBRV", "ISU_NM", "ISU_NAME", "종목명"]
OPEN_KEYS = ["TDD_OPNPRC", "OPNPRC", "시가"]
CLOSE_KEYS = ["TDD_CLSPRC", "CLSPRC", "종가"]
VOLUME_KEYS = ["ACC_TRDVOL", "TRDVOL", "거래량"]


def pick(record: dict, candidates):
    for key in candidates:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def get_records(resp) -> list:
    """라이브러리 응답(dict)에서 레코드 리스트를 안전하게 추출."""
    if isinstance(resp, dict):
        return resp.get("OutBlock_1", []) or []
    if isinstance(resp, list):
        return resp
    return []


def find_record(records: list, code: str):
    for rec in records:
        val = pick(rec, CODE_KEYS)
        if val is not None and code in str(val):
            return rec
    return None


def fetch_record_for_code(client, code, kospi_records, kosdaq_records):
    """미리 받아둔 코스피/코스닥 레코드에서 종목을 찾는다."""
    rec = find_record(kospi_records, code)
    if rec is not None:
        return rec, "KOSPI"
    rec = find_record(kosdaq_records, code)
    if rec is not None:
        return rec, "KOSDAQ"
    return None, None


def build_message(codes, date_str, kospi_records, kosdaq_records) -> str:
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    lines = [f"📈 {pretty_date} 관심종목 시가/종가"]

    for code in codes:
        rec, market = fetch_record_for_code(None, code, kospi_records, kosdaq_records)
        if rec is None:
            lines.append(f"\n[{code}] 데이터 없음 (휴장일이거나 종목코드 확인 필요)")
            continue

        name = pick(rec, NAME_KEYS) or code
        open_price = pick(rec, OPEN_KEYS)
        close_price = pick(rec, CLOSE_KEYS)
        volume = pick(rec, VOLUME_KEYS)

        if open_price is None or close_price is None:
            lines.append(
                f"\n[{name}({code})] 시가/종가 필드를 찾지 못했습니다.\n"
                f"  (원본 필드: {list(rec.keys())})"
            )
            continue

        open_price, close_price = float(open_price), float(close_price)
        change_rate = (close_price - open_price) / open_price * 100 if open_price else 0
        arrow = "🔺" if change_rate > 0 else ("🔻" if change_rate < 0 else "➖")

        entry = (
            f"\n[{name}({code}) · {market}]\n"
            f"  시가: {int(open_price):,}원\n"
            f"  종가: {int(close_price):,}원\n"
            f"  등락: {arrow} {change_rate:+.2f}%"
        )
        if volume is not None:
            entry += f"\n  거래량: {int(float(volume)):,}주"
        lines.append(entry)

    return "\n".join(lines)


def send_telegram_message(token, chat_id, text) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()


def main():
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_env("TELEGRAM_CHAT_ID")
    api_key = get_env("KRX_OPENAPI_KEY")
    codes_raw = get_env("STOCK_CODES")
    codes = [c.strip() for c in codes_raw.split(",") if c.strip()]

    if not codes:
        print("[에러] STOCK_CODES 에 유효한 종목코드가 없습니다.", file=sys.stderr)
        sys.exit(1)

    try:
        client = KRXOpenAPI(api_key=api_key)
    except KRXAuthenticationError:
        print("[에러] KRX_OPENAPI_KEY 가 유효하지 않습니다.", file=sys.stderr)
        sys.exit(1)

    # 오늘부터 최대 5일 과거로 거슬러 올라가며 가장 최근 영업일 데이터를 찾는다.
    date_str = None
    kospi_records, kosdaq_records = [], []
    for i in range(5):
        candidate = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            kospi_resp = client.get_stock_daily_trade(bas_dd=candidate)
        except KRXNetworkError as e:
            print(f"[경고] {candidate} 코스피 조회 네트워크 오류: {e}", file=sys.stderr)
            continue

        kospi_records = get_records(kospi_resp)
        if kospi_records:
            date_str = candidate
            try:
                kosdaq_resp = client.get_kosdaq_stock_daily_trade(bas_dd=candidate)
                kosdaq_records = get_records(kosdaq_resp)
            except Exception as e:
                print(f"[경고] 코스닥 조회 실패(코스피만 사용): {e}", file=sys.stderr)
                kosdaq_records = []
            break

    if date_str is None:
        print("[에러] 최근 5일 내 거래 데이터를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    message = build_message(codes, date_str, kospi_records, kosdaq_records)
    send_telegram_message(token, chat_id, message)
    print("전송 완료:\n", message)


if __name__ == "__main__":
    main()
