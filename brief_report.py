#!/usr/bin/env python3
"""
증권사시황BRIEF: 4개사 모닝 브리핑 수집·요약 → 매 영업일 08:30 이메일 발송

수집 방식:
- 미래에셋: t.me/s/ehdwl (서상영 글로벌 일일 시황)
- 한국투자: t.me/s/kisthemacro (채권·경제 Note)
- 키움증권:  t.me/s/hedgecat0301 (한지영 국내·미국 전략/시황)
- 메리츠:    t.me/s/Meritz_strategy (전략공감2.0, 채권전략)
"""

import argparse, json, logging, re, smtplib, warnings
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TypedDict

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    import schedule, time as _time
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"

KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

# ── 각 증권사 설정 ──────────────────────────────────────────────────────────
COMPANY_CONFIG: list[dict] = [
    {
        "name": "미래에셋증권",
        "channel": "ehdwl",
        "keywords": ["코스피", "미국 증시", "시황", "글로벌", "증시", "국채", "주간 이슈"],
        "fallback_url": "https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=1543",
        "color": "#e31837",
        "lookback_days": 0,
        "max_items": 3,
    },
    {
        "name": "한국투자증권",
        "channel": "kisthemacro",
        "keywords": ["채권 Note", "경제 Note", "금리", "시황", "한국은행", "국채", "거시"],
        "fallback_url": "https://securities.koreainvestment.com/main/research/Main.jsp",
        "color": "#ff6600",
        "lookback_days": 0,
        "max_items": 3,
    },
    {
        "name": "키움증권",
        "channel": "hedgecat0301",
        "keywords": ["코스피", "코스닥", "미국 증시", "시황", "전망", "한지영", "증시"],
        "fallback_url": "https://www.kiwoom.com/h/invest/research/VResearchIssueSelForm",
        "color": "#e8401c",
        "lookback_days": 0,
        "max_items": 2,
    },
    {
        "name": "메리츠증권",
        "channel": "Meritz_strategy",
        "keywords": ["전략공감", "채권전략", "Strategy Daily", "MERITZ", "투자전략"],
        "fallback_url": "https://www.imeritz.com/",
        "color": "#003087",
        "lookback_days": 0,
        "max_items": 3,
    },
]


# ── 언론사 뉴스 설정 ─────────────────────────────────────────────────────────
NEWS_CONFIG: list[dict] = [
    {
        "name": "한국경제",
        "color": "#00499b",
        "feeds": [
            {"url": "https://www.hankyung.com/feed/finance",    "label": "증권·주식"},
            {"url": "https://www.hankyung.com/feed/economy",    "label": "경제"},
            {"url": "https://www.hankyung.com/feed/realestate", "label": "부동산"},
        ],
        "keywords": ["시황", "증시", "코스피", "코스닥", "채권", "금리", "부동산", "아파트", "전망", "오전", "주간"],
        "max_items": 5,
        "home_url": "https://www.hankyung.com",
    },
    {
        "name": "매일경제",
        "color": "#c0392b",
        "feeds": [
            {"url": "https://www.mk.co.kr/rss/40300001/", "label": "증권"},
            {"url": "https://www.mk.co.kr/rss/30000001/", "label": "경제"},
            {"url": "https://www.mk.co.kr/rss/50000001/", "label": "부동산"},
        ],
        "keywords": ["시황", "증시", "코스피", "코스닥", "채권", "금리", "부동산", "아파트", "전망", "오전", "주간"],
        "max_items": 5,
        "home_url": "https://www.mk.co.kr",
    },
]


class BriefResult(TypedDict):
    name: str
    color: str
    items: list[dict]   # {"datetime": str, "text": str, "links": list[str]}
    status: str         # "ok" | "fail"
    fallback_url: str


# ── 유틸 ────────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("brief_report")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_DIR / "brief_report.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
    return logger


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def parse_telegram_dt(dt_str: str) -> datetime | None:
    """ISO datetime → KST datetime 변환"""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(KST)
    except Exception:
        return None


def clean_text(raw: str) -> str:
    """텔레그램 메시지에서 핵심 텍스트 추출"""
    # 불필요한 패턴 제거
    lines = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.search(r"^[*\s]*동\s*자료는|Compliance|저작권|법적\s*책임|t\.me/\w+$", line):
            continue
        lines.append(line)
    # 최대 15줄 제한
    return "\n".join(lines[:15])


def fetch_telegram_channel(
    channel: str,
    keywords: list[str],
    today: date,
    session: requests.Session,
    logger: logging.Logger,
    lookback_days: int = 1,
) -> list[dict]:
    """텔레그램 공개 채널에서 오늘(KST) 관련 메시지 수집"""
    url = f"https://t.me/s/{channel}"
    collected = []
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        posts = soup.select(".tgme_widget_message")

        cutoff = datetime.combine(today - timedelta(days=lookback_days), datetime.min.time()).replace(tzinfo=KST)

        for post in posts:
            dt_el = post.select_one(".tgme_widget_message_date time")
            if not dt_el:
                continue
            kst_dt = parse_telegram_dt(dt_el.get("datetime", ""))
            if not kst_dt or kst_dt < cutoff:
                continue

            text_el = post.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n") if text_el else ""
            if not text.strip():
                continue

            # 키워드 매칭
            if not any(kw in text for kw in keywords):
                continue

            # 외부 링크 수집 (t.me 제외)
            links = []
            if text_el:
                for a in text_el.find_all("a"):
                    href = a.get("href", "")
                    if href and "t.me" not in href and href.startswith("http"):
                        links.append(href)

            collected.append({
                "datetime": kst_dt.strftime("%Y.%m.%d %H:%M"),
                "text": clean_text(text),
                "links": links[:3],
            })

    except Exception as e:
        logger.warning(f"{channel}: 수집 오류 - {e}")

    return collected


def fetch_news_rss(cfg: dict, today: date, session: requests.Session, logger: logging.Logger) -> list[dict]:
    """한국경제·매일경제 RSS에서 오늘 시황 관련 기사 수집"""
    if not HAS_FEEDPARSER:
        return []
    keywords = cfg["keywords"]
    collected = []
    seen_titles: set[str] = set()

    for feed_cfg in cfg["feeds"]:
        url, label = feed_cfg["url"], feed_cfg["label"]
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "")
                # 오늘 날짜 필터 (published_parsed → UTC → KST)
                pp = entry.get("published_parsed")
                if pp:
                    pub_kst = datetime(*pp[:6], tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))
                    if pub_kst.date() != today:
                        continue
                # 키워드 필터
                if not any(kw in title for kw in keywords):
                    continue
                # 제목 중복 제거
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                collected.append({"title": title, "url": link, "label": label})
        except Exception as e:
            logger.warning(f"{cfg['name']} {label} RSS 오류: {e}")

    return collected[:cfg["max_items"]]


def collect_news(today: date, logger: logging.Logger, session: requests.Session) -> list[dict]:
    """언론사별 뉴스 수집 결과 반환"""
    news_results = []
    for cfg in NEWS_CONFIG:
        logger.info(f"{cfg['name']} 뉴스 수집 시작")
        items = fetch_news_rss(cfg, today, session, logger)
        logger.info(f"{cfg['name']}: {len(items)}건")
        news_results.append({"name": cfg["name"], "color": cfg["color"],
                              "home_url": cfg["home_url"], "items": items})
    return news_results


def collect_all(today: date, logger: logging.Logger) -> tuple[list[BriefResult], list[dict]]:
    session = make_session()
    results: list[BriefResult] = []

    for cfg in COMPANY_CONFIG:
        name = cfg["name"]
        lookback = cfg.get("lookback_days", 0)
        max_items = cfg.get("max_items", 5)
        logger.info(f"{name} 수집 시작: t.me/s/{cfg['channel']}")
        items = fetch_telegram_channel(
            channel=cfg["channel"],
            keywords=cfg["keywords"],
            today=today,
            session=session,
            logger=logger,
            lookback_days=lookback,
        )
        items = items[-max_items:] if len(items) > max_items else items

        status = "ok" if items else "fail"
        logger.info(f"{name}: {len(items)}개 메시지 ({status})")
        results.append(
            BriefResult(
                name=name,
                color=cfg["color"],
                items=items,
                status=status,
                fallback_url=cfg["fallback_url"],
            )
        )

    news = collect_news(today, logger, session)
    return results, news


# ── 이메일 구성 ─────────────────────────────────────────────────────────────

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def item_html(item: dict) -> str:
    """개별 메시지 HTML 카드"""
    text_html = item["text"].replace("\n", "<br>")
    link_html = ""
    if item["links"]:
        links_str = " &nbsp; ".join(
            f'<a href="{l}" style="color:#0066cc;font-size:12px;">자료 링크 →</a>'
            for l in item["links"]
        )
        link_html = f'<div style="margin-top:6px;">{links_str}</div>'

    return f"""
<div style="margin-bottom:10px;padding:10px 12px;background:#f8f9fa;border-radius:4px;">
  <div style="font-size:11px;color:#888;margin-bottom:4px;">{item['datetime']}</div>
  <div style="font-size:13px;line-height:1.7;color:#333;">{text_html}</div>
  {link_html}
</div>"""


def company_card_html(res: BriefResult) -> str:
    """회사별 섹션 HTML"""
    if res["status"] == "ok" and res["items"]:
        content_html = "".join(item_html(it) for it in res["items"])
    else:
        content_html = (
            f'<div style="font-size:13px;color:#999;padding:10px;">'
            f'텔레그램 채널에서 금일 브리핑을 찾지 못했습니다. '
            f'<a href="{res["fallback_url"]}" style="color:#0066cc;">리서치 페이지</a>를 직접 확인하세요.'
            f'</div>'
        )

    return f"""
<div style="margin-bottom:20px;border-left:4px solid {res['color']};
            padding-left:0;background:#fff;border-radius:0 6px 6px 0;
            box-shadow:0 1px 4px rgba(0,0,0,.08);">
  <div style="background:{res['color']};color:#fff;padding:8px 14px;
              border-radius:0 6px 0 0;">
    <span style="font-size:14px;font-weight:bold;">{res['name']}</span>
  </div>
  <div style="padding:10px 14px 12px;">
    {content_html}
  </div>
</div>"""


def news_card_html(news: dict) -> str:
    """언론사 뉴스 섹션 HTML"""
    if news["items"]:
        rows = "".join(
            f'<div style="padding:7px 0;border-bottom:1px solid #f0f0f0;">'
            f'<span style="background:#eef3fa;color:{news["color"]};font-size:11px;'
            f'padding:1px 6px;border-radius:3px;margin-right:6px;">{it["label"]}</span>'
            f'<a href="{it["url"]}" style="color:#222;font-size:13px;text-decoration:none;">{it["title"]}</a>'
            f'</div>'
            for it in news["items"]
        )
    else:
        rows = (f'<div style="font-size:13px;color:#999;padding:8px 0;">'
                f'금일 관련 기사가 없습니다. '
                f'<a href="{news["home_url"]}" style="color:#0066cc;">홈페이지</a>를 직접 확인하세요.</div>')

    return f"""
<div style="margin-bottom:20px;border-left:4px solid {news['color']};
            background:#fff;border-radius:0 6px 6px 0;
            box-shadow:0 1px 4px rgba(0,0,0,.08);">
  <div style="background:{news['color']};color:#fff;padding:8px 14px;border-radius:0 6px 0 0;">
    <span style="font-size:14px;font-weight:bold;">{news['name']}</span>
    <span style="font-size:11px;opacity:.8;margin-left:8px;">주식·채권·부동산 시황</span>
  </div>
  <div style="padding:10px 14px 12px;">{rows}</div>
</div>"""


def build_email(results: list[BriefResult], news_list: list[dict], today: date) -> str:
    date_str = f"{today.year}년 {today.month}월 {today.day}일 ({WEEKDAY_KO[today.weekday()]})"
    ok_cnt = sum(1 for r in results if r["status"] == "ok")
    total_items = sum(len(r["items"]) for r in results)

    summary_line = (
        f"{ok_cnt}개 증권사 브리핑 수집 완료 ({total_items}개 메시지)"
        if ok_cnt > 0
        else "브리핑 수집 실패 — 각 증권사 리서치 페이지를 확인하세요"
    )

    cards_html = "".join(company_card_html(r) for r in results)
    news_html  = "".join(news_card_html(n) for n in news_list)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <style>
    body{{font-family:'맑은 고딕','Malgun Gothic',sans-serif;margin:0;padding:0;background:#f0f2f5;color:#222;}}
    .wrap{{max-width:760px;margin:0 auto;padding:16px;}}
    .hdr{{background:#1a1a2e;color:#fff;padding:18px 24px;border-radius:8px 8px 0 0;text-align:center;}}
    .hdr h2{{margin:0 0 4px;font-size:20px;letter-spacing:.5px;}}
    .hdr p{{margin:0;font-size:13px;opacity:.8;}}
    .body{{padding:16px 0;}}
    .sec-title{{font-size:13px;font-weight:bold;color:#555;margin:20px 0 8px;padding-left:4px;
                border-left:3px solid #ccc;padding-left:8px;}}
    .ftr{{text-align:center;font-size:11px;color:#aaa;padding:12px;}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h2>📊 증권사 시황 브리핑 요약</h2>
    <p>{date_str} &nbsp;|&nbsp; {summary_line}</p>
  </div>
  <div class="body">
    <div class="sec-title">증권사 모닝 브리핑</div>
    {cards_html}
    <div class="sec-title">언론사 시황 기사 (한국경제·매일경제)</div>
    {news_html}
  </div>
  <div class="ftr">
    본 메일은 텔레그램 공개 채널 및 RSS 자동 수집 시스템입니다.
  </div>
</div>
</body>
</html>"""


# ── 이메일 발송 ─────────────────────────────────────────────────────────────

def send_email(html: str, today: date, cfg: dict, logger: logging.Logger) -> None:
    em = cfg["email"]
    date_str = f"{today.year}.{today.month:02d}.{today.day:02d}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[증권사시황BRIEF] {date_str}"
    msg["From"] = em["sender"]
    msg["To"] = ", ".join(em["recipients"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    smtp = smtplib.SMTP(em["smtp_host"], em["smtp_port"], timeout=30)
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(em["sender"], em["password"])
        smtp.sendmail(em["sender"], em["recipients"], msg.as_bytes())
        logger.info(f"이메일 발송 완료 → {em['recipients']}")
    except Exception as e:
        logger.error(f"발송 오류: {e}")
        raise
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


# ── 메인 ────────────────────────────────────────────────────────────────────

def run(logger: logging.Logger) -> None:
    today = date.today()
    if today.weekday() >= 5:
        logger.info(f"{today} 주말 — 스킵")
        return
    logger.info(f"=== 증권사시황BRIEF 시작: {today} ===")
    cfg = load_config()
    results, news = collect_all(today, logger)
    html = build_email(results, news, today)
    send_email(html, today, cfg, logger)
    logger.info("=== 완료 ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="증권사시황BRIEF")
    parser.add_argument("--now", action="store_true", help="즉시 실행")
    args = parser.parse_args()

    logger = setup_logging()

    if args.now:
        run(logger)
        return

    if not HAS_SCHEDULE:
        logger.error("schedule 패키지 없음. --now 옵션으로 실행하세요.")
        return

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("08:30").do(run, logger=logger)
    logger.info("스케줄 등록 완료 (월~금 08:30)")
    while True:
        schedule.run_pending()
        _time.sleep(30)


if __name__ == "__main__":
    main()
