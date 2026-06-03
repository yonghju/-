@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 증권사 실적현황 스케줄러 시작 (3,5,8,11월 말일 08:30 자동 발송)
echo.
py securities_report.py
pause
