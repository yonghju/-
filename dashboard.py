"""
증권사 리스크 모니터링 웹 대시보드
실행: python dashboard.py
접속: http://localhost:5000
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

DB_FILE = Path("risk_monitor.db")

app = Flask(__name__)

# ── DB 헬퍼 ────────────────────────────────────────────────────────────────────
def query_db(sql: str, params: tuple = ()) -> list[dict]:
    if not DB_FILE.exists():
        return []
    with sqlite3.connect(DB_FILE) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def get_stats() -> dict:
    if not DB_FILE.exists():
        return {"total": 0, "high": 0, "medium": 0, "low": 0, "today": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    rows = query_db("SELECT severity, COUNT(*) as cnt FROM articles GROUP BY severity")
    counts = {r["severity"]: r["cnt"] for r in rows}
    today_cnt = query_db(
        "SELECT COUNT(*) as cnt FROM articles WHERE detected_at LIKE ?", (f"{today}%",)
    )
    return {
        "total":  sum(counts.values()),
        "high":   counts.get("HIGH", 0),
        "medium": counts.get("MEDIUM", 0),
        "low":    counts.get("LOW", 0),
        "today":  today_cnt[0]["cnt"] if today_cnt else 0,
    }

# ── 메인 HTML 템플릿 ────────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>증권사 리스크 모니터링 대시보드</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #333; }
  header { background: #1a2740; color: #fff; padding: 16px 24px;
           display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 20px; }
  header .sub { font-size: 13px; color: #8fa3bf; }
  .container { max-width: 1200px; margin: 24px auto; padding: 0 16px; }
  .stat-row { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat-card { flex: 1; min-width: 140px; background: #fff; border-radius: 8px;
               padding: 18px; box-shadow: 0 1px 4px rgba(0,0,0,.08); text-align: center; }
  .stat-card .num { font-size: 36px; font-weight: 700; }
  .stat-card .label { font-size: 13px; color: #888; margin-top: 4px; }
  .stat-card.high  .num { color: #c0392b; }
  .stat-card.med   .num { color: #e67e22; }
  .stat-card.low   .num { color: #2980b9; }
  .stat-card.total .num { color: #2c3e50; }
  .stat-card.today .num { color: #27ae60; }
  .filter-row { background: #fff; border-radius: 8px; padding: 14px 18px;
                margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap;
                align-items: center; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .filter-row select, .filter-row input { padding: 7px 10px; border: 1px solid #ddd;
                                          border-radius: 5px; font-size: 14px; }
  .filter-row button { padding: 7px 18px; background: #1a2740; color: #fff;
                       border: none; border-radius: 5px; cursor: pointer; font-size: 14px; }
  .filter-row button:hover { background: #2c3e50; }
  .article { background: #fff; border-radius: 8px; padding: 16px 20px;
             margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
             border-left: 5px solid #ccc; }
  .article.HIGH   { border-left-color: #c0392b; }
  .article.MEDIUM { border-left-color: #e67e22; }
  .article.LOW    { border-left-color: #2980b9; }
  .article-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .badge { padding: 3px 9px; border-radius: 4px; font-size: 12px;
           font-weight: 700; color: #fff; }
  .badge.HIGH   { background: #c0392b; }
  .badge.MEDIUM { background: #e67e22; }
  .badge.LOW    { background: #2980b9; }
  .score { font-size: 12px; color: #aaa; }
  .title a { color: #1a2740; text-decoration: none; font-size: 16px; font-weight: 600; }
  .title a:hover { text-decoration: underline; }
  .meta { font-size: 13px; color: #888; margin: 4px 0; }
  .kw-list { margin: 6px 0; }
  .kw { display: inline-block; background: #eef; color: #1a2740;
        padding: 2px 7px; border-radius: 3px; font-size: 12px; margin: 2px; }
  .kw.HIGH   { background: #fde; color: #c0392b; }
  .kw.MEDIUM { background: #fef3e2; color: #b7770d; }
  .summary { font-size: 14px; color: #555; line-height: 1.5; margin-top: 6px; }
  .empty { text-align: center; padding: 60px; color: #aaa; font-size: 16px; }
  .pagination { text-align: center; margin: 20px 0; }
  .pagination button { padding: 6px 14px; margin: 2px; border: 1px solid #ddd;
                       background: #fff; border-radius: 4px; cursor: pointer; }
  .pagination button.active { background: #1a2740; color: #fff; border-color: #1a2740; }
  .refresh-info { font-size: 12px; color: #aaa; text-align: right; margin-bottom: 8px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>증권사 리스크 모니터링</h1>
    <div class="sub">Securities Firm Loss & Operational Risk News Monitor</div>
  </div>
  <div class="sub" id="clock"></div>
</header>

<div class="container">
  <!-- 통계 카드 -->
  <div class="stat-row" id="stats"></div>

  <!-- 필터 -->
  <div class="filter-row">
    <select id="f-severity">
      <option value="">전체 등급</option>
      <option value="HIGH">🔴 HIGH</option>
      <option value="MEDIUM">🟡 MEDIUM</option>
      <option value="LOW">🔵 LOW</option>
    </select>
    <select id="f-days">
      <option value="7">최근 7일</option>
      <option value="30">최근 30일</option>
      <option value="90">최근 90일</option>
      <option value="0">전체</option>
    </select>
    <input id="f-keyword" type="text" placeholder="키워드 검색 (제목/키워드)" style="flex:1;min-width:180px;">
    <button onclick="loadArticles(1)">검색</button>
    <button onclick="resetFilter()" style="background:#888;">초기화</button>
  </div>

  <div class="refresh-info" id="last-refresh"></div>
  <div id="articles"></div>
  <div class="pagination" id="pagination"></div>
</div>

<script>
let currentPage = 1;
const PAGE_SIZE = 20;

function fmtDate(s) { return s ? s.slice(0, 16) : '-'; }

async function loadStats() {
  const res = await fetch('/api/stats');
  const d = await res.json();
  document.getElementById('stats').innerHTML = `
    <div class="stat-card total"><div class="num">${d.total}</div><div class="label">전체 기사</div></div>
    <div class="stat-card today"><div class="num">${d.today}</div><div class="label">오늘 감지</div></div>
    <div class="stat-card high"><div class="num">${d.high}</div><div class="label">HIGH 위험</div></div>
    <div class="stat-card med"><div class="num">${d.medium}</div><div class="label">MEDIUM 주의</div></div>
    <div class="stat-card low"><div class="num">${d.low}</div><div class="label">LOW 관찰</div></div>
  `;
}

async function loadArticles(page = 1) {
  currentPage = page;
  const severity = document.getElementById('f-severity').value;
  const days = document.getElementById('f-days').value;
  const keyword = document.getElementById('f-keyword').value;

  const params = new URLSearchParams({ page, page_size: PAGE_SIZE });
  if (severity) params.set('severity', severity);
  if (days && days !== '0') params.set('days', days);
  if (keyword) params.set('keyword', keyword);

  const res = await fetch('/api/articles?' + params);
  const d = await res.json();

  const el = document.getElementById('articles');
  if (!d.items.length) {
    el.innerHTML = '<div class="empty">조건에 맞는 기사가 없습니다.</div>';
    document.getElementById('pagination').innerHTML = '';
    return;
  }

  el.innerHTML = d.items.map(a => {
    const kws = (a.keywords || '').split(',').filter(Boolean);
    const badges = kws.map(k =>
      `<span class="kw ${a.severity}">${k}</span>`
    ).join('');
    return `
    <div class="article ${a.severity}">
      <div class="article-header">
        <span class="badge ${a.severity}">${a.severity}</span>
        <span class="score">점수 ${a.severity_score}</span>
      </div>
      <div class="title"><a href="${a.url}" target="_blank">${a.title}</a></div>
      <div class="meta">📰 ${a.source} &nbsp;|&nbsp; 📅 ${fmtDate(a.published)} &nbsp;|&nbsp; 🕐 감지: ${fmtDate(a.detected_at)}</div>
      <div class="kw-list">${badges}</div>
      <div class="summary">${a.summary || ''}</div>
    </div>`;
  }).join('');

  // 페이지네이션
  const total_pages = Math.ceil(d.total / PAGE_SIZE);
  let pag = '';
  for (let i = 1; i <= total_pages; i++) {
    if (i === 1 || i === total_pages || Math.abs(i - page) <= 2) {
      pag += `<button class="${i === page ? 'active' : ''}" onclick="loadArticles(${i})">${i}</button>`;
    } else if (Math.abs(i - page) === 3) {
      pag += '<span>…</span>';
    }
  }
  document.getElementById('pagination').innerHTML = pag;
  document.getElementById('last-refresh').textContent =
    '마지막 갱신: ' + new Date().toLocaleTimeString('ko-KR');
}

function resetFilter() {
  document.getElementById('f-severity').value = '';
  document.getElementById('f-days').value = '7';
  document.getElementById('f-keyword').value = '';
  loadArticles(1);
}

function updateClock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleString('ko-KR');
}

// 초기 로드 & 자동 갱신
loadStats();
loadArticles(1);
setInterval(() => { loadStats(); loadArticles(currentPage); }, 5 * 60 * 1000);
setInterval(updateClock, 1000);
updateClock();

// 엔터키 검색
document.getElementById('f-keyword').addEventListener('keydown', e => {
  if (e.key === 'Enter') loadArticles(1);
});
</script>
</body>
</html>
"""

# ── API 엔드포인트 ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/articles")
def api_articles():
    page      = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 20))
    severity  = request.args.get("severity", "")
    days      = request.args.get("days", "")
    keyword   = request.args.get("keyword", "")

    conditions = []
    params: list = []

    if severity:
        conditions.append("severity = ?")
        params.append(severity)

    if days:
        cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        conditions.append("detected_at >= ?")
        params.append(cutoff)

    if keyword:
        conditions.append("(title LIKE ? OR keywords LIKE ? OR summary LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_row = query_db(f"SELECT COUNT(*) as cnt FROM articles {where}", tuple(params))
    total = count_row[0]["cnt"] if count_row else 0

    offset = (page - 1) * page_size
    rows = query_db(
        f"SELECT * FROM articles {where} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )

    return jsonify({"total": total, "items": rows})


# ── 진입점 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DB_FILE.exists():
        print("⚠  risk_monitor.db 파일이 없습니다. securities_risk_monitor.py를 먼저 실행하세요.")
    print("대시보드 시작 → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
