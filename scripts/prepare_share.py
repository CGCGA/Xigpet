"""Create a clean source distribution from an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = ("main.py", "requirements.txt", ".editorconfig", "Xig.png", "VERSION.json",
             "启动聊天宠物.bat", "打包EXE.bat", "packaging/Xigpet.spec")
MEDIA = {
    "comfort.png": ("抱抱", "安慰 温柔"), "happy.png": ("开心", "开心 庆祝"),
    "hungry.png": ("开饭啦", "吃饭 饿了"), "surprised.png": ("惊讶", "惊讶 好奇"),
    "wink.gif": ("眨眼", "俏皮 开心"), "xig_afraid.png": ("小希害怕", "害怕 紧张"),
    "xig_angry.png": ("小希生气", "生气 不满"), "xig_cry.png": ("小希哭哭", "难过 委屈"),
    "xig_curious.png": ("小希好奇", "好奇 惊讶"), "xig_happy.png": ("小希开心", "开心 庆祝"),
    "xig_love.png": ("小希喜欢", "喜欢 温柔"), "xig_shy.png": ("小希害羞", "害羞 撒娇"),
    "xig_smile.png": ("小希微笑", "微笑 鼓励"), "xig_tire.png": ("小希困困", "困了 疲惫"),
}
SETTING_EXAMPLE = """# 小希的设定

你的名字是小希，是陪用户聊天的桌面宠物。请使用中文，自然、温柔，有自己的想法。
简单问题简短回答，复杂话题按需要展开，不固定分成三段。
情境合适时偶尔使用表情，也可以给自己的画命名并在以后再次分享。
需要旧事实时先检索记忆，不编造经历。需要查看屏幕时使用截图请求工具，等待用户本次确认。
不预设用户姓名、生日、QQ、关系或其他个人信息，由用户自己补充。
"""
GITIGNORE = """__pycache__/
.updates/
*.py[cod]
.pytest_cache/
.venv/
.build-venv/
build/
dist/
old_builds/
tem/
.env
.env.*
*.log
*.exe
*.zip
*.7z
*.bak
*.spec
!packaging/Xigpet.spec
config/deepseek_key.txt
config/setting.md
config/desktop_pet.json
config/qq_bridge.json
memory/*
!memory/.gitkeep
plans/*
!plans/.gitkeep
media/drawings/*
!media/drawings/.gitkeep
notes/
pet/
"""
README = """# Xigpet · Windows 聊天桌宠

使用 DeepSeek 官方 API 的 Windows 桌面聊天宠物，支持桌宠动画、长期记忆、工具调用和可选 QQ 接入。

## 下载和运行

在 GitHub 点击 **Code → Download ZIP**，解压整个项目。当前仓库提供源码，需要 Windows 和 Python 3.10 或更新版本；本仓库不包含预先打包的 exe。

```powershell
python -m pip install -r requirements.txt
python main.py
```

也可以安装依赖后双击 `启动聊天宠物.bat`，或运行 `python -m xigpet`。

首次运行会从 `config/setting.example.md` 创建你的 `config/setting.md`，并创建空 Key 和记忆文件。在窗口中点击 API Key 设置，填入自己的 DeepSeek Key；也可以直接编辑 `config/deepseek_key.txt`，只放一行 Key。按喜好编辑 `config/setting.md`，保存后会自动刷新名字并用于后续聊天。模型默认 `deepseek-flash`，使用 `https://api.deepseek.com/chat/completions`。模型和联网调用需要网络；DeepSeek 调用使用你的账户额度。

## 功能

- 浅蓝聊天界面、图片发送与理解、平滑滚动，主页最多显示 50 条，完整历史按日期查询。
- 人设 Markdown、重要记忆、完整聊天和带原文位置的记忆目录；模型按需要检索旧事。
- 最小化进入置顶桌宠，支持拖动、大小设置、放松/漂浮/玩耍动画和托盘驻留。可以单独关闭桌宠，保留后台聊天与 QQ 功能。
- 每日生成共用主动联系计划，按可行时段上午、下午、晚上各两次；软件运行时执行。
- 时间、百科/萌娘百科、新闻、媒体检索、受限 Python/Pillow 绘画、笔记和截图请求工具。
- 作品保存到 `media/drawings/`，记录名称与标签，之后可检索并再次发送。
- 闲置提醒仅显示桌宠气泡，不加入聊天或转发 QQ；查看屏幕需本次明确确认。截图工具也必须在本机确认，成功后图片才进入聊天和模型输入。
- 可选 SnowLuma OneBot v11 QQ 私聊文字/图片双向同步。SnowLuma 不包含在本项目中，需自行安装、启动并登录宠物 QQ。右键桌宠或托盘打开 QQ 设置，读取自己的 `onebot_账号.json`，填写用户 QQ，保存并启用。默认本机地址 `ws://127.0.0.1:3001/`，需要开启正向 WebSocket；Token 使用当前服务配置，不是 DeepSeek Key。

## 目录

| 目录 | 用途 |
| --- | --- |
| `xigpet/` | 窗口、模型循环、记忆、日程和 QQ 接入 |
| `scripts/` | 绘画、图片、媒体、联网、截图和分享辅助工具 |
| `config/` | 公开模板；运行后生成你自己的配置 |
| `memory/` | 运行后生成的私有记忆、聊天、截图和笔记 |
| `plans/` | 私有每日计划 |
| `media/` | 自带表情；`drawings/` 存放个人作品与索引 |
| `animation/` | 三种桌宠 GIF |
| `tests/` | 使用临时数据、模拟模型和截图的测试 |
| `packaging/` | PyInstaller 配置 |

这个分享副本不包含原使用者的人设、Key、QQ 账号/Token、聊天、记忆、截图、私人笔记或个人画作。真实配置和运行数据已加入 `.gitignore`；不要将填好的配置、整个运行目录或屏幕截图提交到仓库。图片理解及截图会把选定图片发送给 DeepSeek，相关副本保存在本机。

## 自行打包 Windows exe

```powershell
python -m pip install -r requirements.txt pyinstaller
```

双击 `打包EXE.bat`，会在项目根目录生成 `Xigpet.exe`。给别人分发时同时带上公开的 `config/setting.example.md`、`media/` 自带表情、`animation/` 和 `Xig.png`，让对方自行填写 Key。不要附带你运行后产生的真实配置与数据。

## 检查更新

聊天窗口、桌宠右键和托盘菜单都有“检查更新”。发现新版本后自动下载、校验；等当前回复和计划任务完成后，程序退出，由独立更新助手安装并重新打开。取消、网络失败或校验失败都不会覆盖当前程序；文件替换失败会回滚。API Key、人设、QQ 配置、记忆、聊天、截图、笔记、计划和个人画作不属于更新文件。

源码版读取本仓库主分支的 `VERSION.json`，固定到同一个提交下载源码 ZIP，并按 `SHARE_MANIFEST.json` 校验文件。exe 版读取 GitHub Releases 最新正式版本，只接受对应的 Windows 更新 ZIP 及 SHA-256；仅推送源码不会产生新的 exe。当前版本号见 `VERSION.json`，版本号只有变大才更新。

作者发布下一版时：

1. 修改 `VERSION.json`，例如从 `1.1.0` 改为 `1.2.0`。
2. 生成新的干净分享副本，将源码和重新生成的 `SHARE_MANIFEST.json` 一起上传主分支，源码用户即可检查到更新。
3. 若需要更新 exe 用户，先用本版打包脚本生成带版本信息的 `Xigpet.exe`，再执行 `python -m scripts.build_release --exe Xigpet.exe`。
4. 在 GitHub Releases 创建对应的正式版本，例如标签 `v1.2.0`，上传生成的 `dist/Xigpet-v1.2.0-windows-x64.zip` 和同名 `.zip.sha256` 文件。不要改更新包文件名，不要附带真实配置和运行数据。

更新缓存、结果与旧程序备份位于本机 `.updates/`，已被 Git 忽略。程序必须位于可写目录；源码版仍需本机已有所需 Python 依赖。这个功能首次使用需要安装包含更新入口的新版本，旧 exe 不会自行获得入口。

## 测试与再次分享

```powershell
python -B -m unittest discover -s tests -v
python -m scripts.prepare_share --output tem
```

分享工具按白名单复制源码和自带资源，重新生成通用模板，并排除私人文件。目标目录已存在时会停止，不会覆盖已有内容。`SHARE_MANIFEST.json` 记录分享文件的大小与 SHA-256，用于核对副本。
"""


def private_values(root: Path):
    """Read only explicit private fields for exact-match checks; never print values."""
    values = []
    key = root / "config/deepseek_key.txt"
    if key.is_file():
        value = key.read_text(encoding="utf-8-sig").strip()
        if len(value) >= 8 and not value.startswith("在这里填写"):
            values.append(value)
    qq = root / "config/qq_bridge.json"
    if qq.is_file():
        data = json.loads(qq.read_text(encoding="utf-8-sig"))
        for name in ("access_token", "target_qq", "expected_self_qq", "snowluma_config"):
            value = data.get(name)
            if isinstance(value, str) and len(value) >= 5:
                values.append(value)
    return values


def prepare_share(root: Path, output: Path):
    root, output = root.resolve(), output.resolve()
    if not output.is_relative_to(root) or output == root:
        raise ValueError("分享目录必须位于项目内部，且不能是项目根目录。")
    if output.exists():
        raise FileExistsError("分享目录已存在；请指定一个新目录，避免覆盖。")
    files = list(TOP_FILES)
    for folder in ("xigpet", "scripts", "tests"):
        files.extend(path.relative_to(root).as_posix() for path in (root / folder).glob("*.py"))
    files.extend("animation/" + name + ".gif" for name in ("relax", "float", "play"))
    files.extend("media/" + name for name in MEDIA)
    for name in files:
        source = root / name
        if not source.is_file() or source.is_symlink():
            raise ValueError("分享所需文件缺失或是符号链接：" + name)
    output.mkdir()
    for name in sorted(set(files)):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    text_files = {
        "README.md": README, ".gitignore": GITIGNORE,
        ".gitattributes": "*.bat -text whitespace=cr-at-eol\n",
        "config/setting.example.md": SETTING_EXAMPLE,
        "config/setting.md": SETTING_EXAMPLE,
        "config/deepseek_key.example.txt": "在这里填写你自己的 DeepSeek API Key，只填一行。\n",
        "config/deepseek_key.txt": "",
        "config/qq_bridge.example.json": json.dumps({"ws_url": "ws://127.0.0.1:3001/", "access_token": "",
            "target_qq": "", "expected_self_qq": "", "snowluma_config": ""}, ensure_ascii=False, indent=2) + "\n",
        "memory/.gitkeep": "", "plans/.gitkeep": "", "media/drawings/.gitkeep": "",
        "media/index.json": json.dumps({name: {"title": title, "mood": mood, "keywords": mood}
            for name, (title, mood) in MEDIA.items()}, ensure_ascii=False, indent=2) + "\n",
    }
    for name, content in text_files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return audit_share(root, output)


def audit_share(root: Path, output: Path):
    forbidden = [value.encode("utf-8") for value in private_values(root)]
    manifest = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        if ".git" in path.relative_to(output).parts or relative == "SHARE_MANIFEST.json":
            continue
        data = path.read_bytes()
        if any(value in data for value in forbidden):
            raise ValueError("隐私检查未通过，尚不可上传：" + path.relative_to(output).as_posix())
        if relative in ("config/setting.md", "config/deepseek_key.txt"):
            expected = SETTING_EXAMPLE.encode("utf-8") if relative.endswith("setting.md") else b""
            if data != expected:
                raise ValueError("分享目录的配置已被填写，不能再作为干净副本上传。")
            continue
        if any(relative.startswith(prefix) for prefix in ("memory/", "plans/", "media/drawings/")) and path.name != ".gitkeep":
            raise ValueError("分享目录出现了运行数据，尚不可上传：" + relative)
        manifest[path.relative_to(output).as_posix()] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (output / "SHARE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
    return {"files": len(manifest) + 1, "bytes": sum(item["bytes"] for item in manifest.values()),
            "private_values_checked": len(forbidden)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tem")
    args = parser.parse_args()
    print(json.dumps(prepare_share(ROOT, args.output), ensure_ascii=False))
