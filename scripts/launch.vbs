' launch.vbs - silently start backend + frontend with no console windows
Option Explicit
Dim sh, fso, root, beDir, feDir, py, beLog, feLog, cmd

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root    = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
beDir   = root & "\backend"
feDir   = root & "\frontend"
py      = root & "\.venv\Scripts\python.exe"
beLog   = beDir & "\logs\backend.log"
feLog   = feDir & "\logs\frontend.log"

If Not fso.FolderExists(beDir & "\logs") Then fso.CreateFolder(beDir & "\logs")
If Not fso.FolderExists(feDir & "\logs") Then fso.CreateFolder(feDir & "\logs")

' Backend: python.exe in a hidden window (mode 0), output redirected to log
cmd = "cmd /c cd /d """ & beDir & """ && """ & py & """ -m app.main > """ & beLog & """ 2>&1"
sh.Run cmd, 0, False

WScript.Sleep 3000

' Frontend: npm run dev in hidden window, output redirected to log
cmd = "cmd /c cd /d """ & feDir & """ && npm run dev > """ & feLog & """ 2>&1"
sh.Run cmd, 0, False