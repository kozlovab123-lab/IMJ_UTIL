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

if not exist ".env" (
    echo Предупреждение: файл .env не найден. Скопируйте .env.example в .env и заполните GIGACHAT_BASIC_AUTH.
    echo.
)

echo IMJ_UTIL — пакетный анализ из issues-examples-v2.xls
echo Остановка: Ctrl+C
echo.

".venv\Scripts\python.exe" main.py analyze-batch --issues-file issues-examples-v2.xls --image-field image_url %*

set EXIT_CODE=%ERRORLEVEL%
if %EXIT_CODE% neq 0 (
    echo.
    echo Завершено с кодом %EXIT_CODE%
    pause
)
exit /b %EXIT_CODE%
