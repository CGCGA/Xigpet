# Xigpet · Windows 聊天桌宠

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

## 测试与再次分享

```powershell
python -B -m unittest discover -s tests -v
python -m scripts.prepare_share --output tem
```

分享工具按白名单复制源码和自带资源，重新生成通用模板，并排除私人文件。目标目录已存在时会停止，不会覆盖已有内容。`SHARE_MANIFEST.json` 记录分享文件的大小与 SHA-256，用于核对副本。
