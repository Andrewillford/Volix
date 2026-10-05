@echo off
setlocal
rem =====================================================================
rem  Cria o ambiente de HOMOLOGACAO copiando a pasta de PRODUCAO para
rem  C:\LogHub-HOMOLOG (FORA do OneDrive, para nao sincronizar/travar).
rem  Este .bat fica em <producao>\homologacao\ ; a producao e a pasta PAI.
rem  Ideal: pare a producao antes (PARAR-LOGHUB.bat) para os bancos
rem  copiarem consistentes.
rem =====================================================================

rem  Resolve a raiz da producao (pasta pai deste script)
pushd "%~dp0.."
set ORIGEM=%CD%
popd
set DESTINO=C:\LogHub-HOMOLOG

echo.
echo   Origem  (producao): %ORIGEM%
echo   Destino (homolog):  %DESTINO%
echo.
echo   Copiando arquivos... (pode levar alguns minutos)
rem  /E = subpastas | /XD = pastas ignoradas | /XF = arquivos ignorados
rem  .env NAO e copiado (homolog usa o seu proprio, com Olist desligado).
robocopy "%ORIGEM%" "%DESTINO%" /E /R:2 /W:2 /XD backups __pycache__ /XF .env *.pyc >nul

if not exist "%DESTINO%\.env" (
  copy "%DESTINO%\homologacao\.env.homolog.exemplo" "%DESTINO%\.env" >nul
  echo   Criado %DESTINO%\.env  ^(Olist desligado^)
)

echo.
echo   ================= PROXIMOS PASSOS =================
echo   1^) Abra o Prompt/Git Bash em: %DESTINO%
echo   2^) git checkout -b homologacao       ^(cria a branch de teste^)
echo   3^) De dois cliques em: %DESTINO%\homologacao\INICIAR-HOMOLOG.vbs
echo   4^) Acesse:  http://localhost:8001
echo   ==================================================
echo.
pause
endlocal
