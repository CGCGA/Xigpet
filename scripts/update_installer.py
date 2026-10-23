"""Standalone update installer; runs outside application files after the app exits."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time


ROOT_FILES = {"main.py", "VERSION.json", "README.md", "SHARE_MANIFEST.json", "requirements.txt",
              ".editorconfig", ".gitattributes", ".gitignore", "Xig.png", "Xigpet.exe",
              "启动聊天宠物.bat", "打包EXE.bat", "packaging/Xigpet.spec", "media/index.json"}


def allowed_file(name: str) -> bool:
    if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    if name in ROOT_FILES:
        return True
    if len(parts) != 2:
        return False
    folder, base = parts
    if folder in ("xigpet", "scripts", "tests"):
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", base))
    if folder == "config":
        return bool(re.fullmatch(r"[A-Za-z_]+\.example\.(md|txt|json)", base))
    if folder == "animation":
        return base in ("relax.gif", "float.gif", "play.gif")
    if folder == "media":
        return bool(re.fullmatch(r"(xig_[a-z_]+|comfort|happy|hungry|surprised|wink)\.(png|gif)", base))
    return False


def safe_target(root: Path, name: str) -> Path:
    if not allowed_file(name):
        raise ValueError("更新包试图写入非程序文件：" + str(name))
    root = root.resolve()
    path = root / name
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("更新目标不能是符号链接：" + name)
    if not path.resolve().is_relative_to(root):
        raise ValueError("更新目标越出程序目录")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def apply_files(root: Path, payload: Path, files: dict, backup: Path, remove=(), exe_name="Xigpet.exe", retry_seconds=0):
    """Preflight all paths, back up managed files, then replace with rollback on failure."""
    if Path(exe_name).name != exe_name or not re.fullmatch(r"[\w .-]+\.exe", exe_name):
        raise ValueError("程序文件名无效")
    targets = {}
    for name, digest in files.items():
        source = safe_target(payload, name)
        if not re.fullmatch(r"[a-f0-9]{64}", str(digest)) or not source.is_file() or sha256(source) != digest:
            raise ValueError("更新文件校验失败：" + name)
        target = safe_target(root, name)
        if name == "Xigpet.exe" and exe_name != "Xigpet.exe":
            target = root / exe_name
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("程序目标路径无效")
        targets[name] = target
    for name in remove:
        targets.setdefault(name, safe_target(root, name))
    if len({str(path).casefold() for path in targets.values()}) != len(targets):
        raise ValueError("更新文件路径重复")
    backup.mkdir(parents=True, exist_ok=False)
    original = set()
    for name, target in targets.items():
        if target.exists():
            if not target.is_file():
                raise ValueError("更新目标不是普通文件：" + name)
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            original.add(name)
    changed = []
    try:
        # The version is committed last, so an interrupted copy cannot advertise success.
        for name in sorted(targets, key=lambda item: item == "VERSION.json"):
            target = targets[name]
            changed.append(name)
            if name not in files:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".xig-update-tmp")
            try:
                if name == "media/index.json" and target.is_file():
                    incoming = json.loads((payload / name).read_text(encoding="utf-8"))
                    try:
                        existing = json.loads(target.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        raise ValueError("现有媒体标签索引无法读取，已取消更新以保留原文件")
                    if not isinstance(incoming, dict) or not isinstance(existing, dict):
                        raise ValueError("媒体标签索引格式无效")
                    temporary.write_text(json.dumps({**incoming, **existing}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else:
                    shutil.copy2(payload / name, temporary)
                deadline = time.monotonic() + retry_seconds
                while True:
                    try:
                        os.replace(temporary, target)
                        break
                    except PermissionError:
                        if time.monotonic() >= deadline:
                            raise
                        # A one-file exe's bootloader can briefly outlive its GUI process.
                        time.sleep(.1)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        for name in reversed(changed):
            target = targets[name]
            if name in original:
                shutil.copy2(backup / name, target)
            else:
                target.unlink(missing_ok=True)
        raise


def wait_for_exit(pid: int, timeout=60):
    if pid <= 0 or pid == os.getpid():
        raise ValueError("等待退出的进程无效")
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return
            raise OSError("无法等待原程序退出")
        try:
            if kernel.WaitForSingleObject(handle, timeout * 1000) != 0:
                raise TimeoutError("原程序尚未退出，已取消更新")
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise TimeoutError("原程序尚未退出，已取消更新")


def main(plan_path=None):
    if plan_path is None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--plan", required=True)
        plan_path = parser.parse_args().plan
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root = Path(plan["root"]).resolve()
    stage = plan_path.parent
    if stage.parent != root / ".updates":
        raise ValueError("更新暂存位置无效")
    result = {"version": plan["version"], "success": False}
    exited = False
    try:
        wait_for_exit(plan["pid"])
        exited = True
        apply_files(root, stage / "payload", plan["files"], stage / "backup", plan.get("remove", []), plan["exe_name"], retry_seconds=15)
        (root / ".updates" / "installed-files.json").write_text(json.dumps(list(plan["files"])), encoding="utf-8")
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
    (root / ".updates" / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if not exited:
        return
    command = [str(root / plan["exe_name"])] if plan["frozen"] else [plan["python"], "-B", str(root / "main.py")]
    subprocess.Popen(command, cwd=root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    main()
