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

if not exist "data" mkdir data

echo IMJ_UTIL — выгрузка записей с риском выше low в CSV
echo.

".venv\Scripts\python.exe" export_risks.py -o data/elevated_risks.csv --issues-file issues-examples-v2.xls %*

set EXIT_CODE=%ERRORLEVEL%
echo.
if %EXIT_CODE% equ 0 (
    echo Файл: %~dp0data\elevated_risks.csv
) else (
    echo Выгрузка завершилась с ошибкой (код %EXIT_CODE%^)
)
pause
exit /b %EXIT_CODE%
