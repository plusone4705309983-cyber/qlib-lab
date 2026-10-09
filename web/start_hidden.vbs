' Hide-window launcher for the Qlib web app (called by scheduled task "Qlib_App" at logon)
' NOTE: update the path below if the repo is ever moved
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Work_Data\Source_Code_New\qlib-lab\web\run.bat""", 0, False