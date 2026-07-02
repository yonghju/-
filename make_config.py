"""GitHub Actions에서 환경변수(Secrets)로 config.json 생성"""
import json, os

def _s(key, default=""):
    """시크릿 값의 앞뒤 공백 및 따옴표 제거"""
    return os.environ.get(key, default).strip().strip("\"'")

config = {
    "check_interval_minutes": 30,
    "email": {
        "smtp_host": _s("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(_s("SMTP_PORT", "587") or "587"),
        "sender":    _s("EMAIL_SENDER"),
        "password":  _s("EMAIL_PASSWORD"),
        "recipients": [
            r.strip().strip("\"'")
            for r in os.environ.get("EMAIL_RECIPIENTS", "").split(",")
            if r.strip().strip("\"'")
        ],
    },
    "naver_api": {
        "client_id":     os.environ.get("NAVER_CLIENT_ID", ""),
        "client_secret": os.environ.get("NAVER_CLIENT_SECRET", ""),
    },
    "ecos_api_key":  os.environ.get("ECOS_API_KEY", ""),
    "krx_id": "",
    "krx_pw": "",
    "dart_api_key":  os.environ.get("DART_API_KEY", ""),
    "fisis_api_key": os.environ.get("FISIS_API_KEY", ""),
    "slack_webhook_url":  os.environ.get("SLACK_WEBHOOK_URL", ""),
    "teams_webhook_url":  os.environ.get("TEAMS_WEBHOOK_URL", ""),
}

with open("config.json", "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print("config.json 생성 완료")
