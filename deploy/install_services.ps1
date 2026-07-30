# Разовая установка. Запускать НА СЕРВЕРЕ, от администратора, один раз.
#
# Ставит как настоящую Windows-службу только monitor_pl.py — он полностью
# фоновый, интерфейса не открывает.
#
# waybill_app.py службой НЕ делаем: он открывает окна браузера при появлении
# скана (webbrowser.open в коде), а у Windows-служб нет рабочего стола
# (Session 0) — окна просто некому будет показать. Для waybill_app.py
# используется deploy\start_waybill_app.bat через автозагрузку интерактивной
# сессии — см. раздел «Деплой на сервере» в GUIDE.md.
#
# Перед запуском: nssm.exe должен лежать в $RepoPath (скачать с https://nssm.cc/download),
# зависимости должны быть поставлены (python -m pip install -r requirements.txt).

param(
    [string]$RepoPath = "D:\Users\operator9\Desktop\waybill",
    [string]$NssmExe  = "D:\Users\operator9\Desktop\waybill\nssm.exe",
    # Путь к python.exe. По умолчанию берём тот, что первым найдётся в PATH
    # (venv не используется — зависимости ставились в системный Python).
    [string]$PythonExe = "python.exe",
    # Учётка, под которой будет работать служба MonitorPL — важно указать,
    # если сетевая шара со сканами доступна не системной учётке Local System,
    # а конкретному пользователю/сервисному аккаунту.
    # Пример: -ServiceUser ".\waybill_service" (или "ДОМЕН\waybill_service")
    [string]$ServiceUser,
    [string]$ServicePassword
)

if (-not (Test-Path $NssmExe)) {
    Write-Error "nssm.exe не найден по пути $NssmExe. Скачайте с https://nssm.cc/download и положите туда."
    exit 1
}

$python = (Get-Command $PythonExe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Error "python не найден в PATH. Проверь: python --version"
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $RepoPath "logs") | Out-Null

& $NssmExe install MonitorPL $python (Join-Path $RepoPath "monitor_pl.py")
& $NssmExe set MonitorPL AppDirectory $RepoPath
& $NssmExe set MonitorPL AppStdout (Join-Path $RepoPath "logs\MonitorPL.out.log")
& $NssmExe set MonitorPL AppStderr (Join-Path $RepoPath "logs\MonitorPL.err.log")
& $NssmExe set MonitorPL AppRotateFiles 1
& $NssmExe set MonitorPL AppRotateBytes 5242880
& $NssmExe set MonitorPL Start SERVICE_AUTO_START
& $NssmExe set MonitorPL AppRestartDelay 5000

if ($ServiceUser) {
    if (-not $ServicePassword) {
        Write-Error "Указан -ServiceUser, но не указан -ServicePassword."
        exit 1
    }
    & $NssmExe set MonitorPL ObjectName $ServiceUser $ServicePassword
}

& $NssmExe start MonitorPL

Write-Host ""
Write-Host "Служба MonitorPL установлена и запущена."
Write-Host "Проверить: Get-Service MonitorPL"
Write-Host "Логи: $RepoPath\logs\"
Write-Host ""
Write-Host "waybill_app.py — НЕ служба, см. раздел про start_waybill_app.bat в GUIDE.md"
