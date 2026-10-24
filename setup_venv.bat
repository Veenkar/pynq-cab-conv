@echo off
rem Create .venv from the python on PATH and install requirements.txt.
rem   setup_venv.bat              create if missing (recreated if the Python version changed)
rem   setup_venv.bat --reinstall  delete and recreate .venv
setlocal
cd /d "%~dp0"
if "%PYTHON%"=="" set PYTHON=python
set VENV=.venv

if "%~1"=="--reinstall" (
    if exist "%VENV%" rmdir /s /q "%VENV%"
) else if not "%~1"=="" (
    echo usage: setup_venv.bat [--reinstall]
    exit /b 1
)

rem recreate when the base Python version differs from the one the venv was built with
for /f %%v in ('%PYTHON% -c "import platform; print(platform.python_version())"') do set WANT=%%v
if "%WANT%"=="" (
    echo python not found on PATH
    exit /b 1
)
if exist "%VENV%\pyvenv.cfg" (
    set HAVE=
    for /f "tokens=1,* delims== " %%a in ('findstr /b /r "version" "%VENV%\pyvenv.cfg"') do if not defined HAVE set HAVE=%%b
)
if exist "%VENV%\pyvenv.cfg" call :check
rem "call": %PYTHON% may itself be a .bat (e.g. a pyenv-win shim)
if not exist "%VENV%" call %PYTHON% -m venv "%VENV%" || exit /b 1
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
echo ready: %VENV%\Scripts\python.exe (Python %WANT%)
exit /b 0

:check
if not "%HAVE%"=="%WANT%" (
    echo venv Python %HAVE% != %WANT%, recreating
    rmdir /s /q "%VENV%"
)
exit /b 0
