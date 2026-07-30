# Обновление на сервере. Запускать НА СЕРВЕРЕ по RDP после того, как с рабочего
# компьютера запушили изменения в git-репозиторий.
#
# Использование:
#   .\deploy\update.ps1                 — подтянуть код и перезапустить службы
#   .\deploy\update.ps1 -UpdateDeps     — то же + переустановить зависимости venv
#                                          (нужно, если менялся requirements.txt)

param(
    [string]$RepoPath = "C:\Apps\waybill",
    [switch]$UpdateDeps
)

Set-Location $RepoPath

Write-Host "== git pull ==" -ForegroundColor Cyan
git pull
if ($LASTEXITCODE -ne 0) {
    Write-Error "git pull упал — смотри вывод выше. Службы НЕ перезапущены."
    exit 1
}

if ($UpdateDeps) {
    Write-Host "== pip install -r requirements.txt ==" -ForegroundColor Cyan
    & "$RepoPath\venv\Scripts\pip.exe" install -r requirements.txt
}

Write-Host "== restart services ==" -ForegroundColor Cyan
nssm restart WaybillApp
nssm restart MonitorPL

Start-Sleep -Seconds 2

Write-Host "== status ==" -ForegroundColor Cyan
Get-Service WaybillApp, MonitorPL | Format-Table Name, Status

Write-Host "Готово: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
