@echo off
rem ── Painel de Frete — MODO REDE (acesso de outros PCs do mesmo escritorio) ───
rem Sobe o servidor ABERTO para a rede local (--host 0.0.0.0) e mostra o endereco
rem que os colegas devem digitar no navegador. Este PC precisa ficar LIGADO e a
rem janela do servidor ABERTA enquanto as pessoas acessam.
cd /d "%~dp0"

echo Liberando a porta 8000 (encerra o servidor antigo, se houver)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

echo Descobrindo o IP deste PC na rede (adaptador da rota padrao)...
set "IP="
rem Pega o IP do adaptador que realmente sai para a rede/internet (rota padrao),
rem ignorando adaptadores virtuais (VirtualBox/WSL) e sem-rede (169.254.x).
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$i=(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue ^| Sort-Object RouteMetric ^| Select-Object -First 1).InterfaceIndex; (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $i -ErrorAction SilentlyContinue ^| Where-Object { $_.IPAddress -ne '127.0.0.1' } ^| Select-Object -First 1 -ExpandProperty IPAddress)"`) do set "IP=%%i"
if "%IP%"=="" set "IP=SEU-IP-AQUI"

echo.
echo ============================================================
echo   PAINEL EM MODO REDE
echo.
echo   Os colegas devem abrir no navegador do PC deles:
echo.
echo        http://%IP%:8000
echo.
echo   (deixe este PC e a janela do servidor LIGADOS)
echo ============================================================
echo.

echo Iniciando o servidor...
start "Freight Gateway (REDE) - NAO FECHE esta janela" cmd /k python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

rem Aguarda o servidor responder (ate ~20s) antes de abrir o navegador local
for /l %%i in (1,1,20) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto abrir
  timeout /t 1 /nobreak >nul
)
echo AVISO: o servidor demorou para responder. Verifique a janela do servidor.

:abrir
start "" http://localhost:8000/
echo.
echo Pronto. Se um colega nao conseguir abrir, veja o passo do FIREWALL nas instrucoes.
pause
