' ============================================================================
'  LogHub HOMOLOGACAO - servidor de TESTE em segundo plano (porta 8001)
'  Ambiente de teste, ISOLADO da producao (que roda na 8000).
'  Este arquivo fica em <pasta-do-loghub>\homologacao\ ; ele sobe o app a
'  partir da pasta PAI (onde esta a pasta app\).
'  Acesse em http://localhost:8001. Para DESLIGAR use o PARAR-HOMOLOG.bat.
' ============================================================================
Option Explicit
Dim sh, fso, pastaApp, py
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Sobe uma pasta (de \homologacao para a raiz do projeto, onde esta app\)
pastaApp = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = pastaApp

py = "C:\Users\andre\AppData\Local\Programs\Python\Python314\python.exe"
If Not fso.FileExists(py) Then py = "python"

' 1) Libera a porta 8001 (encerra um homolog antigo, se houver) - oculto, aguarda
sh.Run "powershell -NoProfile -WindowStyle Hidden -Command ""Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }""", 0, True

' 2) Sobe o servidor de homologacao OCULTO na porta 8001 (nao espera)
sh.Run """" & py & """ -m uvicorn app.main:app --host 0.0.0.0 --port 8001", 0, False

sh.Popup "LogHub HOMOLOGACAO iniciado (porta 8001)." & vbCrLf & vbCrLf & "Acesse por http://localhost:8001", 4, "LogHub HOMOLOGACAO", 64
