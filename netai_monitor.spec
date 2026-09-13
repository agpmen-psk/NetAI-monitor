# -*- mode: python ; coding: utf-8 -*-
"""
Спецификация PyInstaller для NetAI Monitor.

Сборка (из корня проекта, с активным venv, где стоит requirements.txt):
    pyinstaller netai_monitor.spec --clean

Результат: dist/NetAI-Monitor/NetAI-Monitor.exe (папка с приложением) —
её же пакует installer.iss (Inno Setup) в единый setup.exe для конечного
пользователя. Если нужен один портативный .exe без папки — замените
COLLECT ниже на EXE(..., a.binaries, a.datas, ... , onefile=True) ценой
более медленного запуска (распаковка во временную папку при каждом старте).
"""

hiddenimports = [
    "psycopg2",
    "openpyxl",
    "pyzabbix",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/icon.ico", "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NetAI-Monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # без консольного окна; поставьте True для отладки сборки
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="NetAI-Monitor",
)
