@echo off
rem ── Desligar o servidor LogHub que roda em segundo plano ────────────────────
echo Encerrando o servidor LogHub (porta 8000)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo.
echo Servidor parado. (Para ligar de novo, use o INICIAR-LOGHUB.vbs)
timeout /t 3 >nul
