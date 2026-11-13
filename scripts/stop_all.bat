@echo off
chcp 65001 >nul
title 停止 MaiBot + SnowLuma
echo ============================================================
echo   停止全部服务
echo ============================================================
echo.

echo [1/3] 停止 MaiBot...
taskkill /F /FI "WINDOWTITLE eq MaiBot*" >nul 2>&1
powershell -NoProfile -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*deepsleep*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
powershell -NoProfile -Command "Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*deepsleep*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
echo    完成

echo [2/3] 停止 SnowLuma...
taskkill /F /FI "WINDOWTITLE eq SnowLuma*" >nul 2>&1
powershell -NoProfile -Command "Get-Process node -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*SnowLuma*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
echo    完成

echo [3/3] 检查端口...
powershell -NoProfile -Command "foreach($p in 3001,5099,8001,8100){ $c = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {\$_.LocalPort -eq \$p}; if($c){ Write-Host \"    端口 \$p 仍被占用\" } else { Write-Host \"    端口 \$p 已释放\" } }"

echo.
echo 注意：QQ 客户端的注入状态已随 SnowLuma 停止而解除。
echo      下次使用需重新在 SnowLuma 面板点「加载」注入。
echo.
pause
