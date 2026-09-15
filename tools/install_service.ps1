# install_service.ps1 — установка realtime_service.py службой Windows через NSSM.
#
# Запускать в PowerShell ОТ АДМИНИСТРАТОРА из корня проекта:
#     .\tools\install_service.ps1
#
# Что делает:
#   * находит nssm.exe (в PATH или рядом в tools\nssm.exe);
#   * ставит службу NetAIMonitorRealtime с автозапуском при загрузке ПК;
#   * включает автоперезапуск при падении процесса;
#   * создаёт файл настроек службы (%PROGRAMDATA%\NetAI Monitor\service_config.json),
#     если его ещё нет, и показывает путь — заполнить параметры Zabbix.
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

if ($Remove) {
    & $nssm stop $ServiceName confirm
    & $nssm remove $ServiceName confirm
    Write-Host "Служба $ServiceName удалена." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $scriptPath)) {
    throw "Не найден $scriptPath — запускайте скрипт из корня проекта."
}

if (-not $PythonExe) {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw "python.exe не найден в PATH. Укажите путь явно: .\tools\install_service.ps1 -PythonExe 'C:\Path\to\python.exe'"
    }
    $PythonExe = $pythonCmd.Source
}

Write-Host "Python:  $PythonExe"
Write-Host "Скрипт:  $scriptPath"
Write-Host "NSSM:    $nssm"

& $nssm install $ServiceName $PythonExe $scriptPath
& $nssm set $ServiceName AppDirectory $projectDir
& $nssm set $ServiceName DisplayName "NetAI Monitor — сервис реального времени"
& $nssm set $ServiceName Description "Опрос Zabbix, анализ инцидентов через локальную LLM (Ollama) и запись в общую БД PostgreSQL."
& $nssm set $ServiceName Start SERVICE_AUTO_START
# Перезапускать процесс при любом падении, с паузой 10 секунд.
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 10000
# Служба сама пишет лог с ротацией в %PROGRAMDATA%\NetAI Monitor\logs\realtime.log
# (см. realtime_service.py), поэтому stdout/stderr дополнительно не перенаправляем.

Write-Host ""
Write-Host "Служба $ServiceName установлена." -ForegroundColor Green

# Создаём файл настроек, чтобы администратору было что заполнить.
$configDir = Join-Path $env:PROGRAMDATA "NetAI Monitor"
$configFile = Join-Path $configDir "service_config.json"
& $PythonExe -c "import sys; sys.path.insert(0, r'$projectDir'); from service_config import ensure_config_file; print(ensure_config_file()[0])" | Out-Null

Write-Host ""
Write-Host "ДАЛЬШЕ:" -ForegroundColor Cyan
Write-Host "  1. Заполните параметры Zabbix и PostgreSQL в файле:"
Write-Host "     $configFile"
Write-Host "  2. Запустите службу:   nssm start $ServiceName"
Write-Host "     (или через services.msc — «NetAI Monitor — сервис реального времени»)"
Write-Host "  3. Лог службы:         $configDir\logs\realtime.log"
Write-Host ""
Write-Host "Изменения в service_config.json применяются после перезапуска службы:"
Write-Host "  nssm restart $ServiceName"
