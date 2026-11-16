@echo off
rem Run from source on Windows (needs Python 3.9+). End users: use the .exe from Releases.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 run.py) else (python run.py)
if errorlevel 1 pause
