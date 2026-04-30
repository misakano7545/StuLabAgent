# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 学生端 Agent — 单文件 exe。在仓库根目录执行:
   python -m PyInstaller --clean --noconfirm package/agent.spec
"""
import os

_spec_container = (
    SPECPATH if os.path.isdir(SPECPATH) else os.path.dirname(os.path.abspath(SPECPATH))
)
REPOROOT = os.path.abspath(os.path.join(_spec_container, ".."))
ENTRY = os.path.join(REPOROOT, "agent", "main.py")

datas = [
    (os.path.join(REPOROOT, "configs", "agent.json"), "configs"),
]

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[REPOROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "common",
        "common.protocol",
        "common.paths",
        "agent",
        "agent.rename_host",
        "agent.network_config",
        "agent.machine_identity",
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LabAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
