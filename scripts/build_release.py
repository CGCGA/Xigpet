"""Build an update ZIP from public files and an already-built Windows executable."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from .prepare_share import ROOT, prepare_share
from .update_installer import allowed_file
from .updater import version_tuple


def executable_version(exe):
    try:
        from PyInstaller.archive.readers import CArchiveReader
        return json.loads(CArchiveReader(str(exe)).extract("VERSION.json"))["version"]
    except Exception as exc:
        raise ValueError("exe 缺少版本信息，请先使用最新版打包脚本构建。") from exc


def build_release(root: Path, exe: Path, output: Path):
    version = json.loads((root / "VERSION.json").read_text(encoding="utf-8"))["version"]
    version_tuple(version)
    if executable_version(exe) != version:
        raise ValueError("exe 版本与 VERSION.json 不一致，已停止制作更新包。")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"Xigpet-v{version}-windows-x64.zip"
    if archive.exists():
        raise FileExistsError("此版本更新包已存在，请先更新版本号或另选输出目录。")
    build = root / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=build) as directory:
        clean = Path(directory) / "public"
        prepare_share(root, clean)
        public = json.loads((clean / "SHARE_MANIFEST.json").read_text(encoding="utf-8"))
        files = {name: item["sha256"] for name, item in public.items() if allowed_file(name)}
        files["Xigpet.exe"] = hashlib.sha256(exe.read_bytes()).hexdigest()
        manifest = {"format": 1, "version": version, "kind": "windows-x64", "files": files}
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
            for name in files:
                package.write(exe if name == "Xigpet.exe" else clean / name, name)
            package.writestr("UPDATE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=ROOT / "Xigpet.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build_release(ROOT, args.exe, args.output))
