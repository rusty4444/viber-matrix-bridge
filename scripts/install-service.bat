@echo off
REM Install the Viber bridge as a Windows service via NSSM.
REM
REM Prerequisites:
REM   - NSSM placed in this folder (download from https://nssm.cc/download)
REM   - Python virtualenv already set up at .\venv
REM   - config.yaml filled in
REM
REM CRITICAL: The service must run as your logged-in Windows user (not LocalSystem),
REM or it cannot see the Viber window. You'll be prompted for the password.

setlocal
set SVC=ViberMatrixBridge
set SCRIPT_DIR=%~dp0
set PY=%SCRIPT_DIR%venv\Scripts\python.exe
set BRIDGE=%SCRIPT_DIR%bridge.py
set CFG=%SCRIPT_DIR%config.yaml
set NSSM=%SCRIPT_DIR%nssm.exe

if not exist "%NSSM%" (
  echo ERROR: nssm.exe not found in %SCRIPT_DIR%
  echo Download from https://nssm.cc/download and place nssm.exe here.
  exit /b 1
)
if not exist "%PY%" (
  echo ERROR: venv not found. Run: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
  exit /b 1
)
if not exist "%CFG%" (
  echo ERROR: config.yaml missing. Copy config.example.yaml and edit.
  exit /b 1
)

echo Installing service %SVC%...
"%NSSM%" install %SVC% "%PY%" "%BRIDGE%" --config "%CFG%"
"%NSSM%" set %SVC% AppDirectory "%SCRIPT_DIR%"
"%NSSM%" set %SVC% DisplayName "Viber Matrix Bridge"
"%NSSM%" set %SVC% Description "Bridges Viber Desktop to Matrix via UI Automation"
"%NSSM%" set %SVC% AppStdout "%SCRIPT_DIR%service.stdout.log"
"%NSSM%" set %SVC% AppStderr "%SCRIPT_DIR%service.stderr.log"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateBytes 10485760
"%NSSM%" set %SVC% Start SERVICE_AUTO_START

echo.
echo IMPORTANT: NSSM will now prompt for the Windows account to run the service as.
echo Use your logged-in user (format: .\YourUsername) so the service can access Viber.
echo.
"%NSSM%" edit %SVC%

echo.
echo To start:  net start %SVC%
echo To stop:   net stop %SVC%
echo To check:  sc query %SVC%
