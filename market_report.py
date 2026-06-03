"""
금융시장동향 Daily Report  — 매 영업일 오후 4시 자동 발송
데이터 소스:
  주가: yfinance (KOSPI ^KS11, KOSDAQ ^KQ11, 니케이 ^N225, 다우 ^DJI, 나스닥 ^IXIC)
  환율: yfinance 실시간 시장환율 (원/달러·원/엔100·원/위안, 서울 외환시장 장마감 기준)
  금리: 네이버금융 (국고채3년) + ECOS API (국고채10년, 국채선물3년, API키 옵션)
"""

import sys, json, smtplib, schedule, time, logging, requests
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
import yfinance as yf

# ── 로깅 ───────────────────────────────────────────────────────────────────────
log_handler = RotatingFileHandler(
    "market_report.log", maxBytes=3*1024*1024, backupCount=2, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

# ── 표시 헬퍼 ──────────────────────────────────────────────────────────────────
def arrow(v: float) -> str:
    return "▲" if v > 0 else ("▼" if v < 0 else "-")

def chg_color(v: float) -> str:
    return "#c0392b" if v > 0 else ("#2980b9" if v < 0 else "#777")

# ── 공통 HTTP 헤더 ──────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

# ══════════════════════════════════════════════════════
# 데이터 수집 함수
# ══════════════════════════════════════════════════════

def get_yf(ticker: str, name: str) -> dict | None:
    """yfinance: 주가지수"""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist is None or len(hist) < 2:
            return None
        close = float(hist["Close"].iloc[-1])
        prev  = float(hist["Close"].iloc[-2])
        chg   = close - prev
        return {"name": name, "close": close, "change": chg,
                "change_pct": chg / prev * 100, "date": hist.index[-1].strftime("%m/%d")}
    except Exception as e:
        logger.warning(f"{name}: {e}")
        return None


def get_naver_fx() -> dict:
    """네이버페이 증권 시장지표 환율 (하나은행 고시 기준)"""
    result = {}
    naver_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://m.stock.naver.com/",
    }

    for key, code, name in [
        ("usdkrw", "FX_USDKRW", "원/달러"),
        ("jpykrw", "FX_JPYKRW", "원/엔(100엔)"),
        ("cnykrw", "FX_CNYKRW", "원/위안"),
    ]:
        try:
            r = requests.get(
                f"https://api.stock.naver.com/marketindex/exchange/{code}",
                headers=naver_headers, timeout=10,
            )
            r.raise_for_status()
            d = r.json().get("exchangeInfo", {})

            close     = float(d["closePrice"].replace(",", ""))
            chg       = float(d["fluctuations"].replace(",", ""))
            chg_pct   = float(d["fluctuationsRatio"].replace(",", ""))
            rise_type = d.get("fluctuationsType", {}).get("name", "")
            if rise_type == "FALLING":
                chg     = -abs(chg)
                chg_pct = -abs(chg_pct)

            traded    = d.get("localTradedAt", "")
            date_str  = traded[5:10].replace("-", "/") + " " + traded[11:16] if traded else datetime.now().strftime("%m/%d %H:%M")

            result[key] = {
                "name": name, "close": close,
                "change": chg, "change_pct": chg_pct,
                "date": date_str,
            }
            logger.info(f"{name}: {close:.2f}원  {chg:+.2f}({chg_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"{name} 네이버 환율: {e}")

    return result


def get_naver_bond(code: str, name: str) -> dict | None:
    """네이버 증권 실시간 국채 수익률 (api.stock.naver.com)
    code 예: KR3YT=RR (3년), KR10YT=RR (10년)
    """
    url = f"https://api.stock.naver.com/marketindex/bond/{code}"
    try:
        r = requests.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://m.stock.naver.com/",
        }, timeout=10)
        r.raise_for_status()
        d = r.json()
        rate  = float(d["closePrice"])
        chg   = float(d["fluctuations"])   # 전일대비 %p
        traded = d.get("localTradedAt", "")
        date_fmt = traded[5:10].replace("-", "/") if traded else datetime.now().strftime("%m/%d %H:%M")
        return {"name": name, "rate": rate, "change": chg, "date": date_fmt}
    except Exception as e:
        logger.warning(f"{name} 네이버 API: {e}")
        return None


def get_ecos(api_key: str, stat_code: str, item_code: str, name: str,
             is_futures: bool = False) -> dict | None:
    """한국은행 ECOS API — 일별 금리/가격 조회
    API키 무료발급: https://ecos.bok.or.kr/api/#/DevGuide/TokenKey
    """
    if not api_key:
        return None
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr"
        f"/1/10/{stat_code}/D/{start}/{today}/{item_code}"
    )
    try:
        data = requests.get(url, timeout=10).json()
        rows = data.get("StatisticSearch", {}).get("row", [])
        if len(rows) < 2:
            return None
        cur  = rows[-1]
        prev = rows[-2]
        rate = float(cur["DATA_VALUE"])
        prev_r = float(prev["DATA_VALUE"])
        chg  = rate - prev_r
        d_str = cur["TIME"]
        date_fmt = f"{d_str[4:6]}/{d_str[6:8]}"

        if is_futures:
            return {"name": name, "close": rate, "change": chg,
                    "change_pct": chg / prev_r * 100, "date": date_fmt}
        return {"name": name, "rate": rate, "change": chg, "date": date_fmt}
    except Exception as e:
        logger.warning(f"{name} ECOS: {e}")
        return None


def get_ktb_futures(cfg: dict) -> dict | None:
    """국채선물 3년 종가 — pykrx (KRX 계정 필요, config에 krx_id/krx_pw 설정)"""
    krx_id = cfg.get("krx_id", "")
    krx_pw = cfg.get("krx_pw", "")
    if not (krx_id and krx_pw):
        return None
    try:
        import os
        os.environ["KRX_ID"] = krx_id
        os.environ["KRX_PW"] = krx_pw
        from pykrx import stock
        from pykrx.website.comm import auth as krx_auth
        krx_auth._auth_session = krx_auth.build_krx_session(krx_id, krx_pw)

        from datetime import datetime, timedelta
        end   = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")

        df = stock.get_future_ohlcv_by_ticker(start, end, "KRDRVFUBM3")
        if df is None or len(df) < 2:
            return None
        close = float(df["종가"].iloc[-1])
        prev  = float(df["종가"].iloc[-2])
        chg   = close - prev
        date  = df.index[-1].strftime("%m/%d")
        return {"name": "국채선물 3년", "close": close, "change": chg,
                "change_pct": chg / prev * 100, "date": date}
    except Exception as e:
        logger.warning(f"국채선물 3년 pykrx: {e}")
        return None


# ══════════════════════════════════════════════════════
# 전체 수집
# ══════════════════════════════════════════════════════

def collect_all(cfg: dict) -> dict:
    logger.info("시장 데이터 수집 시작")
    ecos_key = cfg.get("ecos_api_key", "")

    fx = get_naver_fx()

    # 국고채: 네이버 실시간 API 우선, ECOS 전일값 fallback
    bond_3y  = (get_naver_bond("KR3YT=RR",  "국고채 3년")
                or get_ecos(ecos_key, "817Y002", "010200000", "국고채 3년"))
    bond_10y = (get_naver_bond("KR10YT=RR", "국고채 10년")
                or get_ecos(ecos_key, "817Y002", "010210000", "국고채 10년"))

    data = {
        "kospi":       get_yf("^KS11",  "KOSPI"),
        "kosdaq":      get_yf("^KQ11",  "KOSDAQ"),
        "bond_3y":     bond_3y,
        "bond_10y":    bond_10y,
        "nikkei":      get_yf("^N225",  "니케이 225"),
        "dow":         get_yf("^DJI",   "다우존스"),
        "nasdaq":      get_yf("^IXIC",  "나스닥"),
        "usdkrw":      fx.get("usdkrw"),
        "jpykrw":      fx.get("jpykrw"),
        "cnykrw":      fx.get("cnykrw"),
    }
    logger.info("시장 데이터 수집 완료")
    return data


# ══════════════════════════════════════════════════════
# 이메일 HTML 빌드
# ══════════════════════════════════════════════════════

def _td(v, s=""):
    return f"<td style='padding:9px 12px;{s}'>{v}</td>"

def row_index(d: dict | None, is_bond=False, is_fx=False) -> str:
    if not d:
        return (f"<tr style='border-bottom:1px solid #f5f5f5;'>"
                f"{_td('—','color:#ccc;')}{_td('조회 불가 — ECOS 키 필요','color:#ccc;text-align:right;')}"
                f"<td></td><td></td><td></td></tr>")

    chg_v = d.get("change", 0)
    color = chg_color(chg_v)
    ar    = arrow(chg_v)

    if is_bond:
        val_s = f"{d['rate']:.3f}%"
        chg_s = f"{ar} {abs(chg_v):.3f}%p"   # 등락폭
        prev_rate = d["rate"] - chg_v
        chg_pct = (chg_v / prev_rate * 100) if prev_rate else 0
        ext   = f"{chg_pct:+.2f}%"            # 등락률
    else:
        pct   = d.get("change_pct", 0)
        unit  = "원" if is_fx else ""
        chg_s = f"{ar} {abs(chg_v):,.2f}{unit}"  # 등락폭 (앞)
        ext   = f"{pct:+.2f}%"                   # 등락률 (뒤)
        val_s = f"{d['close']:,.2f}원" if is_fx else f"{d['close']:,.2f}"

    return (f"<tr style='border-bottom:1px solid #f5f5f5;'>"
            f"{_td(d['name'], 'font-weight:600;')}"
            f"{_td(val_s, 'text-align:right;font-size:16px;font-weight:700;')}"
            f"{_td(chg_s, f'text-align:right;color:{color};font-weight:600;')}"
            f"{_td(ext,   'text-align:right;color:#999;font-size:13px;')}"
            f"{_td(d.get('date',''), 'text-align:right;color:#ccc;font-size:12px;')}"
            f"</tr>")

TH_ROW = ("<tr style='background:#f0f2f5;color:#666;font-size:12px;'>"
          "<th style='padding:7px 12px;text-align:left;'>구분</th>"
          "<th style='padding:7px 12px;text-align:right;'>종가/금리</th>"
          "<th style='padding:7px 12px;text-align:right;'>등락폭</th>"
          "<th style='padding:7px 12px;text-align:right;'>등락률</th>"
          "<th style='padding:7px 12px;text-align:right;'>기준일</th></tr>")

def section(title: str, rows: str, note: str = "") -> str:
    note_html = (f"<p style='margin:3px 8px 0;color:#aaa;font-size:11px;'>{note}</p>"
                 if note else "")
    return (f"<div style='margin:14px 0;'>"
            f"<div style='background:#1a2740;color:#fff;padding:9px 14px;"
            f"border-radius:6px 6px 0 0;font-weight:700;font-size:14px;'>{title}</div>"
            f"<table style='width:100%;border-collapse:collapse;background:#fff;"
            f"border:1px solid #e8e8e8;border-top:none;'>"
            f"<thead>{TH_ROW}</thead><tbody>{rows}</tbody></table>"
            f"{note_html}</div>")


def build_email(data: dict) -> tuple[str, str]:
    weekdays = ["월","화","수","목","금","토","일"]
    now = datetime.now()
    now_kor = now.strftime(f"%Y년 %m월 %d일 ({weekdays[now.weekday()]}) %H:%M")

    kr_r   = row_index(data["kospi"]) + row_index(data["kosdaq"])
    bond_r = (row_index(data["bond_3y"], is_bond=True)
            + row_index(data["bond_10y"], is_bond=True))
    nk_r   = row_index(data["nikkei"])
    us_r   = row_index(data["dow"]) + row_index(data["nasdaq"])
    fx_r   = (row_index(data["usdkrw"], is_fx=True)
            + row_index(data["jpykrw"], is_fx=True)
            + row_index(data["cnykrw"], is_fx=True))

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'></head>
<body style='font-family:"Malgun Gothic",Arial,sans-serif;max-width:700px;
             margin:auto;padding:20px;background:#f5f6f8;'>
  <div style='background:#1a2740;color:#fff;padding:22px 26px;border-radius:8px 8px 0 0;'>
    <h1 style='margin:0;font-size:22px;'>금융시장동향 Daily Report</h1>
    <p style='margin:6px 0 0;color:#8fa3bf;font-size:13px;'>{now_kor} 기준</p>
  </div>
  <div style='background:#fff;padding:20px 24px;border:1px solid #e0e0e0;
              border-top:none;border-radius:0 0 8px 8px;'>
    {section("📈 국내 주가지수", kr_r)}
    {section("📊 금리", bond_r)}
    {section("🌏 일본 니케이", nk_r)}
    {section("🌎 미국 주요지수", us_r, "※ 미국 지수는 전일 NY 종가 기준")}
    {section("💱 환율 (16:00 기준)", fx_r)}
  </div>
  <p style='text-align:center;color:#bbb;font-size:11px;margin-top:10px;'>
    본 메일은 자동 발송됩니다. | 출처: KRX·yfinance·네이버금융·ECOS
  </p>
</body></html>"""

    # Plain text
    def pt(d, is_bond=False, is_fx=False):
        if not d:
            return None
        ar = arrow(d.get("change", 0))
        if is_bond:
            return f"  {d['name']:<15}: {d['rate']:.3f}%  {ar}{abs(d['change']):.3f}%p"
        elif is_fx:
            return f"  {d['name']:<15}: {d['close']:>10,.2f}원  {ar}{abs(d.get('change_pct',0)):.2f}%"
        return f"  {d['name']:<15}: {d['close']:>12,.2f}  {ar}{abs(d.get('change_pct',0)):.2f}%"

    weekdays = ["월","화","수","목","금","토","일"]
    lines = [f"■ 금융시장동향 Daily Report  {now_kor}\n"]
    for sec, items in [
        ("국내 주가지수", [pt(data["kospi"]), pt(data["kosdaq"])]),
        ("금리",         [pt(data["bond_3y"], is_bond=True),
                          pt(data["bond_10y"], is_bond=True)]),
        ("일본 니케이",  [pt(data["nikkei"])]),
        ("미국 (전일종가)",[pt(data["dow"]), pt(data["nasdaq"])]),
        ("환율",          [pt(data["usdkrw"], is_fx=True),
                           pt(data["jpykrw"], is_fx=True),
                           pt(data["cnykrw"], is_fx=True)]),
    ]:
        lines.append(f"\n[{sec}]")
        lines += [x for x in items if x]

    return "\n".join(lines), html


# ══════════════════════════════════════════════════════
# 발송 & 스케줄러
# ══════════════════════════════════════════════════════

def send_report():
    cfg = load_config()
    ec  = cfg["email"]
    data = collect_all(cfg)
    plain, html = build_email(data)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[금융시장동향] {datetime.now().strftime('%Y년 %m월 %d일')} 일일 리포트"
    msg["From"]    = ec["sender"]
    msg["To"]      = ", ".join(ec["recipients"])
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"])) as s:
        s.ehlo(); s.starttls()
        s.login(ec["sender"], ec["password"])
        s.send_message(msg)
    logger.info("리포트 이메일 발송 완료")


def run_daily():
    if datetime.now().weekday() >= 5:
        logger.info("주말 — 건너뜀")
        return
    try:
        send_report()
    except Exception as e:
        logger.error(f"발송 실패: {e}", exc_info=True)


def main():
    logger.info("금융시장동향 스케줄러 시작 (매 영업일 16:00)")
    if "--now" in sys.argv:
        run_daily(); return
    run_daily()
    schedule.every().day.at("16:00").do(run_daily)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
