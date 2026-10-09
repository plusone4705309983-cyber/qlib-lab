@echo off
REM 启动 Qlib 数据检视器 (waitress, 绑 127.0.0.1:8853)
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
"%PY%" "%~dp0app.py" %*
