@echo off
REM Starts the watcher. It sleeps outside market hours, so it is safe to leave
REM running or to launch at logon from Task Scheduler.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -m alerts.worker %*
