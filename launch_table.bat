@echo off
rem launch_table.bat - start the PySide6 workbench without a lingering console.
rem
rem Why this file looks like this:
rem   * pythonw.exe = windowless python, so the app itself has no console.
rem   * "start "" ..." detaches it: this cmd window closes immediately instead of
rem     staying on screen for as long as the GUI runs (that black box).
rem   * ASCII only on purpose: cmd reads .bat in the OEM/ANSI codepage, so UTF-8
rem     comments show up as garbage (and can confuse parsing on some systems).
rem   * Modules live in layer folders now, so the entry is ui\table__ui.py.
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    echo [launch_table] not found: %PYW%
    echo Create the virtualenv first:  python -m venv .venv
    pause
    exit /b 1
)

set "APP=%~dp0ui\table__ui.py"
if not exist "%APP%" (
    echo [launch_table] not found: %APP%
    pause
    exit /b 1
)

start "" "%PYW%" "%APP%"
