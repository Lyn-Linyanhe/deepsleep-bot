@echo off
chcp 65001 >nul
set EULA_AGREE=8e6e7d647f7f82d6ea98456b73908656
set PRIVACY_AGREE=91e5db7659c560bc3545e63859b6ebc0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d D:\deepsleep\MaiBot
wscript //nologo "D:\deepsleep\hidden_run.vbs" "D:\deepsleep\MaiBot\.venv\Scripts\python.exe bot.py"
