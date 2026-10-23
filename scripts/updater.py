"""GitHub update discovery, bounded downloads and validated update staging."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid
import zipfile

from .update_installer import allowed_file, safe_target, sha256
from xigpet.version import REPOSITORY, VERSION


MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_UNPACKED = 500 * 1024 * 1024
API = "https://api.github.com/repos/" + REPOSITORY


class UpdateError(ValueError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    kind: str
    digest: str = ""
    checksum_url: str = ""


@dataclass
class PreparedUpdate:
    stage: Path
    version: str
    files: dict
    frozen: bool


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise UpdateError("版本号格式无效，需要例如 1.2.0。")
    return tuple(int(item) for item in value.removeprefix("v").split("."))


def _read_url(url, limit):
    request = Request(url, headers={"User-Agent": "Xigpet/" + VERSION, "Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=25) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("GitHub 返回的数据过大。")
    return data


def _json(url):
    return json.loads(_read_url(url, 2 * 1024 * 1024))


def check_for_update(current=VERSION, *, frozen=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    current_version = version_tuple(current)
    try:
        if not frozen:
            commit = _json(API + "/commits/main")["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                raise UpdateError("GitHub 提交信息无效。")
            version = _json(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/VERSION.json")["version"]
            if version_tuple(version) <= current_version:
                return None
            return UpdateInfo(version.removeprefix("v"), f"https://codeload.github.com/{REPOSITORY}/zip/{commit}", "source")
        release = _json(API + "/releases/latest")
        version = release["tag_name"]
        if release.get("draft") or release.get("prerelease"):
            raise UpdateError("没有可用的正式更新版本。")
        if version_tuple(version) <= current_version:
            return None
        version = version.removeprefix("v")
        name = f"Xigpet-v{version}-windows-x64.zip"
        assets = {item["name"]: item for item in release.get("assets", [])}
        if name not in assets:
            raise UpdateError(f"发现 {version}，但作者尚未上传 Windows 更新包。")
        asset = assets[name]
        digest = str(asset.get("digest") or "").removeprefix("sha256:")
        checksum = assets.get(name + ".sha256", {}).get("browser_download_url", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) and not checksum:
            raise UpdateError("Windows 更新包缺少 SHA-256 校验信息。")
        prefix = f"https://github.com/{REPOSITORY}/releases/download/"
        if not asset["browser_download_url"].startswith(prefix) or (checksum and not checksum.startswith(prefix)):
            raise UpdateError("更新资源地址不属于指定仓库。")
        return UpdateInfo(version, asset["browser_download_url"], "windows-x64", digest, checksum)
    except HTTPError as exc:
        if exc.code == 404:
            if frozen:
                raise UpdateError("GitHub 尚未发布 Windows 更新包。exe 版需要作者在 Releases 发布对应更新包；仅上传源码不会更新 exe。") from exc
            raise UpdateError("GitHub 尚未发布可用的源码版本，请稍后再检查。") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub 暂时限制访问，请稍后重试。") from exc
        raise UpdateError(f"GitHub 检查失败（HTTP {exc.code}）。") from exc
    except (OSError, URLError) as exc:
        raise UpdateError("无法连接 GitHub，请检查网络后重试。") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub 的版本信息格式无效。") from exc


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("已取消更新。")


def _download(url, target, progress, cancel):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("github.com", "codeload.github.com"):
        raise UpdateError("下载地址无效。")
    request = Request(url, headers={"User-Agent": "Xigpet/" + VERSION})
    with urlopen(request, timeout=30) as response, target.open("xb") as stream:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or not (final.hostname in ("github.com", "codeload.github.com")
                                            or (final.hostname or "").endswith(".githubusercontent.com")):
            raise UpdateError("下载发生了不受支持的重定向。")
        size = int(response.headers.get("Content-Length", 0))
        if size > MAX_DOWNLOAD:
            raise UpdateError("更新包超过允许的大小。")
        count = 0
        while True:
            _cancelled(cancel)
            block = response.read(256 * 1024)
            if not block:
                break
            count += len(block)
            if count > MAX_DOWNLOAD:
                raise UpdateError("更新包超过允许的大小。")
            stream.write(block)
            if progress:
                progress(count, size)
        if size and count != size:
            raise UpdateError("更新包下载不完整。")


def unpack_package(archive: Path, payload: Path, info: UpdateInfo):
    with zipfile.ZipFile(archive) as package:
        entries = [entry for entry in package.infolist() if not entry.is_dir()]
        if len(entries) > 2000 or any(entry.file_size > 150 * 1024 * 1024 for entry in entries) or sum(entry.file_size for entry in entries) > MAX_UNPACKED:
            raise UpdateError("更新包解压大小或文件数量异常。")
        names = [entry.filename for entry in entries]
        if any("\\" in name or ":" in name or name.startswith("/")
               or any(part in ("", ".", "..") for part in name.split("/")) for name in names):
            raise UpdateError("更新包包含越界路径。")
        prefix = ""
        if info.kind == "source":
            roots = {PurePosixPath(name).parts[0] for name in names}
            if len(roots) != 1 or any(len(PurePosixPath(name).parts) < 2 for name in names):
                raise UpdateError("GitHub 源码包目录结构无效。")
            prefix = next(iter(roots)) + "/"
        by_name = {}
        for entry in entries:
            name = entry.filename.removeprefix(prefix)
            if stat.S_ISLNK(entry.external_attr >> 16) or name.casefold() in {n.casefold() for n in by_name}:
                raise UpdateError("更新包包含符号链接或重复路径。")
            by_name[name] = entry
        manifest_name = "SHARE_MANIFEST.json" if info.kind == "source" else "UPDATE_MANIFEST.json"
        if manifest_name not in by_name or by_name[manifest_name].file_size > 1024 * 1024:
            raise UpdateError("更新包缺少有效的文件校验清单。")
        manifest = json.loads(package.read(by_name[manifest_name]))
        if not isinstance(manifest, dict):
            raise UpdateError("更新清单格式无效。")
        if info.kind == "source":
            if not all(isinstance(item, dict) and "sha256" in item for item in manifest.values()):
                raise UpdateError("更新清单格式无效。")
            files = {name: item["sha256"] for name, item in manifest.items()}
        else:
            if manifest.get("format") != 1 or manifest.get("version") != info.version or manifest.get("kind") != info.kind:
                raise UpdateError("更新包版本或类型不匹配。")
            files = manifest["files"]
        if not isinstance(files, dict) or not files:
            raise UpdateError("更新文件清单为空或无效。")
        allowed_placeholders = {"memory/.gitkeep", "plans/.gitkeep", "media/drawings/.gitkeep"}
        if set(by_name) - set(files) - {manifest_name}:
            raise UpdateError("更新包包含未登记的文件。")
        verified = {}
        payload.mkdir(parents=True, exist_ok=False)
        for name, digest in files.items():
            if name in allowed_placeholders:
                continue
            if not allowed_file(name) or (name == "Xigpet.exe" and info.kind == "source"):
                raise UpdateError("更新包包含非程序文件：" + str(name))
            if name not in by_name or not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
                raise UpdateError("更新清单无效：" + str(name))
            data = package.read(by_name[name])
            if hashlib.sha256(data).hexdigest() != digest:
                raise UpdateError("更新文件校验失败：" + name)
            target = safe_target(payload, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            verified[name] = digest
        if "VERSION.json" not in verified or json.loads((payload / "VERSION.json").read_text(encoding="utf-8"))["version"] != info.version:
            raise UpdateError("更新包声明的版本与实际文件不一致。")
        if "main.py" not in verified or (info.kind != "source" and "Xigpet.exe" not in verified):
            raise UpdateError("更新包缺少程序入口。")
        return verified


def prepare_update(root: Path, info: UpdateInfo, progress=None, cancel=None):
    _cancelled(cancel)
    updates = root / ".updates"
    if updates.is_symlink():
        raise UpdateError("更新缓存目录不能是符号链接。")
    stage = updates / ("update-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    archive = stage / "download.zip"
    try:
        _download(info.url, archive, progress, cancel)
        _cancelled(cancel)
        if info.kind != "source":
            digest = info.digest
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                text = _read_url(info.checksum_url, 4096).decode("ascii").strip()
                digest = text.split()[0]
            if not re.fullmatch(r"[a-f0-9]{64}", digest) or sha256(archive) != digest:
                raise UpdateError("更新包 SHA-256 校验失败，未修改当前程序。")
        files = unpack_package(archive, stage / "payload", info)
        _cancelled(cancel)
        return PreparedUpdate(stage, info.version, files, info.kind != "source")
    except (OSError, URLError, zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新包下载或校验失败，当前程序未改变。") from exc


def launch_installer(root: Path, prepared: PreparedUpdate):
    root = root.resolve()
    if prepared.stage.resolve().parent != root / ".updates":
        raise UpdateError("更新暂存目录无效。")
    previous = root / ".updates" / "installed-files.json"
    remove = []
    if previous.is_file():
        try:
            remove = [name for name in json.loads(previous.read_text(encoding="utf-8"))
                      if name not in prepared.files and allowed_file(name)]
        except (ValueError, TypeError):
            pass
    plan = {"root": str(root), "pid": os.getpid(), "version": prepared.version, "files": prepared.files,
            "remove": remove, "frozen": prepared.frozen, "python": sys.executable,
            "exe_name": Path(sys.executable).name if prepared.frozen else "Xigpet.exe"}
    plan_path = prepared.stage / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if prepared.frozen:
        helper = prepared.stage / "update-helper.exe"
        shutil.copy2(sys.executable, helper)
        command = [str(helper), "--apply-update", str(plan_path)]
    else:
        helper = prepared.stage / "update_installer.py"
        shutil.copy2(Path(__file__).with_name("update_installer.py"), helper)
        command = [sys.executable, "-B", str(helper), "--plan", str(plan_path)]
    return subprocess.Popen(command, cwd=prepared.stage, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
