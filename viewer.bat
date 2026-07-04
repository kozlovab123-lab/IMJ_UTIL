@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Ошибка: не найдено виртуальное окружение .venv
    echo Установите зависимости:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo IMJ_UTIL Viewer — просмотр базы (только чтение)
echo Откройте в браузере: http://127.0.0.1:8765
echo Остановка: Ctrl+C
echo.

".venv\Scripts\python.exe" viewer.py --host 127.0.0.1 --port 8765 %*

set EXIT_CODE=%ERRORLEVEL%
if %EXIT_CODE% neq 0 pause
exit /b %EXIT_CODE%
