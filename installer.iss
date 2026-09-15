; Inno Setup скрипт для NetAI Monitor.
; Собирает установщик из dist/NetAI-Monitor (результат `pyinstaller netai_monitor.spec`).
; Сборка: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName "NetAI Monitor"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "AGPMEN"
#define MyAppExeName "NetAI-Monitor.exe"

[Setup]
AppId={{B7B6B9B0-6C9E-4C6E-9D1C-9C1E7E6E1A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=NetAI-Monitor-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Установщик {#MyAppName}
AppPublisherURL=https://github.com/agpmen-psk/NetAI-monitor
AppSupportURL=https://github.com/agpmen-psk/NetAI-monitor
AppUpdatesURL=https://github.com/agpmen-psk/NetAI-monitor

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительные значки:"

[Files]
Source: "dist\NetAI-Monitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion; DestName: "README.txt"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Messages]
russian.WelcomeLabel2=Будет установлено приложение [name/ver] для интеллектуального анализа сетевых инцидентов и конфигураций на локальной LLM.%n%nЭто тонкий клиент — вся обработка данных и модель выполняются на сервере предприятия. При первом запуске потребуется указать адрес сервера (например, http://адрес-сервера:8000) и войти под своей учётной записью, выданной администратором.
