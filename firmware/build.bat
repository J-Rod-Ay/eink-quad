@echo off
cd /d "%~dp0"
set PIO=pio
where pio >nul 2>&1 || set PIO="%USERPROFILE%\.platformio\penv\Scripts\platformio.exe"
%PIO% run %*
