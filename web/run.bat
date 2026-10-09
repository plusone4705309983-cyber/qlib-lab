@echo off
REM 启动 Qlib 数据检视器 (waitress, 绑 127.0.0.1:8853)
REM 调用方式: web\run.bat  (手动启动, 也会被计划任务开机调用)
setlocal
set "ROOT=%~dp0.."
if not exist "%ROOT%\web\logs" mkdir "%ROOT%\web\logs"
cd /d "%ROOT%"
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
"%PY%" -u "%~dp0app.py" %* >> "%ROOT%\web\logs\app.log" 2>&1