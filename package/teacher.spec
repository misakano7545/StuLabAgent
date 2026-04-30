# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 教师端 — 单文件 exe。在仓库根目录执行:
   python -m PyInstaller --clean --noconfirm package/teacher.spec
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all

_spec_container = (
    SPECPATH if os.path.isdir(SPECPATH) else os.path.dirname(os.path.abspath(SPECPATH))
)
REPOROOT = os.path.abspath(os.path.join(_spec_container, ".."))
ENTRY = os.path.join(REPOROOT, "teacher", "main.py")

if sys.platform == "win32":
    tk_datas, tk_binaries, tk_hidden = collect_all("tkinter")
else:
    tk_datas, tk_binaries, tk_hidden = [], [], []

datas = [
    (os.path.join(REPOROOT, "configs", "teacher.json"), "configs"),
]
datas += tk_datas

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[REPOROOT],
    binaries=tk_binaries,
    datas=datas,
    hiddenimports=list(
        {
            "common",
            "common.protocol",
            "common.paths",
            "teacher",
            "teacher.server",
            "teacher.ui",
            *tk_hidden,
        }
    ),
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
    name="LabTeacher",
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
