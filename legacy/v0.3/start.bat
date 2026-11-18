@echo off
rem Run the desk pet from source on Windows (needs Python 3.9+ and requirements-windows.txt).
rem End users: download the zip from Releases instead.
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (start "" pyw -3 run.py %*) else (python run.py %*)
if errorlevel 1 pause
