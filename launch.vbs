' Launch Quietype with no console window using pythonw.exe
Dim shell, dir, cmd
Set shell = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
cmd = Chr(34) & dir & ".venv\Scripts\pythonw.exe" & Chr(34) & _
      " " & Chr(34) & dir & "main.py" & Chr(34)
shell.Run cmd, 0, False
