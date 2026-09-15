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

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# Тонкий клиент больше не подключается к PostgreSQL/Zabbix напрямую
# (psycopg2/pyzabbix — только на сервере, туда PyInstaller не смотрит), но
# добавляет requests (api_client.py, HTTP до api_server.py) и keyring
# (client_config.py, «запомнить меня» в Диспетчере учётных данных Windows).
#
# keyring находит свои бэкенды (в т.ч. Windows) через importlib.metadata
# entry points своего же пакета — PyInstaller не подхватывает это
# автоматически при статическом анализе импортов, поэтому нужны и
# collect_submodules (сами файлы бэкендов), и copy_metadata (метаданные
# пакета, по которым keyring их ищет). Без этого «запомнить меня» молча не
# работал бы в собранном .exe (client_config.py перехватывает исключение),
# хотя из исходников всё было бы в порядке.
hiddenimports = [
    "openpyxl",
    "requests",
] + collect_submodules("keyring.backends")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/icon.ico", "assets"),
        ("assets/fonts/*.ttf", "assets/fonts"),
        *copy_metadata("keyring"),
    ],
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
