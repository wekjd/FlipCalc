@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 goto nopython

echo Установка нужных библиотек - один раз...
%PY% -m pip install -q fastapi uvicorn httpx
if errorlevel 1 goto piperr

where ssh >nul 2>&1
if errorlevel 1 echo ВНИМАНИЕ: не найден ssh. Telegram Mini App работать не будет. Включите OpenSSH Client в Параметры - Приложения - Дополнительные компоненты.

echo Запуск. Это окно не закрывайте, пока пользуетесь FlipCalc.
start "" /min cmd /c "timeout /t 4 >nul && start http://localhost:8000"
%PY% -m uvicorn yoo_proxy:app --host 127.0.0.1 --port 8000
echo.
echo Сервер остановился. Если выше есть ошибка - пришлите её текст.
pause
exit /b

:nopython
echo Python не найден. Скачайте его на python.org и при установке поставьте галочку Add Python to PATH.
pause
exit /b

:piperr
echo Не удалось установить библиотеки. Проверьте интернет или VPN и запустите ещё раз.
pause
exit /b
