@echo off
REM CH340 COM number wanders between plug-ins. Pass it explicitly (upload.bat COM9)
REM so this can never land on some other attached board by autodetect.
cd /d "%~dp0"
if "%~1"=="" (
  echo usage: upload.bat COMn
  exit /b 1
)
set PIO=pio
where pio >nul 2>&1 || set PIO="%USERPROFILE%\.platformio\penv\Scripts\platformio.exe"
%PIO% run -t upload --upload-port %1
