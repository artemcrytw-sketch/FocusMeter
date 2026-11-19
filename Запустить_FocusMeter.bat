@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден в PATH.
    echo Установи Python 3.10+ с python.org, поставь галочку Add to PATH и запусти снова.
    pause
    exit /b 1
)

python -c "import pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo Первый запуск: ставлю pystray и pillow, это займёт около минуты...
    python -m pip install --quiet pystray pillow
    if errorlevel 1 (
        echo Не удалось установить библиотеки. Проверь интернет.
        pause
        exit /b 1
    )
)

where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "%~dp0FocusMeter.pyw"
) else (
    start "" pythonw "%~dp0FocusMeter.pyw"
)
