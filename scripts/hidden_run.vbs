' Hidden window launcher.
' Usage: wscript //nologo "D:\deepsleep\hidden_run.vbs" "<program and args>"
' Runs the given command line with its console window hidden, without waiting.
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count <> 1 Then
  WScript.Quit 2
End If
sh.Run WScript.Arguments(0), 0, False
