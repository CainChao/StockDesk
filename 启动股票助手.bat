@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 优先使用打包好的 exe（无需 Python 环境）
if exist "%~dp0dist\StockDesk.exe" (
  start "" "%~dp0dist\StockDesk.exe" --isolated
  exit
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0server.py" --isolated
) else (
  start "StockDesk" python "%~dp0server.py" --isolated
)
exit
