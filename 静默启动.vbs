' 静默启动项目管家（不弹黑窗口）
' 双击运行即可；关闭浏览器窗口后，服务仍会在后台待着，
' 下次双击会直接复用已启动的服务再开一个窗口。
Option Explicit

Dim fso, sh, base, cmd, rc

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

' 优先用 pyw（Python Launcher 的无窗口版），没有就退回 pythonw
If fso.FileExists(sh.ExpandEnvironmentStrings("%SystemRoot%") & "\pyw.exe") Then
  cmd = "pyw """ & base & "\server.py"""
Else
  cmd = "pythonw """ & base & "\server.py"""
End If

rc = sh.Run(cmd, 0, False)
