@echo off
chcp 65001 >nul
title 리스크 모니터링 대시보드

cd /d "%~dp0"

pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo Flask 설치 중...
    pip install flask
)

echo 대시보드 시작: http://localhost:5000
start http://localhost:5000
python dashboard.py
pause
