"""
코스피/코스닥 관심 종목의 당일 시가·종가를 텔레그램으로 알림.

필요한 환경변수:
  - TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  - TELEGRAM_CHAT_ID   : 알림을 받을 chat id
  - KRX_OPENAPI_KEY    : KRX 공식 Open API 인증키 (openapi.krx.co.kr 에서 발급)
  - STOCK_CODES        : 쉼표로 구분된 6자리 종목코드 (예: "005930,035720,000660")

사용 라이브러리: pykrx-openapi (KRX 공식 Open API 래퍼, API 키 인증), requests
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


# KRX Open API 응답의 종목코드/종목명 컬럼은 라이브러리 버전에 따라 이름이
# 다를 수 있어서, 흔히 쓰이는 후보들을 순서대로 확인합니다.
CODE_COLUMNS = ["ISU_SRT_CD", "isu_srt_cd", "ISU_CD", "isu_cd", "단축코드", "종목코드"]
NAME_COLUMNS = ["ISU_ABBRV", "isu_abbrv", "ISU_NM", "종목명"]
OPEN_COLUMNS = ["TDD_OPNPRC", "tdd_opnprc", "시가"]
CLOSE_COLUMNS = ["TDD_CLSPRC", "tdd_clsprc", "종가"]
VOLUME_COLUMNS = ["ACC_TRDVOL", "acc_trdvol", "거래량"]


def _first_present(row, candidates):
    for col in candidates:
        if col in row.index and row[col] not in (None, ""):
            return row[col]
    return None


def find_row(df, code: str):
    if df is None or df.empty:
        return None
    for col in CODE_COLUMNS:
        if col in df.columns:
            # 6자리 단축코드든 12자리 ISIN이든 뒷부분에 코드가 포함되므로 contains로 매칭
            matched = df[df[col].astype(str).str.contains(code, na=False)]
            if not matched.empty:
                return matched.iloc[0]
    return None


def fetch_row_for_code(client: KRXOpenAPI, code: str, date_str: str):
    """코스피 → 코스닥 순서로 조회해서 종목코드가 매칭되는 행을 반환."""
    try:
        kospi_df = client.get_stock_daily_trade(bas_dd=date_str)
        row = find_row(kospi_df, code)
        if row is not None:
            return row, "KOSPI"
    except Exception as e:
        print(f"[경고] 코스피 데이터 조회 실패: {e}", file=sys.stderr)

    try:
        kosdaq_df = client.get_kosdaq_stock_daily_trade(bas_dd=date_str)
        row = find_row(kosdaq_df, code)
        if row is not None:
            return row, "KOSDAQ"
    except Exception as e:
        print(f"[경고] 코스닥 데이터 조회 실패: {e}", file=sys.stderr)

    return None, None


def build_message(client: KRXOpenAPI, codes: list[str], date_str: str) -> str:
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    lines = [f"📈 {pretty_date} 관심종목 시가/종가"]

    for code in codes:
        row, market = fetch_row_for_code(client, code, date_str)
        if row is None:
            lines.append(f"\n[{code}] 데이터 없음 (휴장일이거나 종목코드 확인 필요)")
            continue

        name = _first_present(row, NAME_COLUMNS) or code
        open_price = _first_present(row, OPEN_COLUMNS)
        close_price = _first_present(row, CLOSE_COLUMNS)
        volume = _first_present(row, VOLUME_COLUMNS)

        if open_price is None or close_price is None:
            lines.append(
                f"\n[{name}({code})] 시가/종가 컬럼을 찾지 못했습니다.\n"
                f"  (참고용 원본 컬럼: {list(row.index)})"
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


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
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

    # 오늘 날짜로 조회하되, 휴장일이라 전 종목 데이터가 비어있으면 하루씩 과거로
    # 최대 5일까지 되짚어가며 가장 최근 영업일 데이터를 찾습니다.
    date_str = None
    for i in range(5):
        candidate = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            probe = client.get_stock_daily_trade(bas_dd=candidate)
        except KRXNetworkError as e:
            print(f"[경고] {candidate} 조회 중 네트워크 오류: {e}", file=sys.stderr)
            continue
        if probe is not None and not probe.empty:
            date_str = candidate
            break

    if date_str is None:
        print("[에러] 최근 5일 내 거래 데이터를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    message = build_message(client, codes, date_str)
    send_telegram_message(token, chat_id, message)
    print("전송 완료:\n", message)


if __name__ == "__main__":
    main()
