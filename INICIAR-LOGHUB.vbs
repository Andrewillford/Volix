' ============================================================================
'  LogHub - iniciar o servidor em SEGUNDO PLANO (sem abrir janela do CMD)
'  De dois cliques neste arquivo. O servidor sobe invisivel e fica disponivel
'  para os outros PCs em http://ANDRE:8000 (ou http://IP:8000).
'  Para DESLIGAR, use o PARAR-LOGHUB.bat.
' ============================================================================
Option Explicit
Dim sh, fso, pasta, py
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Roda a partir da pasta deste arquivo (a pasta do projeto)
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = pasta

' python.exe com a JANELA ESCONDIDA (o console existe mas fica invisivel; o
' servidor loga normalmente). Usa o caminho conhecido, senao o do PATH.
py = "C:\Users\andre\AppData\Local\Programs\Python\Python314\python.exe"
If Not fso.FileExists(py) Then py = "python"

' 1) Libera a porta 8000 (encerra um servidor antigo, se houver) - oculto, aguarda
sh.Run "powershell -NoProfile -WindowStyle Hidden -Command ""Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }""", 0, True

' 2) Sobe o servidor OCULTO (janela 0 = escondida; nao espera)
sh.Run """" & py & """ -m uvicorn app.main:app --host 0.0.0.0 --port 8000", 0, False

' Aviso rapido de que ligou (some sozinho em 3s)
sh.Popup "LogHub iniciado em segundo plano." & vbCrLf & vbCrLf & "Acesse pelo app instalado, ou por http://ANDRE:8000", 3, "LogHub", 64
