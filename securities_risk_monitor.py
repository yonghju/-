"""
증권사 손실 및 운영리스크 뉴스 모니터링 시스템
- 주요 금융뉴스 RSS + 네이버 뉴스 검색 API로 관련 기사 탐지
- 위험도 자동 분류 (HIGH / MEDIUM / LOW)
- SQLite DB에 기사 이력 저장
- 이메일 + Slack / Teams Webhook 알림
"""

import re
import sys
import difflib
import feedparser
import requests
import smtplib
import json
import hashlib
import sqlite3
import schedule
import time
import logging
from logging.handlers import RotatingFileHandler
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

# ── 로깅 설정 ──────────────────────────────────────────────────────────────────
log_handler = RotatingFileHandler(
    "risk_monitor.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[log_handler],
)
logger = logging.getLogger(__name__)

# ── 설정 ───────────────────────────────────────────────────────────────────────
CONFIG_FILE = Path("config.json")
DB_FILE = Path("risk_monitor.db")

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("config.json이 없습니다.")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

# ── 키워드 & 위험도 가중치 ──────────────────────────────────────────────────────
# 기관 키워드 — 반드시 하나 이상 포함되어야 함
INSTITUTION_KEYWORDS = [
    "증권사", "증권회사", "금융투자회사", "자산운용사", "운용사",
    "투자은행", "증권",
    "은행", "시중은행", "저축은행", "인터넷은행",
]

# 가중치가 높을수록 심각
KEYWORD_WEIGHTS: dict[str, int] = {
    # HIGH 등급 유발 (10점 이상)
    "횡령": 15, "배임": 15, "기소": 12, "집단소송": 12,
    "시세조종": 12, "미공개정보": 12, "업무정지": 12, "영업정지": 12,
    "사기": 10, "검찰": 10, "금융사고": 8,
    # MEDIUM 등급 유발 (4~9점)
    "수사": 7, "과징금": 6, "불법거래": 6, "고발": 5,
    "제재": 5, "행정처분": 5, "불건전영업": 5, "시정명령": 4,
    "과태료": 4, "운영리스크": 4, "운영위험": 4, "규정위반": 4,
    # LOW 등급 유발 (1~3점)
    "손해배상": 3, "손실": 2, "결손": 2, "피해": 2, "분쟁": 2,
    "전산장애": 2, "시스템장애": 2, "시스템오류": 2, "주문오류": 2, "주문장애": 2,
    "민원": 1, "내부통제": 1, "컴플라이언스": 1,
}

SEVERITY_THRESHOLDS = {"HIGH": 10, "MEDIUM": 4, "LOW": 1}

# ── RSS 피드 ────────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {"name": "연합뉴스 경제",  "url": "https://www.yna.co.kr/rss/economy.xml"},
    {"name": "매일경제 경제",  "url": "https://www.mk.co.kr/rss/30000001/"},
    {"name": "아시아경제 주식", "url": "https://www.asiae.co.kr/rss/stock.htm"},
    {"name": "서울경제 증권",  "url": "https://www.sedaily.com/RSS/Finance"},
    {"name": "연합인포맥스",   "url": "https://news.einfomax.co.kr/rss/allArticle.xml"},
]

# ── 데이터 클래스 ───────────────────────────────────────────────────────────────
@dataclass
class Article:
    title: str
    url: str
    summary: str
    source: str
    published: str
    matched_keywords: list = field(default_factory=list)
    severity: str = "LOW"
    severity_score: int = 0


# ── 위험도 산정 ────────────────────────────────────────────────────────────────
def calc_severity(matched_keywords: list[str]) -> tuple[str, int]:
    score = sum(KEYWORD_WEIGHTS.get(kw, 1) for kw in matched_keywords)
    if score >= SEVERITY_THRESHOLDS["HIGH"]:
        return "HIGH", score
    if score >= SEVERITY_THRESHOLDS["MEDIUM"]:
        return "MEDIUM", score
    return "LOW", score


# ── 제목 유사도 중복 제거 ──────────────────────────────────────────────────────
def _normalize_title(title: str) -> str:
    """공백·특수문자 제거 후 소문자 정규화"""
    return re.sub(r"[\s\W]+", "", title).lower()


def dedup_by_title(articles: list, threshold: float = 0.7) -> list:
    """
    제목 유사도 >= threshold 인 기사를 같은 뉴스로 판단,
    가장 먼저 발견된 기사 하나만 유지.
    """
    kept: list = []
    for article in articles:
        norm = _normalize_title(article.title)
        duplicate = any(
            difflib.SequenceMatcher(None, norm, _normalize_title(e.title)).ratio() >= threshold
            for e in kept
        )
        if not duplicate:
            kept.append(article)
    return kept


# ── 관련성 판단 ────────────────────────────────────────────────────────────────
def is_relevant(title: str, summary: str) -> tuple[bool, list[str]]:
    """
    기관 키워드(증권사·자산운용사·은행 등) AND 위험 키워드가 모두 있어야 탐지.
    둘 중 하나만 있으면 제외.
    """
    text = title + " " + summary

    # 조건 1: 기관 키워드 포함 여부
    if not any(kw in text for kw in INSTITUTION_KEYWORDS):
        return False, []

    # 조건 2: 위험 키워드 포함 여부
    matched = [kw for kw in KEYWORD_WEIGHTS if kw in text]
    if not matched:
        return False, []

    return True, matched


# ── SQLite DB ──────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                url          TEXT UNIQUE NOT NULL,
                summary      TEXT,
                source       TEXT,
                published    TEXT,
                detected_at  TEXT NOT NULL,
                keywords     TEXT,
                severity     TEXT,
                severity_score INTEGER
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_detected ON articles(detected_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_severity ON articles(severity)")

def is_seen(article_id: str) -> bool:
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute("SELECT 1 FROM articles WHERE id=?", (article_id,)).fetchone()
        return row is not None


def is_title_seen_recently(title: str, hours: int = 24, threshold: float = 0.75) -> bool:
    """최근 hours시간 내 DB에 유사 제목 기사가 있으면 True (크로스-런 중복 방지)"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    norm = _normalize_title(title)
    with sqlite3.connect(DB_FILE) as con:
        rows = con.execute(
            "SELECT title FROM articles WHERE detected_at >= ?", (cutoff,)
        ).fetchall()
    for (db_title,) in rows:
        if difflib.SequenceMatcher(None, norm, _normalize_title(db_title)).ratio() >= threshold:
            return True
    return False

def save_article(a: Article):
    aid = hashlib.md5(a.url.encode()).hexdigest()
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            """INSERT OR IGNORE INTO articles
               (id, title, url, summary, source, published, detected_at, keywords, severity, severity_score)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                aid, a.title, a.url, a.summary, a.source, a.published,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ",".join(a.matched_keywords),
                a.severity, a.severity_score,
            ),
        )

def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


# ── RSS 수집 ────────────────────────────────────────────────────────────────────
def fetch_from_rss() -> list[Article]:
    articles = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RiskMonitor/1.0)"}

    for feed_info in RSS_FEEDS:
        try:
            resp = requests.get(feed_info["url"], headers=headers, timeout=10)
            feed = feedparser.parse(resp.content)
            logger.info(f"[RSS] {feed_info['name']}: {len(feed.entries)}건 수신")

            for entry in feed.entries:
                url = entry.get("link", "")
                if not url or is_seen(make_id(url)):
                    continue

                title = entry.get("title", "")
                raw_summary = entry.get("summary", entry.get("description", ""))
                summary = re.sub(r"<[^>]+>", "", raw_summary).strip()
                published = entry.get("published", datetime.now().strftime("%Y-%m-%d %H:%M"))

                relevant, matched_kw = is_relevant(title, summary)
                if relevant:
                    severity, score = calc_severity(matched_kw)
                    articles.append(Article(
                        title=title, url=url, summary=summary[:400],
                        source=feed_info["name"], published=published,
                        matched_keywords=matched_kw, severity=severity, severity_score=score,
                    ))
        except Exception as e:
            logger.warning(f"[RSS] {feed_info['name']} 수집 실패: {e}")

    return articles


# ── 네이버 뉴스 API ─────────────────────────────────────────────────────────────
def fetch_from_naver(cfg: dict) -> list[Article]:
    naver_cfg = cfg.get("naver_api", {})
    client_id = naver_cfg.get("client_id", "")
    client_secret = naver_cfg.get("client_secret", "")
    if not client_id or not client_secret:
        return []

    queries = [
        "증권사 손실", "증권사 횡령 배임", "증권사 금융사고",
        "증권사 제재 과징금", "증권사 손해배상",
        "자산운용사 손실", "자산운용사 횡령", "자산운용사 제재",
        "은행 횡령 배임", "은행 금융사고", "은행 손실",
    ]
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    articles = []

    for query in queries:
        try:
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": query, "display": 20, "sort": "date"},
                timeout=10,
            )
            for item in resp.json().get("items", []):
                url = item.get("originallink") or item.get("link", "")
                if not url or is_seen(make_id(url)):
                    continue
                title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                desc = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                relevant, matched_kw = is_relevant(title, desc)
                if relevant:
                    severity, score = calc_severity(matched_kw)
                    articles.append(Article(
                        title=title, url=url, summary=desc[:400],
                        source=f"네이버({query})", published=item.get("pubDate", ""),
                        matched_keywords=matched_kw, severity=severity, severity_score=score,
                    ))
        except Exception as e:
            logger.warning(f"[Naver] '{query}' 실패: {e}")

    return articles


# ── Slack Webhook 알림 ─────────────────────────────────────────────────────────
def send_slack(articles: list[Article], cfg: dict):
    webhook_url = cfg.get("slack_webhook_url", "")
    if not webhook_url:
        return

    severity_emoji = {"HIGH": ":red_circle:", "MEDIUM": ":large_yellow_circle:", "LOW": ":large_blue_circle:"}
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"증권사 Risk 모니터링 - {len(articles)}건 감지"}},
        {"type": "divider"},
    ]
    for a in articles[:10]:  # Slack 블록 한도
        emoji = severity_emoji.get(a.severity, ":white_circle:")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{a.severity}* | <{a.url}|{a.title}>\n"
                    f">출처: {a.source}  |  {a.published}\n"
                    f">키워드: `{'` `'.join(a.matched_keywords)}`"
                ),
            },
        })

    try:
        resp = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
        if resp.status_code == 200:
            logger.info("Slack 알림 발송 완료")
        else:
            logger.warning(f"Slack 응답 오류: {resp.status_code}")
    except Exception as e:
        logger.error(f"Slack 알림 실패: {e}")


# ── Teams Webhook 알림 ─────────────────────────────────────────────────────────
def send_teams(articles: list[Article], cfg: dict):
    webhook_url = cfg.get("teams_webhook_url", "")
    if not webhook_url:
        return

    severity_color = {"HIGH": "attention", "MEDIUM": "warning", "LOW": "accent"}
    facts_list = []
    for a in articles[:15]:
        facts_list.append({
            "type": "FactSet",
            "facts": [
                {"title": "등급", "value": a.severity},
                {"title": "출처", "value": a.source},
                {"title": "키워드", "value": ", ".join(a.matched_keywords)},
                {"title": "링크", "value": f"[기사 보기]({a.url})"},
            ],
        })

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                     "text": f"증권사 Risk 모니터링 - {len(articles)}건 감지"},
                    *facts_list,
                ],
            },
        }],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        logger.info(f"Teams 알림 발송: {resp.status_code}")
    except Exception as e:
        logger.error(f"Teams 알림 실패: {e}")


# ── 이메일 알림 ────────────────────────────────────────────────────────────────
def send_email(articles: list[Article], cfg: dict):
    email_cfg = cfg.get("email", {})
    sender = email_cfg.get("sender", "")
    # 플레이스홀더 또는 미설정 상태면 건너뜀
    if not sender or not sender.isascii() or "@" not in sender:
        logger.info("이메일 미설정 — 발송 건너뜀 (config.json의 email.sender를 설정하세요)")
        return
    if not email_cfg.get("password") or "비밀번호" in email_cfg.get("password", ""):
        logger.info("이메일 비밀번호 미설정 — 발송 건너뜀")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    severity_color = {"HIGH": "#c0392b", "MEDIUM": "#e67e22", "LOW": "#2980b9"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[증권사 Risk 모니터링] {len(articles)}건 감지 ({now_str})"
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg["recipients"])

    # Plain text
    lines = [f"▣ 증권사 손실/운영리스크 관련 기사 {len(articles)}건\n"]
    for i, a in enumerate(articles, 1):
        lines += [
            f"[{i}] [{a.severity}] {a.title}",
            f"    출처: {a.source}  |  일시: {a.published}",
            f"    URL: {a.url}",
            f"    키워드: {', '.join(a.matched_keywords)}  |  점수: {a.severity_score}",
            f"    요약: {a.summary[:200]}", "",
        ]

    # HTML
    cards = ""
    for a in articles:
        color = severity_color.get(a.severity, "#555")
        badges = "".join(
            f'<span style="background:{color};color:#fff;padding:2px 7px;border-radius:3px;'
            f'font-size:11px;margin:2px;display:inline-block;">{kw}</span>'
            for kw in a.matched_keywords
        )
        cards += f"""
        <div style="border-left:4px solid {color};padding:12px 16px;margin:10px 0;
                    background:#fafafa;border-radius:0 6px 6px 0;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                <span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;
                             font-size:12px;font-weight:bold;">{a.severity}</span>
                <span style="color:#888;font-size:12px;">점수 {a.severity_score}</span>
            </div>
            <h3 style="margin:4px 0;">
                <a href="{a.url}" style="color:#2c3e50;text-decoration:none;">{a.title}</a>
            </h3>
            <p style="margin:4px 0;color:#7f8c8d;font-size:13px;">
                <b>출처:</b> {a.source} &nbsp;|&nbsp; <b>일시:</b> {a.published}
            </p>
            <p style="margin:6px 0;">{badges}</p>
            <p style="margin:6px 0;color:#555;font-size:13px;">{a.summary[:300]}</p>
        </div>"""

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:820px;margin:auto;padding:20px;">
        <h2 style="color:#c0392b;border-bottom:2px solid #c0392b;padding-bottom:8px;">
            증권사 Risk 모니터링 알림</h2>
        <p style="color:#555;">탐지 시각: <b>{now_str}</b> &nbsp;|&nbsp; 신규: <b>{len(articles)}건</b>
            &nbsp;|&nbsp;
            <span style="color:#c0392b;">HIGH {sum(1 for a in articles if a.severity=='HIGH')}</span> /
            <span style="color:#e67e22;">MEDIUM {sum(1 for a in articles if a.severity=='MEDIUM')}</span> /
            <span style="color:#2980b9;">LOW {sum(1 for a in articles if a.severity=='LOW')}</span>
        </p>
        {cards}
        <hr style="margin-top:30px;">
        <p style="color:#aaa;font-size:12px;">본 메일은 자동 모니터링 시스템에 의해 발송되었습니다.</p>
    </body></html>"""

    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], int(email_cfg["smtp_port"])) as server:
            server.ehlo()
            server.starttls()
            server.login(email_cfg["sender"], email_cfg["password"])
            server.send_message(msg)
        logger.info(f"이메일 발송 완료: {len(articles)}건")
    except Exception as e:
        logger.error(f"이메일 발송 실패: {e}")


# ── 콘솔 출력 ──────────────────────────────────────────────────────────────────
SEVERITY_LABEL = {"HIGH": "[HIGH]  ", "MEDIUM": "[MEDIUM]", "LOW": "[LOW]   "}

def print_articles(articles: list[Article]):
    if not articles:
        return
    high = sum(1 for a in articles if a.severity == "HIGH")
    med  = sum(1 for a in articles if a.severity == "MEDIUM")
    low  = sum(1 for a in articles if a.severity == "LOW")
    print(f"\n{'='*72}")
    print(f"  감지 기사 {len(articles)}건  |  HIGH {high}  MEDIUM {med}  LOW {low}")
    print("="*72)
    for i, a in enumerate(articles, 1):
        label = SEVERITY_LABEL.get(a.severity, a.severity)
        print(f"\n[{i}] {label} (점수:{a.severity_score})  {a.title}")
        print(f"     출처: {a.source}  |  {a.published}")
        print(f"     URL: {a.url}")
        print(f"     키워드: {', '.join(a.matched_keywords)}")
        if a.summary:
            print(f"     요약: {a.summary[:120]}...")
    print("="*72 + "\n")


# ── 메인 체크 ──────────────────────────────────────────────────────────────────
def run_check():
    logger.info("─── 뉴스 체크 시작 ───")
    cfg = load_config()

    articles: list[Article] = []
    articles += fetch_from_rss()
    articles += fetch_from_naver(cfg)

    # URL 기준 중복 제거
    seen_urls: set[str] = set()
    url_deduped: list[Article] = []
    for a in articles:
        if a.url not in seen_urls:
            seen_urls.add(a.url)
            url_deduped.append(a)

    # 제목 유사도 기준 중복 제거 (동일 기사가 여러 언론사에 실린 경우 — 현재 배치)
    deduped = dedup_by_title(url_deduped)
    if len(url_deduped) != len(deduped):
        logger.info(f"유사 기사 중복 제거: {len(url_deduped)}건 → {len(deduped)}건")

    # 최근 24시간 DB 기사와 제목 유사도 비교 (이전 사이클 중복 방지)
    unique = []
    for a in deduped:
        if is_title_seen_recently(a.title):
            logger.info(f"[중복-DB] 스킵: {a.title[:60]}")
        else:
            unique.append(a)
    if len(deduped) != len(unique):
        logger.info(f"DB 유사 제목 중복 제거: {len(deduped)}건 → {len(unique)}건")

    # DB 저장
    for a in unique:
        save_article(a)

    # HIGH 우선 정렬
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    unique.sort(key=lambda a: (severity_order.get(a.severity, 9), -a.severity_score))

    if unique:
        logger.info(f"신규 관련 기사 {len(unique)}건 발견")
        print_articles(unique)
        send_email(unique, cfg)
        send_slack(unique, cfg)
        send_teams(unique, cfg)
    else:
        logger.info("신규 관련 기사 없음")

    logger.info("─── 뉴스 체크 완료 ───\n")


# ── 진입점 ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    cfg = load_config()
    interval = cfg.get("check_interval_minutes", 30)

    # GitHub Actions 등 1회 실행 모드
    if "--once" in sys.argv:
        logger.info("리스크 모니터 1회 실행 (--once)")
        run_check()
        return

    logger.info(f"증권사 리스크 모니터링 시작 (체크 주기: {interval}분)")
    run_check()
    schedule.every(interval).minutes.do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
