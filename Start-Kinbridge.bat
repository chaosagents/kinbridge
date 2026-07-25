@echo off
cd /d "%~dp0"
python kinbridge.py
if errorlevel 1 (
    echo.
    echo Something went wrong. If "python" wasn't found, install Python 3.8+
    echo from https://www.python.org/downloads/ and make sure "Add python.exe
    echo to PATH" is checked during install, then double-click this file again.
    pause
)
