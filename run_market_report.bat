@echo off
chcp 65001 >nul
title 금융시장동향 리포트

cd /d "%~dp0"

echo =====================================================
echo  금융시장동향 Daily Report (매 영업일 16:00 자동 발송)
echo =====================================================

python market_report.py
pause
