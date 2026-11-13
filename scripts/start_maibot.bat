@echo off
chcp 65001 >nul
title MaiBot 启动器
cd /d "%~dp0MaiBot"

echo ============================================================
echo   MaiBot v1.3.15 启动器
echo ============================================================
echo.
echo   请先确认：
echo     1. QQ 小号 100000001 已登录 QQ 客户端
echo     2. SnowLuma 已启动并完成注入（看到 3001 端口监听）
echo   否则本程序会不断重连，属正常现象。
echo.
echo   关闭此窗口 = 停止 MaiBot
echo ============================================================
echo.

REM EULA / 隐私协议哈希（对应 MaiBot v1.3.15，升级后需按启动提示替换）
set EULA_AGREE=8e6e7d647f7f82d6ea98456b73908656
set PRIVACY_AGREE=91e5db7659c560bc3545e63859b6ebc0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo 正在启动 MaiBot...
echo.
echo   WebUI 地址: http://127.0.0.1:8001
echo   访问令牌:   见 data\webui.json 中的 access_token
echo.
echo ------------------------------------------------------------
echo.

REM 记忆系统的本地 embedding 服务（8100），没起就自动拉起
powershell -NoProfile -Command "$c = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {$_.LocalPort -eq 8100}; if(-not $c){ Start-Process -FilePath 'D:\deepsleep\embedding_server\启动embedding服务.bat' -WorkingDirectory 'D:\deepsleep\embedding_server' }"

".venv\Scripts\python.exe" bot.py

echo.
echo ------------------------------------------------------------
echo MaiBot 已退出。
pause
