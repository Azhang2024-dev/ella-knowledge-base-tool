' 启动.vbs — 双击启动网页爬虫工具（无控制台窗口）
' 自动检测 .venv 虚拟环境，也可改为绝对路径

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)

If fso.FileExists(base & "\.venv\Scripts\pythonw.exe") Then
    py = base & "\.venv\Scripts\pythonw.exe"
ElseIf fso.FileExists(base & "\venv\Scripts\pythonw.exe") Then
    py = base & "\venv\Scripts\pythonw.exe"
Else
    py = "pythonw"
End If

sh.Run Chr(34) & py & Chr(34) & " " & Chr(34) & base & "\main.py" & Chr(34), 0, False
