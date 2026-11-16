# Xigpet 外部工具接口（API v1）

把一个完整工具目录放到程序旁边的 `tool/` 下，下一轮对话就会发现它。不需要为这个工具修改主程序、重新打包或启动 MCP 服务。

这是**参考 MCP 工具声明的本地 Python 接口**，使用 `name`、`description`、`inputSchema`，不实现 MCP 的 JSON-RPC/stdio/HTTP 传输协议。主程序负责注册、参数校验、调用和图片发送。

**适用前提：主程序必须已经包含这套加载器。此前的旧 EXE 没有加载接口，仅复制目录不会生效。当前可通过“启动聊天宠物.bat”运行最新源码；将来换成带加载器的 EXE 后，再新增工具就不必重新打包。**

## 1. 安装、移除、禁用

```text
程序目录/
├─ main.py 或支持接口的 Xigpet.exe
├─ tool/
│  ├─ README.md
│  ├─ fortune/
│  │  ├─ manifest.json
│  │  ├─ plugin.py
│  │  ├─ engine.py
│  │  └─ resource/       # 抽签图片、字体、签文
│  └─ hello/
│     ├─ manifest.json
│     └─ plugin.py
├─ config/
├─ memory/
└─ media/
```

- 安装：复制完整目录，必须包含 `manifest.json` 和 Python 入口。
- 禁用：将清单的 `enabled` 改为 `false`。
- 移除：把整个工具目录移到 `tool/` 外，或改名为 `_fortune` 这类以下划线开头的名字。
- 新增、移除、禁用及代码修改在**下一轮对话**生效；正在进行的调用请先等它结束再改文件。
- 快捷按钮约两秒刷新；最多显示前四个，其他工具仍能通过对话调用。
- 移除插件不会删除历史聊天、生成图片或私有状态，重新安装仍可恢复使用。主程序自动更新不会覆盖、安装或删除外部工具。
- 只识别目录名为小写字母开头、后接小写字母/数字/下划线的工具目录（最多32字符）。点号或下划线开头的目录会忽略。

## 2. 最小可用示例

创建 `tool/hello/manifest.json`：

```json
{
  "api_version": 1,
  "id": "hello",
  "name": "打招呼",
  "version": "1.0.0",
  "enabled": true,
  "entrypoint": "plugin.py",
  "requires": [],
  "instructions": "用户要求打招呼时可以调用hello_greet，根据真实返回自然回复。",
  "quick_actions": [
    {"label": "打招呼", "message": "用打招呼工具向我问好"}
  ],
  "tools": [
    {
      "name": "hello_greet",
      "description": "向指定名字问好，返回一段问候文字。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "name": {"type": "string", "minLength": 1, "maxLength": 30}
        },
        "required": ["name"],
        "additionalProperties": false
      },
      "when": {
        "user_only": true,
        "keywords": ["问好", "打招呼"]
      }
    }
  ]
}
```

创建 `tool/hello/plugin.py`：

```python
def invoke(name, arguments, context):
    if name == "hello_greet":
        return {"text": f"你好，{arguments['name']}！"}
    return {"error": "没有这个工具。"}
```

然后对宠物说“用打招呼工具向小明问好”。工具返回值会交给模型，模型再自然回复。

## 3. manifest.json 字段

| 字段 | 含义 |
| --- | --- |
| `api_version` | 必须为整数 `1` |
| `id` | 必须与目录名相同，安装后保持稳定 |
| `name`、`version` | 工具包的可读名称和版本信息 |
| `enabled` | 可选，默认 `true` |
| `entrypoint` | 工具根目录中的 Python 文件，默认 `plugin.py` |
| `requires` | 必须已存在的顶层 Python 模块名，如 `PIL`；检查失败不注册，不自动安装 |
| `instructions` | 供模型参考的使用说明，最多4000字；与工具一起放在外部目录 |
| `quick_actions` | 可选按钮列表，每个包含 `label`、`message`；按钮只是发送普通用户请求 |
| `tools` | 1～20个工具定义；名称不能覆盖内置或其他插件工具 |

每个工具必填 `name`、`description`、`inputSchema`。名称使用 ASCII 字母/数字/下划线，以字母或下划线开头，最多64字符；建议使用 `插件id_动作`。

`when` 控制本轮能否调用：

- `user_only` 默认 `true`，主动聊天和计划任务不能调用；明确设为 `false` 才允许这些场景。
- `keywords` 默认空列表；非空时，当前真实用户消息须包含至少一个关键词。这里是普通字符串，不是正则表达式，也不读取旧聊天或网页作为触发指令。
- `force_on` 可选短命令列表，当前用户消息与其中一项完全匹配时，程序要求模型调用此工具，如抽签插件的“抽签”。仍须满足本轮调用条件。避免不同工具使用相同短命令。

没有合适关键词限制时，可省略 `keywords`，由模型根据描述选择工具。清单每个最多64KB；主程序和插件总工具数最多128，冲突或无效插件不影响其他插件。

### 支持的 inputSchema

根类型必须是 `object`。对象属性也须设置 `additionalProperties: false`，省略时加载器会补上。

当前支持：`type`（object/array/string/integer/number/boolean/null）、`properties`、`required`、`additionalProperties:false`、`items`、`enum`、`minLength/maxLength`、`minimum/maximum`、`minItems/maxItems`，以及说明字段 `description/title/default`。

不支持 `$ref`、`oneOf`、`pattern` 等完整 JSON Schema 功能，出现不支持的字段会拒绝注册。`default` 只是说明，**不会自动填入参数**，请在 Python 入口使用 `arguments.get()` 处理默认值。参数在执行前本地校验，错误类型、未知字段和缺少必填项不会进入插件。

## 4. Python 调用约定与 context

必须提供同步函数：

```python
def invoke(name: str, arguments: dict, context) -> dict:
    ...
```

一个入口可以实现清单中的多个工具。其他源码放在同目录，使用相对导入：`from .engine import MyTool`。主程序不会把工具目录加入全局 `sys.path`。入口和相对导入模块按当前对话加载，对话结束释放；同一个插件的调用串行处理，避免同时改坏共享记录。

`context` 提供以下 `pathlib.Path` 路径，不直接提供 API Key：

| 属性 | 位置/用途 |
| --- | --- |
| `root` | 程序目录，EXE 模式也是 EXE 所在目录 |
| `tool_dir` | 当前工具目录，读取同目录资源用它 |
| `plugin_id` | 当前插件 id |
| `state_dir` | `memory/tools/<id>/`，保存私有状态 |
| `config_dir` | `config/tools/<id>/`，保存私有配置，不放到插件清单中 |
| `output_dir` | `media/tools/<id>/`，生成图片建议放这里 |
| `media_dir` | 程序的 `media/` 根目录 |
| `memory_dir`、`config_root` | 兼容已有功能数据时使用的根目录 |
| `source` | 本轮来源，如 `user`、主动聊天等 |

需要写文件时自行 `mkdir(parents=True, exist_ok=True)`，不要依赖工作目录，不要把私有记录写进工具安装目录。网络请求须设置超时；进程内接口无法安全强制终止死循环或无超时的第三方调用。不要让工具自行启动常驻线程或服务。

## 5. 返回文字、数据和图片

返回值必须是 JSON 可序列化的字典，总计最多32000字；不能返回 Python 对象、文件字节或 Base64 大图片。普通数据可自由使用字段，如 `text`、`items`、`result`，由模型读取。

失败使用：`{"error": "用户看得懂的具体原因"}`。未知异常由主程序转成错误，不把调用栈或异常里的潜在密钥发给模型。

图片示例：

```python
from PIL import Image

def invoke(name, arguments, context):
    context.output_dir.mkdir(parents=True, exist_ok=True)
    output = context.output_dir / "sample.png"
    image = Image.new("RGB", (320, 180), "lightblue")
    try:
        image.save(output)
    finally:
        image.close()
    return {
        "text": "图片已生成。",
        "media": [f"tools/{context.plugin_id}/sample.png"]
    }
```

`media` 是相对于 `media/` 的图片路径列表，最多三项。支持 PNG/JPG/JPEG/GIF/WebP，每张最多20MB；`tools/<id>/文件名` 是通用插件输出目录，兼容现有的 `fortunes/`、`drawings/` 和根目录图片。会检查路径、实际文件存在与目录链接。不要自行拼接聊天附件标记或直接连接 QQ，主程序负责聊天展示和已配置的 QQ 同步。

当前 v1 的 `media` 输出仍只接受图片，不要用它返回音频、视频或文档。主程序另提供MP3语音接口，未来的音频插件可在 `invoke` 中调用：

```python
# 下载公开MP3并排队，主程序按QQ语音发送。
return context.download_voice("https://example.com/voice.mp3", label="招呼语音")

# 或将已有下载缓存中的MP3按语音排队。
return context.send_voice(file_id)
```

以上只是接口示例，不会安装下载插件。`file_id` 必须来自主程序下载缓存，不是路径、网址或任意文件名。下载入口复用主程序的公网地址、重定向、真实类型、80MB音频上限、磁盘空间及取消校验；语音入口仅支持真实MP3，发送前再次校验。`send_voice` 可把本轮已下载、尚未发出的同一附件切换为语音，只保留一次发送；发送模式是本轮消息属性，不写入缓存元数据。想发送普通音频文件时，现有主程序 `download_file` / `send_download` 使用 `send_as="file"`（默认），想发语音使用 `send_as="voice"`。

SDK调用返回 `file_id`、`send_as="voice"`、大小等JSON信息，同时已经在当前资源任务中排队。不要拼接内部附件标记、再调用一次 `send_voice` 或自行连接QQ；主程序负责聊天附件卡片和QQ同步。成功排队不等于QQ确认送达，发送失败会显示QQ错误，不会自动换成文件或重试，以免重复发送。QQ仅支持同机OneBot读取缓存路径；Docker或其他设备的SnowLuma未挂载同一路径时不能直接读取。SnowLuma收到 `record` 消息段后负责实际语音编码/上传，具体MP3兼容性取决于服务版本；主程序不额外安装转码器、TTS或常驻服务。宠物自主发语音与图片共用单轮一条/张及现有冷却机制；明确只要链接时不会下载或排队。

工具自行生成文件的保留与缓存清理由工具自己实现；主程序下载的语音沿用下载缓存保留策略，卸载插件不自动删除私有数据。

## 6. 依赖、信任及排错

- 插件 Python 与主程序运行在同一进程，**不是沙箱**。只安装可信代码；模型不会因为读到网页指令就安装工具。
- EXE 自带哪些第三方库，插件就可以直接使用哪些库。`requires` 不会执行 pip。新依赖可在源码环境手工安装；纯 Python 依赖也可以放在插件的 `vendor/` 包中，通过相对导入使用。新的原生扩展须匹配 Python 版本和系统，不能保证任意依赖都能直接放进旧 EXE。
- 这是插件扩展接口，不承诺可随意替换主程序、底层 SDK 或更换协议版本；新增主程序能力仍需要更新主程序一次。
- 在项目目录执行 `python -m xigpet.tool_plugins`，可查看已发现工具、缺失依赖、清单格式错误或名称冲突。该命令只检查声明，不执行工具，不调用模型。
- 本仓库抽签示例在 `fortune/`，可以直接阅读 `manifest.json` 和 `plugin.py`。分享脚本只收录这个公开示例，不自动复制用户自己安装的插件或私有配置。
