@echo off
setlocal
rem pmem launcher: pick a Python >= 3.11 automatically.
rem Override by setting PMEM_PYTHON to a full path of python.exe.
rem NOTE 1: keep this file ASCII-only. cmd.exe reads .bat as the OEM codepage,
rem         so non-ASCII comments would be executed as garbage commands.
rem NOTE 2: no parenthesized blocks around the final call. Inside a block,
rem         %errorlevel% is expanded when the block is PARSED, so the exit
rem         code of the command just run would be lost (it always passed 0).
rem         Using labels keeps %errorlevel% expanding per line.

if defined PMEM_PYTHON goto use_custom

set "CAND=C:\Program Files\DSH Desktop\resources\office-runtime\primary-runtime\dependencies\python\python.exe"
if exist "%CAND%" goto use_cand

where py >nul 2>nul
if %errorlevel%==0 goto use_py

goto use_plain

:use_custom
"%PMEM_PYTHON%" -m pmem %*
exit /b %errorlevel%

:use_cand
"%CAND%" -m pmem %*
exit /b %errorlevel%

:use_py
py -3.13 -m pmem %* 2>nul
if %errorlevel%==0 exit /b 0
py -3.12 -m pmem %* 2>nul
if %errorlevel%==0 exit /b 0
py -3.11 -m pmem %* 2>nul
if %errorlevel%==0 exit /b 0
rem none of 3.11-3.13 worked; fall through to a plain python so the user
rem sees the real error instead of a silent failure
goto use_plain

:use_plain
python -m pmem %*
exit /b %errorlevel%
