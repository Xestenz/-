# Разовая установка. Запускать НА СЕРВЕРЕ, от администратора, один раз.
# Перед запуском: nssm.exe должен лежать в $RepoPath (скачать с https://nssm.cc/download),
# venv должен быть создан и зависимости поставлены (см. деплой-раздел GUIDE.md).

param(
    [string]$RepoPath = "C:\Apps\waybill",
    [string]$NssmExe  = "C:\Apps\waybill\nssm.exe"
)

if (-not (Test-Path $NssmExe)) {
    Write-Error "nssm.exe не найден по пути $NssmExe. Скачайте с https://nssm.cc/download и положите туда."
    exit 1
}

$python = Join-Path $RepoPath "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Не найден venv по пути $python. Сначала: python -m venv venv; venv\Scripts\pip install -r requirements.txt"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $RepoPath "logs") | Out-Null

function Install-PyService($Name, $ScriptFile) {
    & $NssmExe install $Name $python (Join-Path $RepoPath $ScriptFile)
    & $NssmExe set $Name AppDirectory $RepoPath
    & $NssmExe set $Name AppStdout (Join-Path $RepoPath "logs\$Name.out.log")
    & $NssmExe set $Name AppStderr (Join-Path $RepoPath "logs\$Name.err.log")
    & $NssmExe set $Name AppRotateFiles 1
    & $NssmExe set $Name AppRotateBytes 5242880
    & $NssmExe set $Name Start SERVICE_AUTO_START
    & $NssmExe set $Name AppRestartDelay 5000
}

Install-PyService "WaybillApp" "waybill_app.py"
Install-PyService "MonitorPL"  "monitor_pl.py"

& $NssmExe start WaybillApp
& $NssmExe start MonitorPL

Write-Host ""
Write-Host "Службы установлены и запущены: WaybillApp, MonitorPL"
Write-Host "Проверить: Get-Service WaybillApp, MonitorPL"
Write-Host "Логи: $RepoPath\logs\"
