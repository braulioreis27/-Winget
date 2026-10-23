@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PYEXE="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
if defined PYEXE goto runexe
where py >nul 2>nul
if not errorlevel 1 goto runpy
python -m wpm %*
exit /b %errorlevel%
:runpy
py -3 -m wpm %*
exit /b %errorlevel%
:runexe
"%PYEXE%" -m wpm %*
exit /b %errorlevel%
