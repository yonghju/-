@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 금융유관기관 보도자료 스케줄러 시작 (매일 07:30 자동 발송)
echo.
py press_release_report.py
pause
