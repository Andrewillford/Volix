@echo off
rem ── Painel de Frete (Auditoria + Torre de Controle + Scorecard) ────────────
rem Sempre reinicia o servidor para garantir que a versao mais recente do
rem codigo seja carregada, e entao abre o painel no navegador.
cd /d "%~dp0"

echo Liberando a porta 8000 (encerra o servidor antigo, se houver)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

echo Iniciando o servidor do painel de frete...
start "Freight Gateway - nao feche esta janela" cmd /k python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

rem Aguarda o servidor responder (ate ~20s)
for /l %%i in (1,1,20) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto abrir
  timeout /t 1 /nobreak >nul
)
echo AVISO: o servidor demorou para responder. Verifique a janela "Freight Gateway".

:abrir
start "" http://localhost:8000/audit
