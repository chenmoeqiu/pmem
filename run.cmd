@echo off
setlocal
rem pmem launcher: pick a Python >= 3.11 automatically.
rem Override by setting PMEM_PYTHON to a full path of python.exe.
rem NOTE: keep this file ASCII-only. cmd.exe reads .bat as the OEM codepage,
rem so non-ASCII comments would be executed as garbage commands.

if defined PMEM_PYTHON (
    "%PMEM_PYTHON%" -m pmem %*
    exit /b %errorlevel%
)

set "CAND=C:\Program Files\DSH Desktop\resources\office-runtime\primary-runtime\dependencies\python\python.exe"
if exist "%CAND%" (
    "%CAND%" -m pmem %*
    exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.13 -m pmem %* 2>nul
    if %errorlevel%==0 exit /b 0
    py -3.12 -m pmem %* 2>nul
    if %errorlevel%==0 exit /b 0
    py -3.11 -m pmem %* 2>nul
    if %errorlevel%==0 exit /b 0
)

python -m pmem %*
exit /b %errorlevel%
