@echo off
rem -- Desligar o servidor de HOMOLOGACAO que roda em segundo plano (porta 8001) --
echo Encerrando o LogHub HOMOLOGACAO (porta 8001)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo.
echo Homologacao parada. (Para ligar de novo, use INICIAR-HOMOLOG.vbs)
timeout /t 3 >nul
