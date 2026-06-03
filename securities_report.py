"""
증권사 실적현황 자동 수집 및 이메일 발송
===========================================
기준 결산기: 12월말 / 3월말 / 6월말 / 9월말
발송 시점  : 3월 말일 / 5월 말일 / 8월 말일 / 11월 말일  오전 08:30
데이터 소스: 금융감독원 DART 오픈API (opendart.fss.or.kr)

사전 준비:
  1) https://opendart.fss.or.kr/api/ 에서 무료 API 키 발급
  2) config.json 에 "dart_api_key": "발급키" 추가

실행:
  py securities_report.py          # 스케줄러 시작 (매월 말일 08:30 자동 발송)
  py securities_report.py --now    # 즉시 실행 (현재 시점 기준)
  py securities_report.py --now --month 5   # 5월 공시 기준으로 즉시 실행
"""

import json, re, sys, time, smtplib, schedule, logging
import xml.etree.ElementTree as ET
from datetime import datetime
from calendar import monthrange
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber as _pdfplumber
    _HAS_PDF = True
except ImportError:
    _HAS_PDF = False

# ── 로깅 ────────────────────────────────────────────────────────────────────
log_handler = RotatingFileHandler(
    "securities_report.log", maxBytes=3*1024*1024, backupCount=2, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")
DART_BASE   = "https://opendart.fss.or.kr/api"
FISIS_BASE  = "http://fisis.fss.or.kr/openapi"
HEADERS     = {"User-Agent": "Mozilla/5.0"}

# ── 증권사 목록 (표시명 → DART 검색명 후보 순서대로) ────────────────────────
COMPANY_MAP: dict[str, list[str]] = {
    "SK증권":         ["SK증권"],
    "상상인증권":     ["상상인증권"],
    "한양증권":       ["한양증권"],
    "LS증권":         ["LS증권", "이베스트투자증권"],
    "유진증권":       ["유진투자증권", "유진증권"],
    "디에스증권":     ["디에스투자증권", "DS투자증권"],
    "케이프증권":     ["케이프투자증권", "케이프증권"],
    "리딩투자증권":   ["리딩투자증권"],
    "카카오페이증권": ["카카오페이증권"],
    "토스증권":       ["토스증권"],
    "코리아에셋증권": ["코리아에셋투자증권", "코리아에셋"],
    "다올투자증권":   ["다올투자증권"],
    "부국증권":       ["부국증권"],
}

# ── FISIS finance_cd 매핑 (금감원 FISIS Open API) ───────────────────────────
FISIS_CODE_MAP: dict[str, str] = {
    "SK증권":         "0010114",
    "상상인증권":     "0010104",
    "한양증권":       "0010098",
    "LS증권":         "0010137",
    "유진증권":       "0010097",
    "디에스증권":     "0011980",
    "케이프증권":     "0011978",
    "리딩투자증권":   "0010132",
    "카카오페이증권": "0011982",
    "토스증권":       "0017713",
    "코리아에셋증권": "0010138",
    "다올투자증권":   "0011981",
    "부국증권":       "0010102",
}

# ── 주식코드 (corpCode.xml 우선 조회용) ─────────────────────────────────────
STOCK_MAP: dict[str, str] = {
    "SK증권":         "001510",
    "상상인증권":     "001290",
    "한양증권":       "001750",
    "LS증권":         "078020",
    "유진증권":       "001200",
    "리딩투자증권":   "086390",
    "코리아에셋증권": "190650",
    "다올투자증권":   "030210",
    "부국증권":       "001270",
}

# ── 분기 한글 표기 ────────────────────────────────────────────────────────────
REPRT_QTR_KO = {"11011": "4분기", "11012": "2분기", "11013": "1분기", "11014": "3분기"}
REPRT_QTR_FY = {"11011": "4/4분기", "11012": "2/4분기", "11013": "1/4분기", "11014": "3/4분기"}

# ── corp_code 메모리 캐시 ────────────────────────────────────────────────────
_corp_cache: dict[str, str | None] = {}
_corp_code_db: dict | None = None  # {"by_stock": {...}, "by_name": {...}}


# ════════════════════════════════════════════════════════════════════════════
# 설정 & 결산기
# ════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_period_info(override_month: int | None = None) -> dict:
    """공시월 → 보고서 종류/기준기간 매핑"""
    now   = datetime.now()
    month = override_month or now.month
    year  = now.year

    # 3월말 공시: 전년도 12월말(사업보고서)
    if month <= 3:
        return dict(bsns_year=str(year - 1), reprt_code="11011",
                    period=f"{year-1}년 12월말", period_short=f"{year-1}/12",
                    bgn_de=f"{year-1}1201", end_de=f"{year}0331")
    # 5월말 공시: 당해 3월말(1분기보고서)
    elif month <= 5:
        return dict(bsns_year=str(year), reprt_code="11013",
                    period=f"{year}년 3월말", period_short=f"{year}/03",
                    bgn_de=f"{year}0301", end_de=f"{year}0531")
    # 8월말 공시: 당해 6월말(반기보고서)
    elif month <= 8:
        return dict(bsns_year=str(year), reprt_code="11012",
                    period=f"{year}년 6월말", period_short=f"{year}/06",
                    bgn_de=f"{year}0601", end_de=f"{year}0831")
    # 11월말 공시: 당해 9월말(3분기보고서)
    else:
        return dict(bsns_year=str(year), reprt_code="11014",
                    period=f"{year}년 9월말", period_short=f"{year}/09",
                    bgn_de=f"{year}0901", end_de=f"{year}1130")


# ════════════════════════════════════════════════════════════════════════════
# FISIS API (순자본비율·3개월유동성비율)
# ════════════════════════════════════════════════════════════════════════════

def _period_to_basemm(period: dict) -> str:
    """보고서 기간 → FISIS base_month (YYYYMM)"""
    code = period["reprt_code"]
    year = period["bsns_year"]
    return {"11011": f"{year}12", "11013": f"{year}03",
            "11012": f"{year}06", "11014": f"{year}09"}.get(code, f"{year}12")


def fetch_fisis_ratios(fisis_key: str, finance_cd: str, base_month: str) -> tuple[dict, str]:
    """FISIS API에서 순자본비율(SF308/E)·3개월유동성비율(SF209/C) 조회.
    target 기간 데이터가 없으면 최근 8분기 내 가장 최신 데이터를 사용.
    Returns: (결과 dict, 실제 사용된 base_month 또는 '')"""
    result: dict[str, float | None] = {"순자본비율": None, "유동성비율": None}
    used_month = ""
    # 8분기(24개월) 전 YYYYMM 계산 (FISIS 공표 지연 감안)
    yr, mm = int(base_month[:4]), int(base_month[4:])
    idx = yr * 12 + (mm - 1) - 24   # 0-indexed 월 기준 24개월 전
    start_mm = f"{idx // 12}{idx % 12 + 1:02d}"
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        for list_no, acc_cd, key in [
            ("SF308", "E", "순자본비율"),
            ("SF209", "C", "유동성비율"),
        ]:
            r = sess.get(f"{FISIS_BASE}/statisticsInfoSearch.json", params={
                "lang": "kr", "auth": fisis_key,
                "financeCd": finance_cd, "listNo": list_no, "accountCd": acc_cd,
                "term": "Q", "startBaseMm": start_mm, "endBaseMm": base_month,
            }, timeout=15)
            raw = r.content.decode("utf-8", errors="replace")
            data = json.loads(raw)
            items = data.get("result", {}).get("list", [])
            if items:
                latest = sorted(items, key=lambda x: x.get("base_month", ""))[-1]
                try:
                    result[key] = float(latest.get("a", ""))
                    used_month = latest.get("base_month", "")
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        logger.debug(f"FISIS API 오류 ({finance_cd}): {e}")
    return result, used_month


# ════════════════════════════════════════════════════════════════════════════
# DART API 유틸
# ════════════════════════════════════════════════════════════════════════════

def dart_get(dart_key: str, endpoint: str, **params) -> dict:
    params["crtfc_key"] = dart_key
    try:
        r = requests.get(f"{DART_BASE}/{endpoint}", params=params,
                         headers=HEADERS, timeout=20)
        return r.json()
    except Exception as e:
        logger.warning(f"DART API [{endpoint}]: {e}")
        return {}


def _load_corp_codes(dart_key: str) -> dict:
    """corpCode.xml 다운로드 및 파싱 (세션당 1회 캐시)"""
    global _corp_code_db
    if _corp_code_db is not None:
        return _corp_code_db

    try:
        r = requests.get(f"{DART_BASE}/corpCode.xml",
                         params={"crtfc_key": dart_key},
                         headers=HEADERS, timeout=60)
        zf = ZipFile(BytesIO(r.content))
        xml_data = zf.read("CORPCODE.xml")
        root = ET.fromstring(xml_data)

        by_stock: dict[str, str] = {}
        by_name:  dict[str, str] = {}
        for item in root.findall("list"):
            name  = item.findtext("corp_name", "")
            code  = item.findtext("corp_code", "")
            stock = item.findtext("stock_code", "").strip()
            by_name[name] = code
            if stock:
                by_stock[stock] = code

        _corp_code_db = {"by_stock": by_stock, "by_name": by_name}
        logger.info(f"corpCode.xml 로드 완료: {len(by_name):,}개 기업")
    except Exception as e:
        logger.error(f"corpCode.xml 로드 실패: {e}")
        _corp_code_db = {"by_stock": {}, "by_name": {}}

    return _corp_code_db


def find_corp_code(dart_key: str, display: str, candidates: list[str]) -> str | None:
    """주식코드 → 정확명 → 유사명 순서로 corp_code 조회 (corpCode.xml 기반)"""
    if display in _corp_cache:
        return _corp_cache[display]

    db       = _load_corp_codes(dart_key)
    by_stock = db["by_stock"]
    by_name  = db["by_name"]
    code     = None

    # 1) 주식코드로 조회
    stock = STOCK_MAP.get(display)
    if stock and stock in by_stock:
        code = by_stock[stock]
        logger.info(f"  {display}: corp_code={code} (주식코드={stock})")

    # 2) 정확명 조회
    if not code:
        for name in candidates:
            if name in by_name:
                code = by_name[name]
                logger.info(f"  {display}: corp_code={code} (정확명={name})")
                break

    # 3) 유사명 조회 (증권 포함)
    if not code:
        for name in candidates:
            for corp_nm, c in by_name.items():
                if ((name in corp_nm or corp_nm in name)
                        and "증권" in corp_nm and len(corp_nm) > 3):
                    code = c
                    logger.info(f"  {display}: corp_code={code} (유사명={corp_nm})")
                    break
            if code:
                break

    if not code:
        logger.warning(f"  {display}: DART 검색 실패")

    _corp_cache[display] = code
    return code


# ════════════════════════════════════════════════════════════════════════════
# 재무제표 수집 (자본금, 자기자본, 영업이익, 당기순익)
# ════════════════════════════════════════════════════════════════════════════

def _dart_financials_once(dart_key: str, corp_code: str,
                          bsns_year: str, reprt_code: str) -> dict:
    """단일 기간 DART 재무제표 조회 (개별OFS 우선)"""
    empty = {"자본금": None, "자기자본": None, "영업이익": None, "당기순익": None}
    for fs_div in ("OFS", "CFS"):
        data  = dart_get(dart_key, "fnlttSinglAcntAll.json",
                         corp_code=corp_code, bsns_year=bsns_year,
                         reprt_code=reprt_code, fs_div=fs_div)
        items = data.get("list", [])
        if not items:
            continue
        res = dict(empty)
        for item in items:
            sj  = item.get("sj_div", "")
            nm  = item.get("account_nm", "").strip()
            raw = item.get("thstrm_amount", "").replace(",", "").strip()
            try:
                amt = int(raw)
            except (ValueError, AttributeError):
                continue
            if sj == "BS" and nm.endswith("자본금") and "조정" not in nm and res["자본금"] is None:
                res["자본금"] = amt
            elif sj == "BS" and nm in ("자본총계", "자기자본") and res["자기자본"] is None:
                res["자기자본"] = amt
            elif sj in ("IS","CIS") and "영업이익" in nm and "기타" not in nm and res["영업이익"] is None:
                res["영업이익"] = amt
            elif sj in ("IS","CIS") and ("당기순이익" in nm or "당기순손익" in nm or "분기순이익" in nm) and res["당기순익"] is None:
                res["당기순익"] = amt
        if any(v is not None for v in res.values()):
            return res
    return empty


def get_financials(dart_key: str, corp_code: str,
                   bsns_year: str, reprt_code: str) -> dict:
    """DART 재무제표 조회 (개별기준). 해당 분기 없으면 연간 → 전년도 연간 fallback."""
    empty = {"자본금": None, "자기자본": None, "영업이익": None, "당기순익": None}

    # 1차: 당해 기간
    res = _dart_financials_once(dart_key, corp_code, bsns_year, reprt_code)
    if any(v is not None for v in res.values()):
        return res

    # 2차 fallback: 당해 연간(사업보고서)
    if reprt_code != "11011":
        res = _dart_financials_once(dart_key, corp_code, bsns_year, "11011")
        if any(v is not None for v in res.values()):
            logger.info(f"    fallback → {bsns_year}년 연간(사업보고서)")
            return res

    # 3차 fallback: 전년도 연간
    prev = str(int(bsns_year) - 1)
    res = _dart_financials_once(dart_key, corp_code, prev, "11011")
    if any(v is not None for v in res.values()):
        logger.info(f"    fallback → {prev}년 연간(사업보고서)")
        return res

    return empty


# ════════════════════════════════════════════════════════════════════════════
# 홈페이지 영업보고서 PDF 파싱 (DART 비상장사 대체)
# ════════════════════════════════════════════════════════════════════════════

def _parse_yb_pdf(pdf_bytes: bytes) -> dict:
    """영업보고서 PDF에서 재무/비율 데이터 추출 (개별기준)"""
    result = {"자본금": None, "자기자본": None, "영업이익": None, "당기순익": None,
              "순자본비율": None, "레버리지비율": None, "유동성비율": None}
    if not _HAS_PDF:
        return result

    def _big_ints(s: str) -> list[int]:
        return [int(m.replace(",", "")) for m in re.findall(r'-?\d{1,3}(?:,\d{3})+|-?\d{7,}', s)]

    try:
        with _pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            full = "\n".join(p.extract_text() or "" for p in pdf.pages)

        lines = full.split("\n")
        for i, line in enumerate(lines):
            s = line.strip()

            # 자기자본 (대차대조표 자본총계)
            if s.startswith("자본총계") and result["자기자본"] is None:
                ns = _big_ints(s)
                if ns:
                    result["자기자본"] = ns[0]

            # 자본금 (Ⅰ. 자본금 또는 보통주자본금)
            if result["자본금"] is None and ("Ⅰ." in s or "보통주자본금" in s) and "자본금" in s:
                ns = _big_ints(s)
                if ns:
                    result["자본금"] = ns[0]

            # 영업이익 (손익계산서 Ⅲ.)
            if result["영업이익"] is None and "Ⅲ." in s and "영업이익" in s:
                ns = _big_ints(s)
                if ns:
                    result["영업이익"] = ns[0]

            # 당기순이익 (Ⅹ.)
            if result["당기순익"] is None and "Ⅹ." in s and "당기순이익" in s:
                ns = _big_ints(s)
                if ns:
                    result["당기순익"] = ns[0]

        # 순자본비율(개별) — "순자본비율Ⅰ(개별)" 섹션 내에서 찾기
        if result["순자본비율"] is None:
            idx_indiv = full.find("순자본비율Ⅰ(개별)")
            if idx_indiv < 0:
                idx_indiv = 0
            kw_r = "순자본비율(=(3/5)×100)"
            idx_r = full.find(kw_r, idx_indiv)
            if idx_r >= 0:
                # 키워드 자체의 '100' 등 숫자를 피해 키워드 이후부터 탐색
                # 첫 줄만 사용 (50자면 충분, 연결 섹션 값 혼입 방지)
                first_line = full[idx_r + len(kw_r): idx_r + len(kw_r) + 60].split("\n")[0]
                nums = [float(m) for m in re.findall(r'\b(\d{2,5}(?:\.\d{1,2})?)\b', first_line)
                        if float(m) >= 50]
                if len(nums) >= 2:
                    result["순자본비율"] = nums[1]  # [전기말, 당기말, ...] 중 당기말
                elif nums:
                    result["순자본비율"] = nums[0]

        # 레버리지비율 — "레버리지비율(=(1/2)×100)" 앞 문맥에서 추출
        if result["레버리지비율"] is None:
            idx_l = full.find("레버리지비율(=(1/2)×100)")
            if idx_l >= 0:
                ctx = full[max(0, idx_l - 400): idx_l + 50]
                # 2~4자리 독립 정수 (레버리지비율 범위: 50~3000%)
                cands = [int(m) for m in re.findall(r'(?<!\d)(\d{2,4})(?!\d|,)', ctx)
                         if 50 <= int(m) <= 3000]
                if len(cands) >= 3:
                    result["레버리지비율"] = float(cands[-2])  # [전기, 당기, 증감] 중 당기
                elif len(cands) == 2:
                    result["레버리지비율"] = float(cands[-1])
                elif cands:
                    result["레버리지비율"] = float(cands[0])

        # 3개월유동성비율 우선, fallback 유동성비율
        if result["유동성비율"] is None:
            idx_u = full.find("3개월유동성비율")
            if idx_u < 0:
                idx_u = full.find("유동성비율")
            if idx_u >= 0:
                snippet = full[idx_u: idx_u + 200]
                nums = [float(m) for m in re.findall(r'\b(\d{2,5}(?:\.\d{1,2})?)\b', snippet)
                        if float(m) >= 50]
                if len(nums) >= 2:
                    result["유동성비율"] = nums[1]
                elif nums:
                    result["유동성비율"] = nums[0]

    except Exception as e:
        logger.warning(f"PDF 파싱 오류: {e}")

    return result


def _fetch_pdf_ds(period: dict) -> bytes | None:
    """DS투자증권 홈페이지 영업보고서 PDF 다운로드"""
    qtr  = REPRT_QTR_KO.get(period["reprt_code"], "")
    year = period["bsns_year"]
    kw   = f"{year}년 {qtr}"
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        LIST = "https://www.ds-sec.co.kr/bbs/board.php?bo_table=sub05_02"
        soup = BeautifulSoup(sess.get(LIST, timeout=20).content
                             .decode("utf-8", errors="replace"), "html.parser")
        wr_id = None
        for a in soup.find_all("a", href=True):
            if kw in a.get_text():
                m = re.search(r"wr_id=(\d+)", a["href"])
                if m:
                    wr_id = m.group(1)
                    break
        if not wr_id:
            logger.debug(f"DS증권 게시글 미발견: {kw}")
            return None
        detail = f"https://www.ds-sec.co.kr/bbs/board.php?bo_table=sub05_02&wr_id={wr_id}"
        sess.get(detail, timeout=20)
        down = f"https://www.ds-sec.co.kr/bbs/download.php?bo_table=sub05_02&wr_id={wr_id}&no=0"
        r = sess.get(down, headers={"Referer": detail}, timeout=60)
        if len(r.content) > 10000:
            return r.content
    except Exception as e:
        logger.warning(f"DS증권 PDF: {e}")
    return None


def _fetch_pdf_kakao(period: dict) -> bytes | None:
    """카카오페이증권 홈페이지 영업보고서 PDF 다운로드"""
    fy_kw = f"FY{period['bsns_year']} {REPRT_QTR_FY.get(period['reprt_code'], '')}"
    sess  = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        LIST = "https://www.kakaopaysec.com/management/routine/dynamicPage.do"
        soup = BeautifulSoup(sess.get(LIST, timeout=20).content
                             .decode("utf-8", errors="replace"), "html.parser")
        detail_id = None
        for a in soup.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if fy_kw in txt or fy_kw in a.get("title", ""):
                m = re.search(r"id=(\d+)", a["href"])
                if m:
                    detail_id = m.group(1)
                    break
        if not detail_id:
            logger.debug(f"카카오페이증권 게시글 미발견: {fy_kw}")
            return None
        detail = f"https://www.kakaopaysec.com/routine/sales/dynamicBoardPageDetail.do?id={detail_id}"
        soup2  = BeautifulSoup(sess.get(detail, timeout=20).content
                               .decode("utf-8", errors="replace"), "html.parser")
        file_id = None
        for a in soup2.find_all("a", href=True):
            m = re.search(r"downloadFile\.do\?id=(\d+)", a["href"])
            if m:
                file_id = m.group(1)
                break
        if not file_id:
            return None
        r = sess.get(f"https://www.kakaopaysec.com/downloadFile.do?id={file_id}",
                     headers={"Referer": detail}, timeout=60)
        if len(r.content) > 10000:
            return r.content
    except Exception as e:
        logger.warning(f"카카오페이증권 PDF: {e}")
    return None


# 홈페이지 PDF 스크래퍼 매핑
_HP_SCRAPERS: dict[str, object] = {
    "디에스증권":    _fetch_pdf_ds,
    "카카오페이증권": _fetch_pdf_kakao,
}


# ════════════════════════════════════════════════════════════════════════════
# 비율 파싱 (순자본비율 / 레버리지비율 / 유동성비율)
# ════════════════════════════════════════════════════════════════════════════

def _extract_ratio(text: str, keyword: str) -> float | None:
    """텍스트에서 키워드 인근 첫 번째 합리적인 숫자 추출"""
    idx = text.find(keyword)
    if idx == -1:
        return None
    snippet = text[idx: idx + 300]
    for m in re.finditer(r'(\d{1,5}(?:\.\d{1,2})?)', snippet):
        val = float(m.group(1))
        if 10 <= val <= 50000:   # 합리적인 비율 범위 (%)
            return val
    return None


def parse_ratios_from_html(html_bytes: bytes) -> dict:
    """단일 HTML 바이트에서 3개 비율 파싱"""
    result: dict[str, float | None] = {
        "순자본비율": None, "레버리지비율": None, "유동성비율": None}

    try:
        try:
            text = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = html_bytes.decode("cp949", errors="replace")

        soup = BeautifulSoup(text, "html.parser")

        # 테이블 셀 기반 파싱 (정확도 우선)
        for row in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td","th"])]
            if not cells:
                continue
            head = cells[0]
            vals = cells[1:]

            def first_num(lst):
                for c in lst:
                    v = _extract_ratio(c, "")
                    if v:
                        return v
                return None

            if ("순자본비율" in head or "영업용순자본비율" in head) and result["순자본비율"] is None:
                result["순자본비율"] = first_num(vals) or _extract_ratio(head, "비율")
            if "레버리지비율" in head and result["레버리지비율"] is None:
                result["레버리지비율"] = first_num(vals)
            # 3개월유동성비율 우선, 없으면 유동성비율
            if "3개월유동성비율" in head and result["유동성비율"] is None:
                result["유동성비율"] = first_num(vals)
            elif "유동성비율" in head and "3개월" not in head and result["유동성비율"] is None:
                result["유동성비율"] = first_num(vals)

        # fallback: 전체 텍스트 검색
        plain = soup.get_text(" ", strip=True)
        if result["순자본비율"] is None:
            result["순자본비율"] = (
                _extract_ratio(plain, "순자본비율") or
                _extract_ratio(plain, "영업용순자본비율")
            )
        if result["레버리지비율"] is None:
            result["레버리지비율"] = _extract_ratio(plain, "레버리지비율")
        if result["유동성비율"] is None:
            result["유동성비율"] = (
                _extract_ratio(plain, "3개월유동성비율") or
                _extract_ratio(plain, "유동성비율")
            )

    except Exception as e:
        logger.debug(f"HTML 파싱 오류: {e}")

    return result


def get_ratios(dart_key: str, corp_code: str,
               bsns_year: str, reprt_code: str,
               bgn_de: str, end_de: str) -> dict:
    """DART 공시 원문 ZIP 다운로드 후 비율 파싱"""
    empty = {"순자본비율": None, "레버리지비율": None, "유동성비율": None}

    # 1) 공시 목록에서 rcept_no 획득
    reprt_nm = {"11011":"사업보고서","11012":"반기보고서",
                "11013":"분기보고서","11014":"분기보고서"}.get(reprt_code,"")
    list_d = dart_get(dart_key, "list.json",
                      corp_code=corp_code, bgn_de=bgn_de, end_de=end_de,
                      pblntf_ty="A", page_count=10)
    rcept_no = None
    for rpt in list_d.get("list", []):
        if reprt_nm in rpt.get("report_nm",""):
            rcept_no = rpt.get("rcept_no")
            break

    if not rcept_no:
        logger.debug("  비율 rcept_no 없음")
        return empty

    # 2) 문서 ZIP 다운로드
    try:
        r = requests.get(f"{DART_BASE}/document.xml",
                         params={"crtfc_key": dart_key, "rcept_no": rcept_no},
                         headers=HEADERS, timeout=60, stream=True)
        content = r.content
        if not content:
            return empty

        zf = ZipFile(BytesIO(content))

        # 3) "순자본비율" 포함 파일만 파싱
        for name in zf.namelist():
            if not (name.endswith(".htm") or name.endswith(".html")):
                continue
            raw = zf.read(name)
            # UTF-8 또는 CP949로 디코딩 후 키워드 확인
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    txt = raw.decode("cp949", errors="replace")
                except Exception:
                    continue
            if "순자본비율" in txt or "영업용순자본비율" in txt or "3개월유동성비율" in txt:
                ratios = parse_ratios_from_html(raw)
                if any(v is not None for v in ratios.values()):
                    return ratios

        return empty

    except BadZipFile:
        logger.debug("  ZIP 파일 오류")
        return empty
    except Exception as e:
        logger.warning(f"  비율 수집 오류: {e}")
        return empty


# ════════════════════════════════════════════════════════════════════════════
# 전체 수집
# ════════════════════════════════════════════════════════════════════════════

def collect_all(cfg: dict, period: dict) -> list[dict]:
    dart_key = cfg.get("dart_api_key", "").strip()
    if not dart_key:
        raise ValueError(
            "config.json에 'dart_api_key'가 없습니다.\n"
            "https://opendart.fss.or.kr/api/ 에서 무료 발급 후 입력하세요."
        )

    results = []
    total = len(COMPANY_MAP)
    for idx, (display, candidates) in enumerate(COMPANY_MAP.items(), 1):
        logger.info(f"[{idx}/{total}] {display} 수집 중...")
        row: dict = {"name": display, "error": None,
                     "자본금": None, "자기자본": None, "영업이익": None, "당기순익": None,
                     "순자본비율": None, "레버리지비율": None, "유동성비율": None}

        corp_code = find_corp_code(dart_key, display, candidates)

        # ── DART 재무제표 (개별기준)
        if corp_code:
            fin = get_financials(dart_key, corp_code,
                                 period["bsns_year"], period["reprt_code"])
            row.update(fin)
            time.sleep(0.4)

            # DART 비율 (공시 원문 파싱)
            if any(v is not None for v in fin.values()):
                ratios = get_ratios(dart_key, corp_code,
                                    period["bsns_year"], period["reprt_code"],
                                    period["bgn_de"], period["end_de"])
                row.update(ratios)
                time.sleep(0.4)

        # ── 홈페이지 영업보고서 PDF (DART 미공시사 또는 비율 보완)
        if display in _HP_SCRAPERS:
            dart_has_fin = any(row.get(k) is not None for k in ("자기자본", "영업이익"))
            dart_has_ratio = any(row.get(k) is not None for k in ("순자본비율", "레버리지비율"))

            if not dart_has_fin or not dart_has_ratio:
                logger.info(f"  → 홈페이지 PDF 시도...")
                pdf_bytes = _HP_SCRAPERS[display](period)
                if pdf_bytes:
                    hp = _parse_yb_pdf(pdf_bytes)
                    for k, v in hp.items():
                        if v is not None and row.get(k) is None:
                            row[k] = v
                    logger.info(f"  → PDF 파싱 완료")
                else:
                    if not dart_has_fin:
                        row["error"] = "공시 미확인(홈페이지 조회 실패)"

        if not corp_code and display not in _HP_SCRAPERS:
            row["error"] = "DART 검색 실패"

        # ── FISIS API로 비율 보완 (순자본비율·3개월유동성비율)
        fisis_key = cfg.get("fisis_api_key", "").strip()
        if fisis_key:
            finance_cd = FISIS_CODE_MAP.get(display)
            if finance_cd and (row.get("순자본비율") is None or row.get("유동성비율") is None):
                base_month = _period_to_basemm(period)
                fisis, used_mm = fetch_fisis_ratios(fisis_key, finance_cd, base_month)
                updated = []
                for k, v in fisis.items():
                    if v is not None and row.get(k) is None:
                        row[k] = v
                        updated.append(f"{k}={v}")
                if updated:
                    period_tag = f"({used_mm[:4]}/{used_mm[4:]})" if used_mm else ""
                    logger.info(f"  → FISIS{period_tag} 보완: {', '.join(updated)}")
                time.sleep(0.3)

        logger.info(f"  → 자기자본={_fmt_amt(row.get('자기자본'))}억  "
                    f"영업이익={_fmt_amt(row.get('영업이익'))}억  "
                    f"순자본비율={_fmt_pct(row.get('순자본비율'))}")
        results.append(row)

    # 자기자본 내림차순 정렬 (없으면 맨 뒤)
    results.sort(key=lambda r: r.get("자기자본") or -1, reverse=True)
    return results


# ════════════════════════════════════════════════════════════════════════════
# 포맷 헬퍼
# ════════════════════════════════════════════════════════════════════════════

def _fmt_amt(val: int | None) -> str:
    if val is None:
        return "-"
    return f"{round(val / 1e8):,}"   # 억원


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:.1f}%"


def _color_profit(val: int | None) -> str:
    if val is None:
        return "#333"
    return "#c0392b" if val < 0 else "#154360"


# ════════════════════════════════════════════════════════════════════════════
# 이메일 HTML 빌드
# ════════════════════════════════════════════════════════════════════════════

COLS = ["자본금", "자기자본", "영업이익", "당기순익",
        "순자본비율", "레버리지비율", "유동성비율"]
COL_LABELS = ["자본금", "자기자본", "영업이익", "당기순익",
              "순자본비율", "레버리지비율", "3개월유동성비율"]
UNITS = ["억원", "억원", "억원", "억원", "%", "%", "%"]
AMT_COLS  = {"자본금","자기자본","영업이익","당기순익"}
PCT_COLS  = {"순자본비율","레버리지비율","유동성비율"}


def _th(text, extra=""):
    return f"<th style='padding:9px 12px;text-align:right;white-space:nowrap;{extra}'>{text}</th>"

def _td(text, extra=""):
    return f"<td style='padding:8px 11px;text-align:right;{extra}'>{text}</td>"


def build_email(results: list[dict], period: dict) -> tuple[str, str]:
    now_str = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")

    # ── HTML 헤더 행
    th_cells = (_th("증권사", "text-align:left;") +
                "".join(_th(c) for c in COL_LABELS))
    unit_cells = ("<td style='padding:3px 11px;text-align:left;color:#aaa;font-size:10px;'>단위</td>" +
                  "".join(f"<td style='padding:3px 11px;text-align:right;color:#aaa;font-size:10px;'>{u}</td>"
                          for u in UNITS))

    # ── 데이터 행
    rows_html = ""
    for i, row in enumerate(results):
        bg  = "#fff" if i % 2 == 0 else "#f5f7fb"
        err = (f" <span style='color:#e74c3c;font-size:10px;font-weight:400;'>({row['error']})</span>"
               if row.get("error") else "")

        cells = ""
        for col in COLS:
            val = row.get(col)
            if col in AMT_COLS:
                txt   = _fmt_amt(val)
                color = _color_profit(val) if col in ("영업이익","당기순익") else "#333"
            else:
                txt   = _fmt_pct(val)
                color = "#333"
            cells += _td(txt, f"color:{color};")

        rows_html += (f"<tr style='background:{bg};border-bottom:1px solid #eee;'>"
                      f"<td style='padding:8px 11px;font-weight:600;white-space:nowrap;'>"
                      f"{row['name']}{err}</td>{cells}</tr>")

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'></head>
<body style='font-family:"Malgun Gothic",Arial,sans-serif;max-width:960px;
             margin:auto;padding:16px;background:#f0f2f5;'>
  <div style='background:#1a2740;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0;'>
    <h1 style='margin:0;font-size:19px;letter-spacing:-0.5px;'>증권사 실적현황</h1>
    <p style='margin:5px 0 0;color:#8fa3bf;font-size:12px;'>
      {period["period"]} 기준 (개별재무제표) &nbsp;|&nbsp; 작성: {now_str}
    </p>
  </div>
  <div style='overflow-x:auto;background:#fff;border:1px solid #dde;
              border-top:none;border-radius:0 0 8px 8px;'>
    <table style='width:100%;border-collapse:collapse;font-size:13px;min-width:780px;'>
      <thead>
        <tr style='background:#1a2740;color:#fff;'>
          <th style='padding:9px 12px;text-align:left;white-space:nowrap;'>증권사</th>
          {''.join(_th(c) for c in COL_LABELS)}
        </tr>
        <tr style='background:#e8ecf2;'>
          {unit_cells}
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <p style='text-align:center;color:#bbb;font-size:11px;margin-top:10px;line-height:1.8;'>
    ※ 자본금·자기자본·영업이익·당기순익: 억원(원 단위에서 변환) &nbsp;|&nbsp; 비율: % 단위<br>
    ※ '-': 공시 미확인 또는 DART 조회 불가 &mdash; 원본 공시 직접 확인 필요<br>
    ※ 순자본비율·3개월유동성비율은 FISIS Open API(금감원) 또는 DART 공시문서 파싱 결과이며 오차 가능<br>
    ※ 개별재무제표 기준. DART 미공시사(토스증권·케이프증권 등)는 홈페이지 영업보고서 참조<br>
    ※ 출처: 금융감독원 DART (opendart.fss.or.kr) · 금감원 FISIS (fisis.fss.or.kr) · 각 증권사 홈페이지
  </p>
</body></html>"""

    # ── Plain text
    plain = f"■ 증권사 실적현황 ({period['period']} 기준)  작성: {now_str}\n\n"
    plain += f"{'증권사':<12} {'자본금':>8} {'자기자본':>10} {'영업이익':>9} {'당기순익':>9}"
    plain += f" {'순자본비율':>9} {'레버리지':>8} {'유동성':>7}\n"
    plain += "-" * 80 + "\n"
    for row in results:
        plain += (f"{row['name']:<12} "
                  f"{_fmt_amt(row.get('자본금')):>8} "
                  f"{_fmt_amt(row.get('자기자본')):>10} "
                  f"{_fmt_amt(row.get('영업이익')):>9} "
                  f"{_fmt_amt(row.get('당기순익')):>9} "
                  f"{_fmt_pct(row.get('순자본비율')):>9} "
                  f"{_fmt_pct(row.get('레버리지비율')):>8} "
                  f"{_fmt_pct(row.get('유동성비율')):>7}\n")
    plain += "\n단위: 자본금~순익=억원, 비율=%, '-'=미확인\n출처: DART (opendart.fss.or.kr)"

    return plain, html


# ════════════════════════════════════════════════════════════════════════════
# 발송 & 스케줄러
# ════════════════════════════════════════════════════════════════════════════

def send_report(cfg: dict, period: dict):
    ec = cfg["email"]
    logger.info(f"=== 증권사 실적 수집 시작 ({period['period']}) ===")
    results = collect_all(cfg, period)
    plain, html = build_email(results, period)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[증권사실적] {period['period']} 기준 실적현황"
    msg["From"]    = ec["sender"]
    msg["To"]      = ", ".join(ec["recipients"])
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"])) as s:
        s.ehlo(); s.starttls()
        s.login(ec["sender"], ec["password"])
        s.send_message(msg)
    logger.info("=== 증권사 실적 이메일 발송 완료 ===")


def is_send_day() -> bool:
    now   = datetime.now()
    month = now.month
    if month not in (3, 5, 8, 11):
        return False
    last_day = monthrange(now.year, month)[1]
    return now.day == last_day


def run_scheduled():
    if not is_send_day():
        return
    cfg = load_config()
    period = get_period_info()
    try:
        send_report(cfg, period)
    except Exception as e:
        logger.error(f"발송 실패: {e}", exc_info=True)


def main():
    logger.info("증권사 실적 스케줄러 시작 (3·5·8·11월 말일 08:30 자동 발송)")

    if "--now" in sys.argv:
        override_month = None
        if "--month" in sys.argv:
            idx = sys.argv.index("--month")
            if idx + 1 < len(sys.argv):
                override_month = int(sys.argv[idx + 1])
        cfg    = load_config()
        period = get_period_info(override_month)
        logger.info(f"기준: {period['period']}")
        send_report(cfg, period)
        return

    schedule.every().day.at("08:30").do(run_scheduled)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
