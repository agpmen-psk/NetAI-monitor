# install_service.ps1 — установка realtime_service.py службой Windows через NSSM.
#
# Запускать в PowerShell ОТ АДМИНИСТРАТОРА:
#     .\tools\install_service.ps1
#
# Что делает:
#   * находит nssm.exe (в PATH или рядом в tools\nssm.exe);
#   * ставит (или обновляет, если уже стоит) службу NetAIMonitorRealtime
#     с автозапуском при загрузке ПК и автоперезапуском при падении;
#   * ЯВНО заключает путь к скрипту в кавычки — иначе путь с пробелами
#     (например «...\Рабочий стол\NetAI NEW\...») приходит в python как
#     несколько аргументов, и он падает с «can't open file» (код 2);
#   * перенаправляет stdout/stderr в файлы: без этого ранние падения
#     (ошибка импорта, неверный путь) не попадают никуда вообще —
#     собственный лог приложения к тому моменту ещё не открыт;
#   * создаёт файл настроек службы, если его ещё нет, и печатает путь.
#
# Удалить службу:  .\tools\install_service.ps1 -Remove

param(
    [string]$ServiceName = "NetAIMonitorRealtime",
    [string]$PythonExe = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

function Find-Nssm {
    $inPath = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($inPath) { return $inPath.Source }
    $local = Join-Path $PSScriptRoot "nssm.exe"
    if (Test-Path $local) { return $local }
    throw "nssm.exe не найден. Скачайте с https://nssm.cc/download и положите в tools\nssm.exe или добавьте в PATH."
}

# Права администратора обязательны: создание/удаление службы Windows.
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Запустите PowerShell от имени администратора — установка службы Windows требует прав администратора."
}

$nssm = Find-Nssm
$projectDir = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectDir "realtime_service.py"
$dataDir = Join-Path $env:PROGRAMDATA "NetAI Monitor"
$logDir = Join-Path $dataDir "logs"

if ($Remove) {
    & $nssm stop $ServiceName confirm 2>&1 | Out-Null
    & $nssm remove $ServiceName confirm
    Write-Host "Служба $ServiceName удалена." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из папки tools внутри проекта."
}

if (-not $PythonExe) {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw "python.exe не найден в PATH. Укажите путь явно: .\tools\install_service.ps1 -PythonExe 'C:\Path\to\python.exe'"
    }
    $PythonExe = $pythonCmd.Source
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "Python:  $PythonExe"
Write-Host "Скрипт:  $scriptPath"
Write-Host "NSSM:    $nssm"
Write-Host ""

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Служба уже установлена — останавливаю и обновляю параметры." -ForegroundColor Yellow
    & $nssm stop $ServiceName confirm 2>&1 | Out-Null
} else {
    & $nssm install $ServiceName $PythonExe | Out-Null
}

& $nssm set $ServiceName Application $PythonExe | Out-Null
& $nssm set $ServiceName AppDirectory $projectDir | Out-Null
& $nssm set $ServiceName DisplayName "NetAI Monitor — сервис реального времени" | Out-Null
& $nssm set $ServiceName Description "Опрос Zabbix, анализ инцидентов через локальную LLM (Ollama) и запись в общую БД PostgreSQL." | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
# Перезапускать процесс при любом падении, с паузой 10 секунд.
& $nssm set $ServiceName AppExit Default Restart | Out-Null
& $nssm set $ServiceName AppRestartDelay 10000 | Out-Null

# stdout/stderr в файлы с ротацией силами NSSM. Собственный лог приложения
# (realtime.log) остаётся основным, но он открывается уже ПОСЛЕ импортов —
# всё, что падает раньше, видно только здесь.
& $nssm set $ServiceName AppStdout (Join-Path $logDir "service-stdout.log") | Out-Null
& $nssm set $ServiceName AppStderr (Join-Path $logDir "service-stderr.log") | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateBytes 5242880 | Out-Null

# AppParameters пишем прямо в реестр: путь к скрипту ОБЯЗАН быть в кавычках
# (пробелы в пути иначе разобьют его на несколько аргументов python), а
# передать литеральные кавычки через nssm.exe средствами PowerShell —
# ненадёжно из-за двойного разбора аргументов нативных команд.
$paramsKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
$quotedScript = '"' + $scriptPath + '"'
Set-ItemProperty -Path $paramsKey -Name "AppParameters" -Value $quotedScript

# Проверяем, что реально записалось — ошибка кавычек стоила отладки,
# больше её молча пропускать нельзя.
$effective = (Get-ItemProperty -Path $paramsKey).AppParameters
Write-Host "AppParameters = $effective"
if ($effective -ne $quotedScript) {
    throw "Не удалось записать AppParameters в кавычках (получилось: $effective). Проверьте права администратора."
}

Write-Host ""
Write-Host "Служба $ServiceName настроена." -ForegroundColor Green

# Создаём файл настроек, чтобы администратору было что заполнить.
$configFile = Join-Path $dataDir "service_config.json"
& $PythonExe -c "import sys; sys.path.insert(0, r'$projectDir'); from service_config import ensure_config_file; ensure_config_file()" | Out-Null

Write-Host ""
Write-Host "ДАЛЬШЕ:" -ForegroundColor Cyan
Write-Host "  1. Заполните параметры Zabbix и PostgreSQL в файле:"
Write-Host "     $configFile"
Write-Host "  2. Запустите службу:   nssm start $ServiceName"
Write-Host "     (или через services.msc — «NetAI Monitor — сервис реального времени»)"
Write-Host "  3. Логи:"
Write-Host "     $logDir\realtime.log         — основной лог приложения"
Write-Host "     $logDir\service-stderr.log   — ранние ошибки запуска (если служба не стартует)"
Write-Host ""
Write-Host "Изменения в service_config.json применяются после перезапуска службы:"
Write-Host "  nssm restart $ServiceName"
