"""Paths for development and PyInstaller-frozen executables."""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def import_tree_root() -> str:
    """Parent directory of the `common` package (repo root or PyInstaller extract root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exe_dir() -> str:
    """Directory containing the running .exe (frozen) or interpreter (dev)."""
    return os.path.dirname(os.path.abspath(sys.executable))


def bundle_root() -> str:
    """
    冻结运行时资源根目录：``sys._MEIPASS``（单文件 exe 解压目录或 onedir 的 ``_internal``），
    否则为仓库根。
    """
    if is_frozen():
        me = getattr(sys, "_MEIPASS", None)
        if me:
            return me
        return exe_dir()
    return import_tree_root()


def runtime_dir() -> str:
    """
    默认可写配置所在目录：开发时为仓库根；打包后优先为 **exe 同级目录**
    （便于在 ``LabTeacher.exe`` 旁放置 ``configs`` 覆盖内置配置）。
    """
    if is_frozen():
        return exe_dir()
    return import_tree_root()


def _frozen_default_config(filename: str) -> str:
    """打包后：exe 旁 ``configs/`` 优先，否则使用 PyInstaller 内置的 ``_MEIPASS/configs/``。"""
    sidecar = os.path.join(exe_dir(), "configs", filename)
    if os.path.isfile(sidecar):
        return sidecar
    bundled = os.path.join(bundle_root(), "configs", filename)
    if os.path.isfile(bundled):
        return bundled
    return sidecar


def default_teacher_config_path() -> str:
    if is_frozen():
        return _frozen_default_config("teacher.json")
    return os.path.join(import_tree_root(), "configs", "teacher.json")


def default_agent_config_path() -> str:
    if is_frozen():
        return _frozen_default_config("agent.json")
    return os.path.join(import_tree_root(), "configs", "agent.json")


def resolve_config_path(path: str) -> str:
    """绝对路径原样返回；相对路径相对 ``runtime_dir()``（打包后为 exe 所在目录）。"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(runtime_dir(), path))


def ensure_import_root() -> None:
    """Put import tree root on sys.path (parent of ``common``)."""
    root = import_tree_root()
    if root not in sys.path:
        sys.path.insert(0, root)
