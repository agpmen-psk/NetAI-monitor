# install_service.ps1 — установка серверных процессов NetAI Monitor
# службами Windows через NSSM. Сервер состоит из ТРЁХ независимых процессов
# (см. README.md/DEPLOYMENT.md) — скрипт ставит их по одному, вызывается
# трижды с разным -Service:
#
#     .\tools\install_service.ps1 -Service realtime
#     .\tools\install_service.ps1 -Service worker
#     .\tools\install_service.ps1 -Service api
#
# Запускать в PowerShell ОТ АДМИНИСТРАТОРА.
#
# Что делает:
#   * находит nssm.exe (в PATH или рядом в tools\nssm.exe);
#   * ставит (или обновляет, если уже стоит) соответствующую службу
#     с автозапуском при загрузке ПК и автоперезапуском при падении;
#   * ЯВНО заключает путь к скрипту в кавычки — иначе путь с пробелами
#     (например «...\Рабочий стол\NetAI NEW\...») приходит в python как
#     несколько аргументов, и он падает с «can't open file» (код 2);
#   * перенаправляет stdout/stderr в файлы: без этого ранние падения
#     (ошибка импорта, неверный путь) не попадают никуда вообще —
#     собственный лог приложения к тому моменту ещё не открыт;
#   * создаёт общий файл настроек службы (service_config.json), если его
#     ещё нет — он один на все три процесса.
#
# Удалить службу:  .\tools\install_service.ps1 -Service realtime -Remove

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("realtime", "worker", "api")]
    [string]$Service,
    [string]$PythonExe = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# Метаданные трёх серверных процессов — единственное место, которое надо
# менять, если появится четвёртый процесс или поменяются имена файлов.
$ServiceDefs = @{
    "realtime" = @{
        ServiceName = "NetAIMonitorRealtime"
        Script      = "realtime_service.py"
        Display     = "NetAI Monitor — сервис реального времени"
        Description = "Опрос Zabbix, анализ инцидентов через локальную LLM (Ollama) и запись в общую БД PostgreSQL."
        AppLog      = "realtime.log"
    }
    "worker" = @{
        ServiceName = "NetAIMonitorJobWorker"
        Script      = "job_worker.py"
        Display     = "NetAI Monitor — очередь LLM-заданий"
        Description = "Обрабатывает очередь заданий (аудит/diff конфигураций, сравнение с RAG/без, переиндексация), поставленных API-сервером."
        AppLog      = "job_worker.log"
    }
    "api" = @{
        ServiceName = "NetAIMonitorApi"
        Script      = "api_server.py"
        Display     = "NetAI Monitor — API-сервер"
        Description = "Единственная точка входа для десктоп-клиентов (FastAPI/uvicorn) — авторизация, чтение/запись истории, чат, очередь заданий."
        AppLog      = "api_server.log"
    }
}
$def = $ServiceDefs[$Service]
$ServiceName = $def.ServiceName

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
$scriptPath = Join-Path $projectDir $def.Script
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
        throw "python.exe не найден в PATH. Укажите путь явно: -PythonExe 'C:\Path\to\python.exe'"
    }
    $PythonExe = $pythonCmd.Source
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "Служба:  $ServiceName ($($def.Display))"
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
& $nssm set $ServiceName DisplayName $def.Display | Out-Null
& $nssm set $ServiceName Description $def.Description | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
# Перезапускать процесс при любом падении, с паузой 10 секунд.
& $nssm set $ServiceName AppExit Default Restart | Out-Null
& $nssm set $ServiceName AppRestartDelay 10000 | Out-Null

# stdout/stderr в файлы с ротацией силами NSSM. Собственный лог приложения
# остаётся основным, но он открывается уже ПОСЛЕ импортов — всё, что падает
# раньше (например, uvicorn не смог забиндить порт), видно только здесь.
$stdoutLog = Join-Path $logDir "service-stdout-$Service.log"
$stderrLog = Join-Path $logDir "service-stderr-$Service.log"
& $nssm set $ServiceName AppStdout $stdoutLog | Out-Null
& $nssm set $ServiceName AppStderr $stderrLog | Out-Null
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

# Общий файл настроек — один на все три процесса, создаём один раз, если
# его ещё нет (повторные вызовы для других -Service его не тронут).
$configFile = Join-Path $dataDir "service_config.json"
& $PythonExe -c "import sys; sys.path.insert(0, r'$projectDir'); from service_config import ensure_config_file; ensure_config_file()" | Out-Null

Write-Host ""
Write-Host "ДАЛЬШЕ:" -ForegroundColor Cyan
Write-Host "  1. Заполните общие параметры (Zabbix/Oxidized/PostgreSQL/Ollama) в файле:"
Write-Host "     $configFile"
Write-Host "  2. Запустите службу:   nssm start $ServiceName"
Write-Host "     (или через services.msc — «$($def.Display)»)"
Write-Host "  3. Логи:"
Write-Host "     $logDir\$($def.AppLog)        — основной лог приложения"
Write-Host "     $stderrLog   — ранние ошибки запуска (если служба не стартует)"
Write-Host ""
Write-Host "Изменения в service_config.json применяются после перезапуска ВСЕХ ТРЁХ служб:"
Write-Host "  nssm restart NetAIMonitorRealtime; nssm restart NetAIMonitorJobWorker; nssm restart NetAIMonitorApi"
Write-Host ""
if ($Service -ne "api") {
    Write-Host "Не забудьте установить и остальные службы (если ещё не сделали):" -ForegroundColor Yellow
    foreach ($key in $ServiceDefs.Keys) {
        if ($key -ne $Service) {
            Write-Host "  .\tools\install_service.ps1 -Service $key"
        }
    }
}
