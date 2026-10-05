@echo off
setlocal
rem =====================================================================
rem  Atualiza os DADOS de homologacao com uma copia dos bancos de PRODUCAO.
rem  Este .bat fica em <producao>\homologacao\ ; a producao e a pasta PAI.
rem  IMPORTANTE: pare o homolog antes (PARAR-HOMOLOG.bat) para os bancos
rem  nao ficarem travados.
rem =====================================================================

pushd "%~dp0..\tables"
set ORIGEM=%CD%
popd
set DESTINO=C:\LogHub-HOMOLOG\tables

echo.
echo   Copiando bancos de:
echo     %ORIGEM%
echo   para:
echo     %DESTINO%
echo.
robocopy "%ORIGEM%" "%DESTINO%" *.db *.json /R:2 /W:2 >nul
echo   Dados de homologacao atualizados.
echo   Reinicie o INICIAR-HOMOLOG.vbs para recarregar.
echo.
timeout /t 4 >nul
endlocal
