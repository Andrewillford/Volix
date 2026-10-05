@echo off
rem ── Liberar a porta 8000 no Firewall do Windows ─────────────────────────────
rem Permite que OUTROS PCs da mesma rede acessem o LogHub por http://IP:8000
rem EXECUTE COMO ADMINISTRADOR: clique com o botao direito neste arquivo e
rem escolha "Executar como administrador". Roda uma vez so.
echo Liberando a porta 8000 (entrada, TCP) no Firewall do Windows...
netsh advfirewall firewall delete rule name="LogHub 8000" >nul 2>&1
netsh advfirewall firewall add rule name="LogHub 8000" dir=in action=allow protocol=TCP localport=8000
echo.
if %errorlevel%==0 (
  echo OK! Porta 8000 liberada.
  echo Agora, no outro PC, abra no navegador:  http://%COMPUTERNAME%  ou o IP mostrado no ABRIR-PAINEL-REDE.bat
) else (
  echo FALHOU. Voce executou como ADMINISTRADOR? Clique direito ^> Executar como administrador.
)
echo.
pause
