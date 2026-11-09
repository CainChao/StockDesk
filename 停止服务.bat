@echo off
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765 .*LISTENING"') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8766 .*LISTENING"') do taskkill /F /PID %%p >nul 2>&1
exit
