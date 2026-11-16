"""Create a clean source distribution from an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from xigpet.chat_style import DEFAULT_CONFIG as CHAT_STYLE_DEFAULTS
from xigpet.self_growth import DEFAULT_CONFIG as GROWTH_DEFAULTS
from scripts.deepseek_web import DEFAULT_CONFIG as WEB_DEFAULTS
from scripts.bangumi_tools import DEFAULT_CONFIG as BANGUMI_DEFAULTS


ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = ("main.py", "requirements.txt", ".editorconfig", "Xig.png", "VERSION.json",
             "启动聊天宠物.bat", "打包EXE.bat", "packaging/Xigpet.spec", "THIRD_PARTY_NOTICES.md", "宠物定制指南.md", "tool/README.md")
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
像熟人发消息一样，一条只说一句，可以自然连发几条短消息，不固定条数；复杂话题按需要完整展开。
情境合适时偶尔使用表情，也可以给自己的画命名并在以后再次分享。
需要旧事实时先检索记忆，不编造经历。用户本轮明确要求截图时程序会直接附图，收到后直接分析；主动请求查看屏幕仍须等待用户确认。
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
config/mood.json
config/memory.json
config/chat_style.json
config/growth.json
config/web.json
config/bangumi.json
config/fortune.json
config/emoticons.json
config/images.json
config/tools/
media/tools/
media/fortunes/
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

想设置专属宠物的名字、人设、头像、表情和 GIF，请阅读 [宠物定制指南](宠物定制指南.md)。

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

- 浅蓝聊天界面、图片发送与理解、平滑滚动，主页最多显示 50 条，完整历史按日期查询。普通聊天一句一个气泡，可自然连发几条，主动联系及查资料后的回复也逐句发送；桌面与QQ共用发送节奏，代码块等指定格式保留完整。
- 人设 Markdown、重要记忆、完整聊天和带原文位置的记忆目录；模型按需要检索旧事。
- 外部工具接口：将工具目录放入 `tool/`，用 `manifest.json` 声明函数与参数、`plugin.py` 实现调用；下一轮自动发现，无需为新增工具重新打包。格式、依赖和图片返回约定见 `tool/README.md`。
- 抽签已剥离为 `tool/fortune/` 插件，保留12个主题和396张签底，桌面与QQ共用每日结果。通过对话请求抽签，不提供快捷按钮；移除或禁用后不提供该功能；抽签代码和资源不再嵌入EXE。
- 受限浏览与下载：优先HTTP流式下载，动态页面才启动独立临时Edge；30秒空闲自动关闭，聊天页可取消。支持限量图片、音频、普通视频和文档，附件可保存与QQ同步；自主表情不限画风，保留冷却和单次一张限制。需要按requirements安装Playwright等依赖，无需另装Chromium，具体边界见宠物定制指南。
- 网页图片下载：`download_image`从百度/Bing/Google图片搜索或指定公开网页（含贴吧帖子）提取图片并下载发送，支持原图、预览图及懒加载地址。已有本地表情可复用；取图不限画风，原图及预览图下载失败时尝试截取已加载的图片元素发送（静态PNG）。旧固定表情API已移除，可通过`config/images.json`关闭联网取图。
- Bangumi公开动漫资料：搜索作品、角色、声优/制作人物，查询简介、日期、集数、评分和前后作、staff与对应版本的配音关联。默认开启，无需额外Key；每轮最多6次请求、缓存5分钟，附来源链接，详见宠物定制指南。
- 自我成长：宠物可在兴趣、表达习惯、小目标、自我描述四项内维护自己的补充设定；聊天页可锁定、编辑、关闭自主修改及恢复历史版本。默认间隔6小时、每天最多3次，主人设始终优先。符合编辑条件时每6轮交流在后台自动回顾一次（至少间隔30分钟），有原文依据才更新，额外使用当前模型额度；自我成长页显示回顾状态。
- DeepSeek官方Search与Fetch复用当前Key，按需先搜索再读网页正文，保留来源与日期。每轮默认最多4次网络请求，重复请求及同篇正文翻页复用缓存；官方工具另按成功请求计费，可在config/web.json关闭或调整预算，详见宠物定制指南。
- 自然聊天风格：按人设接话，减少客服套话、重复开场和例行追问；少量可选口语素材避免近期重复，主动联系接续真实话题。默认开启，不增加模型调用次数；可复制 `config/chat_style.example.json` 为 `config/chat_style.json` 调整或关闭，详见宠物定制指南。
- 全文记忆检索覆盖完整原文，支持中英文常用同义词、轻微拼写错误和角色/日期筛选。旧对话可在后台整理为带来源的摘要，按引用还原原文；输入上下文有估算预算，完整工具组可折叠读回，原始聊天不会改写。通过聊天页、桌宠右键或托盘的“记忆与检索”查看。后台摘要达到门槛才额外调用当前模型，可在设置中关闭；本地检索不调用模型。私有数据位于 `config/memory.json` 和 `memory/context.sqlite3`，不进入分享与更新文件。
- 最小化进入置顶桌宠，支持拖动、大小设置、放松/漂浮/玩耍动画和托盘驻留。可以单独关闭桌宠，保留后台聊天与 QQ 功能。
- 每日生成共用主动联系计划，按可行时段上午、下午、晚上各两次；另可自主安排每天最多 3 次画画，题材由宠物结合性格、记忆和兴趣决定。软件运行时执行，作品保存到聊天记录与画作库，不弹绘画提示气泡。
- 时间、百科/萌娘百科、新闻、媒体检索、受限 Python/Pillow 绘画、笔记和截图请求工具。
- 绘画支持贝塞尔曲线、透明图层、渐变和 2 倍抗锯齿，作品保存到 `media/drawings/`，记录名称与标签，之后可检索并再次发送。
- 新作品同时保存 `.drawing.json` 图形源文件，可通过 `read_drawing` / `edit_drawing` 修改局部、颜色和图层；修改生成新版本并保留原作。旧 PNG 仍可发送，没有源文件的旧作无法直接局部编辑。备份作品时请保留 PNG 与同名源文件。
- 心情系统包含总心情、七种情绪、自然回稳和最近60条变化记录，可从聊天页、桌宠右键和托盘打开。反应强度默认150%，可在设置中调整；当前情绪持续影响后续对话的措辞、语气和接话积极程度，每日带入次数只限制变化原因，不限制当前心情。默认每轮结束后额外调用一次当前模型判断，使用你的 API 额度；在后台执行，不阻塞回复，可在心情设置中关闭。设置和状态分别保存在私有的 `config/mood.json`、`memory/mood.json`，不会进入分享或更新文件。
- 闲置提醒仅显示桌宠气泡，不加入聊天或转发 QQ。用户在本轮聊天或已配置QQ私聊中明确要求截图/查看屏幕时直接截图一次；宠物主动请求仍需本机按钮确认。截图成功后才进入聊天记录和模型输入。

自主绘画作为 `plans/plan.md` 中带 `[自主画画]` 标记的独立项目，计划器每天选择安排 0～3 次，不再由随机闲置气泡触发。每项仅创作一幅；开始前持久化计数，失败或重启不自动重试，过期项目不集中补画。普通主动联系不临时调用绘画工具；你在正常聊天中主动要求画画仍可使用原有绘画功能。计划创作需要 API Key，使用当前模型额度；启用 QQ 接入时，自主画作和说明会同步发送到当前 QQ 联系人。升级旧计划时保留已有提醒和已发送记录。
- 可选 SnowLuma OneBot v11 QQ 私聊文字/图片双向同步，以及通过`record`消息段发送MP3语音（`send_as="voice"`）；普通音频仍可按文件发送。SnowLuma 不包含在本项目中，需自行安装、启动并登录宠物 QQ。右键桌宠或托盘打开 QQ 设置，读取自己的 `onebot_账号.json`，填写用户 QQ，保存并启用。默认本机地址 `ws://127.0.0.1:3001/`，需要开启正向 WebSocket；Token 使用当前服务配置，不是 DeepSeek Key。

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


def example_tool_file(name):
    """Public fortune example assets, separate from host updater permissions."""
    from pathlib import PurePosixPath
    if "\\" in name or ":" in name or any(p in ("", ".", "..") for p in name.split("/")):
        return False
    parts=PurePosixPath(name).parts
    if parts[:2] != ("tool","fortune"):
        return False
    if len(parts)==3:
        return parts[2] in {"engine.py","plugin.py","manifest.json","README.md","LICENSE"}
    if parts[2:3] != ("resource",):
        return False
    if len(parts)==4:
        return parts[3] in {"themes.json","specific_rules.json"}
    if len(parts)==5:
        return (parts[3]=="fortune" and parts[4]=="copywriting.json") or (parts[3]=="font" and parts[4] in {"Mamelon.otf","sakura.ttf"})
    if len(parts)==6 and parts[3]=="img":
        import re
        return bool(re.fullmatch(r"[a-z_]+",parts[4]) and re.fullmatch(r"[\w\u4e00-\u9fff .-]+\.(?:jpg|jpeg|png|webp)",parts[5],re.I))
    return False


def prepare_share(root: Path, output: Path):
    root, output = root.resolve(), output.resolve()
    if not output.is_relative_to(root) or output == root:
        raise ValueError("分享目录必须位于项目内部，且不能是项目根目录。")
    if output.exists():
        raise FileExistsError("分享目录已存在；请指定一个新目录，避免覆盖。")
    files = list(TOP_FILES)
    # Ship only this public example, never arbitrary user-installed tools.
    files.extend(path.relative_to(root).as_posix() for path in (root / "tool/fortune").rglob("*")
                 if path.is_file() and example_tool_file(path.relative_to(root).as_posix()))
    for folder in ("xigpet", "scripts", "tests"):
        files.extend(path.relative_to(root).as_posix() for path in (root / folder).glob("*.py"))
    files.extend("animation/" + name + ".gif" for name in ("relax", "float", "play"))
    files.extend("media/" + name for name in MEDIA)
    for name in files:
        source = root / name
        if not source.is_file() or source.is_symlink():
            raise ValueError("分享所需文件缺失或是符号链接：" + name)
    output.mkdir(parents=True)
    for name in sorted(set(files)):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    text_files = {
        "README.md": README, ".gitignore": GITIGNORE,
        ".gitattributes": "*.bat -text whitespace=cr-at-eol\n",
        "config/setting.example.md": SETTING_EXAMPLE,
        "config/chat_style.example.json": json.dumps(CHAT_STYLE_DEFAULTS, ensure_ascii=False, indent=2) + "\n",
        "config/growth.example.json": json.dumps(GROWTH_DEFAULTS, ensure_ascii=False, indent=2) + "\n",
        "config/web.example.json": json.dumps(WEB_DEFAULTS, ensure_ascii=False, indent=2) + "\n",
        "config/bangumi.example.json": json.dumps(BANGUMI_DEFAULTS, ensure_ascii=False, indent=2) + "\n",
        "config/images.example.json": '{"enabled": true}\n',
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
        if any(relative.startswith(prefix) for prefix in ("memory/", "plans/", "media/drawings/", "media/tools/")) and path.name != ".gitkeep":
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
