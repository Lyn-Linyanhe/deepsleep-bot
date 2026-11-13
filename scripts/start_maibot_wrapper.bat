@echo off
chcp 65001 >nul
title MaiBot
set TRACE=D:\deepsleep\wrapper_trace.log
echo [%date% %time%] wrapper start >> "%TRACE%"
cd /d D:\deepsleep\MaiBot
echo [%date% %time%] cd done, cwd=%CD% >> "%TRACE%"
netstat -ano | findstr /C:":8100 " | findstr "LISTENING" >nul 2>&1 || Start-Process -FilePath "D:\deepsleep\embedding_server\xe5¯åŠ¨embeddingæœåŠ¡.bat" -WorkingDirectory "D:\deepsleep\embedding_server"
echo [%date% %time%] embed-check done >> "%TRACE%"
set EULA_AGREE=8e6e7d647f7f82d6ea98456b73908656
set PRIVACY_AGREE=91e5db7659c560bc3545e63859b6ebc0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo [%date% %time%] launching bot.py >> "%TRACE%"
"D:\deepsleep\MaiBot\.venv\Scripts\python.exe" bot.py
echo [%date% %time%] bot.py exited with %ERRORLEVEL% >> "%TRACE%"
