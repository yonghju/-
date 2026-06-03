@echo off
chcp 65001 >nul
title 증권사 리스크 모니터링

echo =====================================================
echo  증권사 손실^&운영리스크 뉴스 모니터링 시작
echo =====================================================

cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않습니다.
    pause
    exit /b 1
)

pip show feedparser >nul 2>&1
if %errorlevel% neq 0 (
    echo 패키지 설치 중...
    pip install -r requirements.txt
)

python securities_risk_monitor.py
pause
