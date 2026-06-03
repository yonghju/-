"""GitHub Actions에서 환경변수(Secrets)로 config.json 생성"""
import json, os

config = {
    "check_interval_minutes": 30,
    "email": {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "sender": os.environ.get("EMAIL_SENDER", ""),
        "password": os.environ.get("EMAIL_PASSWORD", ""),
        "recipients": [
            r.strip()
            for r in os.environ.get("EMAIL_RECIPIENTS", "").split(",")
            if r.strip()
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
