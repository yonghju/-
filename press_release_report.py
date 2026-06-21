# press_release_report.py
# 금융유관기관 전일 보도자료 수집 → 이메일 발송 (매일 08:00)
import json, re, sys, io, time, smtplib, datetime, logging
from pathlib import Path
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import xml.etree.ElementTree as ET

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

try:
    import schedule
    _HAS_SCHEDULE = True
except ImportError:
    _HAS_SCHEDULE = False

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_handler_file = logging.FileHandler(_LOG_DIR / "press_release_report.log", encoding="utf-8")
_log_handler_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_handlers = [_log_handler_file]
if sys.stdout and hasattr(sys.stdout, "buffer"):
    _handlers.append(logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)

CFG_FILE = "config.json"

# ── 설정 로드 ─────────────────────────────────────────────────────────────────
def load_cfg() -> dict:
    with open(CFG_FILE, encoding="utf-8") as f:
        return json.load(f)

# ── 날짜 헬퍼 ─────────────────────────────────────────────────────────────────
def yesterday() -> datetime.date:
    return datetime.date.today() - datetime.timedelta(days=1)

def _ymd(d: datetime.date) -> str:
    return d.strftime("%Y-%m-%d")

def _ymd_kr(d: datetime.date) -> str:
    return d.strftime("%Y년 %m월 %d일")

# 날짜 문자열 파싱 (여러 형식 대응)
_DATE_PATS = [
    (re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})"), "%Y-%m-%d"),  # 2026-05-18
    (re.compile(r"(\d{2})(\d{2})(\d{2})"),                               None),   # 260518
    (re.compile(r"(\d{4})(\d{2})(\d{2})"),                               None),   # 20260518
]

def _parse_date_str(s: str) -> datetime.date | None:
    s = s.strip()
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"\b(\d{6})\b", s)
    if m:
        yy, mm, dd = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
        try:
            return datetime.date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            pass
    m = re.search(r"(\d{8})", s)
    if m:
        raw = m.group(1)
        try:
            return datetime.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            pass
    return None

# ── HTTP 세션 ─────────────────────────────────────────────────────────────────
SESS = requests.Session()
SESS.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
})

def _get(url, **kw) -> requests.Response | None:
    try:
        r = SESS.get(url, timeout=20, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        logging.warning(f"GET {url} failed: {e}")
        return None

# ── 각 기관 파서 ──────────────────────────────────────────────────────────────

def _fetch_fsc(target_date: datetime.date) -> list[str]:
    """금융위원회 보도자료 HTML (https://www.fsc.go.kr/no010101)
    구조: <li> 안에 <a>제목</a> ... 날짜 텍스트(2026-05-18)
    연결 불안정 → 재시도 3회
    """
    if not _HAS_BS4:
        return []
    items = []
    r = None
    for attempt in range(3):
        r = _get("https://www.fsc.go.kr/no010101")
        if r:
            break
        time.sleep(2)
    if not r:
        # RSS fallback
        r = _get("http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111")
        if not r:
            return items
        try:
            root = ET.fromstring(r.content)
            ns = {"dc": "http://purl.org/dc/elements/1.1/"}
            for item_el in root.iter("item"):
                title = (item_el.findtext("title") or "").strip()
                pub = item_el.findtext("pubDate") or ""
                d = None
                m2 = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", pub)
                if m2:
                    try:
                        d = datetime.datetime.strptime(
                            f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%d %b %Y").date()
                    except ValueError:
                        pass
                if d is None:
                    d = _parse_date_str(pub)
                if d == target_date and title:
                    items.append(title)
        except Exception as e:
            logging.warning(f"FSC RSS fallback error: {e}")
        return items
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # 각 <li>에서 제목(<a>) + 날짜 추출
        for li in soup.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 5:
                continue
            li_text = li.get_text(" ", strip=True)
            d = _parse_date_str(li_text)
            if d == target_date:
                items.append(title)
    except Exception as e:
        logging.warning(f"FSC parse error: {e}")
    return items

def _fetch_fss(target_date: datetime.date) -> list[str]:
    """금융감독원 보도자료 목록 HTML"""
    if not _HAS_BS4:
        return []
    items = []
    r = _get("https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218")
    if not r:
        return items
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # 게시판 목록 행 탐색
        rows = soup.select("table tbody tr") or soup.select(".bbs_list tr") or soup.select("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            # 제목 컬럼 (보통 2번째), 날짜 컬럼 (보통 마지막)
            title_td = None
            date_str = ""
            for td in cols:
                text = td.get_text(" ", strip=True)
                if _parse_date_str(text):
                    date_str = text
                elif len(text) > 8 and not title_td:
                    title_td = text
            if not title_td or not date_str:
                continue
            d = _parse_date_str(date_str)
            if d == target_date:
                items.append(title_td)
    except Exception as e:
        logging.warning(f"FSS parse error: {e}")
    return items

def _fetch_bok(target_date: datetime.date) -> list[str]:
    """한국은행 보도자료 RSS 피드
    페이지 JS 동적 로딩 → RSS 사용 (https://www.bok.or.kr/portal/bbs/B0000552/news.rss?menuNo=200690)
    """
    items = []
    r = _get("https://www.bok.or.kr/portal/bbs/B0000552/news.rss?menuNo=200690")
    if not r:
        return items
    try:
        root = ET.fromstring(r.content)
        for item_el in root.iter("item"):
            title = (item_el.findtext("title") or "").strip()
            pub = item_el.findtext("pubDate") or ""
            d = None
            # pubDate: "Mon, 19 May 2026 00:00:00 +0900"
            m2 = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", pub)
            if m2:
                try:
                    d = datetime.datetime.strptime(
                        f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%d %b %Y").date()
                except ValueError:
                    pass
            if d is None:
                d = _parse_date_str(pub)
            if d == target_date and title:
                items.append(title)
    except Exception as e:
        logging.warning(f"BOK RSS parse error: {e}")
    return items

def _fetch_kofia(target_date: datetime.date) -> list[str]:
    """금융투자협회 보도자료 목록 HTML"""
    if not _HAS_BS4:
        return []
    items = []
    r = _get("https://www.kofia.or.kr/brd/m_211/list.do")
    if not r:
        return items
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table tbody tr") or soup.select(".bbs_list tr") or soup.select("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            title_td = None
            date_str = ""
            for td in cols:
                text = td.get_text(" ", strip=True)
                if re.search(r"\d{4}[.\-]\d{2}[.\-]\d{2}", text):
                    date_str = text
                elif len(text) > 8 and not title_td:
                    title_td = text
            if not title_td or not date_str:
                continue
            d = _parse_date_str(date_str)
            if d == target_date:
                items.append(title_td)
    except Exception as e:
        logging.warning(f"KOFIA parse error: {e}")
    return items

def _fetch_krx(target_date: datetime.date) -> list[str]:
    """한국거래소 보도자료 — 메인 홈페이지 JSON API (noti_info&obj=news)"""
    items = []
    try:
        SESS.get("https://www.krx.co.kr/main/main.jsp", timeout=15)
        r = SESS.post(
            "https://www.krx.co.kr/main/main.jspx?cmd=noti_info&obj=news",
            headers={"Referer": "https://www.krx.co.kr/main/main.jsp"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        target_str = target_date.strftime("%Y/%m/%d")
        for item in data.get("output", []):
            if item.get("wrt_dd", "") == target_str:
                title = item.get("title", "").rstrip(".")
                if title:
                    items.append(title)
    except Exception as e:
        logging.warning(f"KRX parse error: {e}")
    return items

def _fetch_molit(target_date: datetime.date) -> list[str]:
    """국토교통부 보도자료 목록 HTML (연결 불안정 → 재시도)"""
    if not _HAS_BS4:
        return []
    items = []
    url = "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"
    r = None
    for attempt in range(3):
        try:
            r = SESS.get(url, timeout=25)
            r.raise_for_status()
            break
        except Exception as e:
            logging.warning(f"MOLIT attempt {attempt+1} failed: {e}")
            time.sleep(2)
    if not r:
        return items
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table tbody tr") or soup.select(".bbs_list tr") or soup.select("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            title_td = None
            date_str = ""
            for td in cols:
                text = td.get_text(" ", strip=True)
                if re.search(r"\d{4}[.\-]\d{2}[.\-]\d{2}", text):
                    date_str = text
                elif len(text) > 8 and not title_td:
                    title_td = text
            if not title_td or not date_str:
                continue
            d = _parse_date_str(date_str)
            if d == target_date:
                items.append(title_td)
    except Exception as e:
        logging.warning(f"MOLIT parse error: {e}")
    return items

# ── 전체 수집 ─────────────────────────────────────────────────────────────────
AGENCY_FETCHERS = [
    ("금융감독원",   _fetch_fss),
    ("금융위원회",   _fetch_fsc),
    ("한국은행",     _fetch_bok),
    ("금융투자협회", _fetch_kofia),
    ("한국거래소",   _fetch_krx),
    ("국토교통부",   _fetch_molit),
]

def collect_releases(target_date: datetime.date) -> dict[str, list[str]]:
    result = {}
    for name, fetcher in AGENCY_FETCHERS:
        logging.info(f"  {name} 수집 중...")
        try:
            items = fetcher(target_date)
        except Exception as e:
            logging.warning(f"  {name} 오류: {e}")
            items = []
        result[name] = items
        logging.info(f"  {name}: {len(items)}건")
    return result

# ── HTML 이메일 생성 ───────────────────────────────────────────────────────────
def _bullet(items: list[str]) -> str:
    if not items:
        return "<li style='color:#888;'>전일 보도자료 없음</li>"
    parts = []
    for i in items:
        if i.startswith("⚠"):
            parts.append(f"<li style='color:#e65100;font-style:italic;'>{i}</li>")
        else:
            parts.append(f"<li>{i}</li>")
    return "".join(parts)

def build_html(data: dict[str, list[str]], target_date: datetime.date) -> str:
    date_str = _ymd_kr(target_date)
    total = sum(len(v) for v in data.values())

    sections = []
    for agency, items in data.items():
        badge = f"<span style='background:#1565c0;color:#fff;padding:1px 8px;border-radius:12px;font-size:12px;'>{len(items)}건</span>"
        bullets = _bullet(items)
        sections.append(f"""
        <div style="margin-bottom:18px;">
          <h3 style="margin:0 0 6px;color:#1565c0;font-size:14px;">
            {agency} {badge}
          </h3>
          <ul style="margin:0;padding-left:20px;font-size:13px;line-height:1.7;">
            {bullets}
          </ul>
        </div>""")

    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"></head>
<body style="font-family:'맑은 고딕',Arial,sans-serif;background:#f5f6fa;padding:0;margin:0;">
<div style="max-width:680px;margin:20px auto;background:#fff;border-radius:8px;
            box-shadow:0 2px 8px rgba(0,0,0,.12);overflow:hidden;">

  <!-- 헤더 -->
  <div style="background:#1565c0;padding:20px 28px;">
    <div style="color:#fff;font-size:18px;font-weight:bold;">
      금융유관기관 보도자료 요약
    </div>
    <div style="color:#bbdefb;font-size:13px;margin-top:4px;">
      {date_str} 기준 &nbsp;·&nbsp; 총 {total}건
    </div>
  </div>

  <!-- 본문 -->
  <div style="padding:24px 28px;">
    {body}
  </div>

  <!-- 푸터 -->
  <div style="background:#f5f5f5;padding:12px 28px;font-size:11px;color:#9e9e9e;
              border-top:1px solid #e0e0e0;">
    금융감독원·금융위원회·한국은행·금융투자협회·한국거래소·국토교통부 전일 보도자료 자동 수집
  </div>
</div>
</body></html>"""

# ── 이메일 발송 ───────────────────────────────────────────────────────────────
def send_email(cfg: dict, html: str, target_date: datetime.date):
    ec = cfg["email"]
    date_str = target_date.strftime("%Y.%m.%d")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[금융유관기관 보도자료] {date_str}"
    msg["From"]    = ec["sender"]
    msg["To"]      = ", ".join(ec["recipients"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(ec["smtp_host"], ec["smtp_port"]) as s:
        s.ehlo()
        s.starttls()
        s.login(ec["sender"], ec["password"])
        s.sendmail(ec["sender"], ec["recipients"], msg.as_bytes())

    logging.info(f"이메일 발송 완료 → {ec['recipients']}")

# ── 메인 작업 ─────────────────────────────────────────────────────────────────
def run_report():
    logging.info("=== 금융유관기관 보도자료 수집 시작 ===")
    cfg  = load_cfg()
    tgt  = yesterday()
    logging.info(f"대상일: {_ymd(tgt)}")

    data = collect_releases(tgt)
    html = build_html(data, tgt)

    # 미리보기 로그
    total = sum(len(v) for v in data.values())
    logging.info(f"[{_ymd(tgt)}] 금융유관기관 보도자료 요약 (총 {total}건)")
    for agency, items in data.items():
        logging.info(f"  {agency} ({len(items)}건)")
        for i, title in enumerate(items, 1):
            logging.info(f"    {i}. {title}")
        if not items:
            logging.info("    (전일 보도자료 없음)")

    send_email(cfg, html, tgt)
    logging.info("=== 완료 ===\n")

# ── 스케줄러 ──────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="즉시 실행 후 종료")
    args = ap.parse_args()

    if args.now:
        run_report()
        return

    if not _HAS_SCHEDULE:
        logging.error("schedule 미설치 — pip install schedule")
        return

    schedule.every().day.at("08:20").do(run_report)
    logging.info("스케줄러 시작: 매일 08:20 실행")
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
