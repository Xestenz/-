@echo off
REM Перезапускающий цикл для waybill_app.py.
REM Кладётся ярлыком в папку автозагрузки (shell:startup) учётки, под которой
REM держим постоянную RDP-сессию — waybill_app.py открывает окна браузера,
REM а для этого нужен настоящий рабочий стол (в службе Windows его нет).
REM
REM Если процесс упадёт или его "убьют" (deploy\update.ps1 делает это при
REM обновлении) — цикл через 3 секунды запустит его заново с текущим кодом.

cd /d D:\Users\operator9\Desktop\waybill
if not exist logs mkdir logs

:loop
echo [%date% %time%] Запуск waybill_app.py >> logs\WaybillApp.wrapper.log
python waybill_app.py
echo [%date% %time%] Процесс завершился, перезапуск через 3 сек >> logs\WaybillApp.wrapper.log
timeout /t 3 /nobreak >nul
goto loop
