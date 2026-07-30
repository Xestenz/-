# Обновление на сервере. Запускать НА СЕРВЕРЕ по RDP после того, как с рабочего
# компьютера запушили изменения в git-репозиторий.
#
# Использование:
#   .\deploy\update.ps1                 — подтянуть код и перезапустить оба процесса
#   .\deploy\update.ps1 -UpdateDeps     — то же + переустановить зависимости
#                                          (нужно, если менялся requirements.txt)

param(
    [string]$RepoPath = "D:\Users\operator9\Desktop\waybill",
    [switch]$UpdateDeps
)

Set-Location $RepoPath

Write-Host "== git pull ==" -ForegroundColor Cyan
git pull
if ($LASTEXITCODE -ne 0) {
    Write-Error "git pull упал — смотри вывод выше. Ничего не перезапущено."
    exit 1
}

if ($UpdateDeps) {
    Write-Host "== pip install -r requirements.txt ==" -ForegroundColor Cyan
    python -m pip install -r requirements.txt
}

Write-Host "== restart MonitorPL (служба) ==" -ForegroundColor Cyan
nssm restart MonitorPL

Write-Host "== restart WaybillApp (процесс в интерактивной сессии) ==" -ForegroundColor Cyan
$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'waybill_app\.py' }
if ($proc) {
    $proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "Процесс остановлен, start_waybill_app.bat поднимет его заново в течение ~3 сек."
} else {
    Write-Warning "waybill_app.py сейчас не запущен (или не найден процесс) — проверь вручную, что start_waybill_app.bat вообще работает в этой сессии."
}

Start-Sleep -Seconds 5

Write-Host "== status ==" -ForegroundColor Cyan
Get-Service MonitorPL | Format-Table Name, Status
$stillRunning = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'waybill_app\.py' }
if ($stillRunning) {
    Write-Host "WaybillApp: запущен (PID $($stillRunning.ProcessId))" -ForegroundColor Green
} else {
    Write-Warning "WaybillApp: процесс не найден после перезапуска — смотри logs\WaybillApp.wrapper.log"
}

Write-Host "Готово: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
