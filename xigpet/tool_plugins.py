"""Local drop-in tools, with MCP-shaped definitions and per-turn Python modules.

Installed Python is trusted local code, not sandboxed code. No service or pip is
started. Modules are loaded only on invocation and released after the turn.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import importlib.abc
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
import types
import uuid
import weakref

from scripts.media_tools import media_path

ACTIVE = ContextVar("xigpet_external_tools", default=None)
_LOCK = threading.RLock()
_ROOTS = {}
_CALL_LOCKS = weakref.WeakValueDictionary()
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class ToolPluginError(ValueError):
    pass


def safe_file(root, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative or relative.startswith("/") or any(p in ("", ".", "..") for p in relative.split("/")):
        raise ToolPluginError("工具文件路径无效。")
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ToolPluginError("工具文件不能通过链接访问目录之外的位置。")
    return path


class FreshLoader(importlib.abc.Loader):
    def __init__(self, path):
        self.path = path
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        if self.path.stat().st_size > 2 * 1024 * 1024:
            raise ToolPluginError("单个工具代码文件超过2MB。")
        module.__file__ = str(self.path)
        exec(compile(self.path.read_bytes(), str(self.path), "exec"), module.__dict__)


class ToolFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        namespace, _, suffix = fullname.partition(".")
        with _LOCK:
            root = _ROOTS.get(namespace)
        if root is None or not suffix:
            return None
        parts = suffix.split(".")
        if not all(NAME.fullmatch(part) for part in parts):
            raise ImportError("Invalid local tool module")
        package = safe_file(root, "/".join(parts))
        source = safe_file(root, "/".join(parts) + ".py")
        if package.is_dir():
            init = safe_file(root, "/".join(parts) + "/__init__.py")
            spec = importlib.util.spec_from_loader(fullname, FreshLoader(init) if init.is_file() else None, is_package=True)
            spec.submodule_search_locations = [str(package)]
            return spec
        if source.is_file():
            return importlib.util.spec_from_loader(fullname, FreshLoader(source))
        raise ModuleNotFoundError("工具内缺少模块：" + suffix, name=suffix)


_FINDER = ToolFinder()
sys.meta_path.insert(0, _FINDER)


def check_schema(schema, depth=0):
    if depth > 8 or not isinstance(schema, dict):
        raise ToolPluginError("inputSchema无效或嵌套过深。")
    supported = {"type", "description", "title", "default", "enum", "properties", "required", "additionalProperties", "items",
                 "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"}
    if set(schema) - supported:
        raise ToolPluginError("inputSchema包含当前接口不支持的字段。")
    if schema.get("type") not in ("object", "array", "string", "integer", "number", "boolean", "null"):
        raise ToolPluginError("inputSchema必须声明受支持的type。")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 100):
        raise ToolPluginError("enum须为1～100项列表。")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or not 0 <= schema[key] <= 8192):
            raise ToolPluginError(key + "须为0～8192的整数。")
    for key in ("minimum", "maximum"):
        if key in schema and type(schema[key]) not in (int, float):
            raise ToolPluginError(key + "须为数字。")
    if schema["type"] == "object":
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(props, dict) or len(props) > 64 or not isinstance(required, list) or any(not isinstance(k,str) or k not in props for k in required):
            raise ToolPluginError("对象properties/required无效。")
        if schema.get("additionalProperties", False) is not False:
            raise ToolPluginError("对象必须使用additionalProperties=false。")
        schema["additionalProperties"] = False
        for key, child in props.items():
            if not NAME.fullmatch(key):
                raise ToolPluginError("参数名无效。")
            check_schema(child, depth+1)
    elif schema["type"] == "array":
        check_schema(schema.get("items"), depth+1)


def validate(value, schema, path="参数"):
    kind = schema["type"]
    good = {"object": isinstance(value,dict), "array": isinstance(value,list), "string": isinstance(value,str),
            "integer": type(value) is int, "number": type(value) in (int,float), "boolean": type(value) is bool, "null": value is None}[kind]
    if not good or ("enum" in schema and not any(value == item and isinstance(value,bool) == isinstance(item,bool) for item in schema["enum"])):
        raise ToolPluginError(path + "类型或取值无效。")
    if kind == "object":
        props = schema.get("properties", {})
        if set(value) - set(props) or any(key not in value for key in schema.get("required", [])):
            raise ToolPluginError(path + "缺少必填项或包含未声明项。")
        for key, child in value.items():
            validate(child, props[key], path+"."+key)
    elif kind in ("string", "array"):
        lo, hi = ("minLength", "maxLength") if kind == "string" else ("minItems", "maxItems")
        if not schema.get(lo,0) <= len(value) <= schema.get(hi,8192 if kind=="string" else 64):
            raise ToolPluginError(path + "长度超出限制。")
        if kind == "array":
            for item in value: validate(item, schema["items"], path+"[]")
    elif kind in ("number", "integer") and not schema.get("minimum",float("-inf")) <= value <= schema.get("maximum",float("inf")):
        raise ToolPluginError(path + "数值超出限制。")


@dataclass(frozen=True)
class ToolContext:
    root: Path
    tool_dir: Path
    plugin_id: str
    memory_dir: Path
    config_root: Path
    media_dir: Path
    source: str

    @property
    def state_dir(self):
        return self.memory_dir / "tools" / self.plugin_id

    @property
    def config_dir(self):
        return self.config_root / "tools" / self.plugin_id

    @property
    def output_dir(self):
        return self.media_dir / "tools" / self.plugin_id

    def send_voice(self, file_id):
        """Queue a verified cached MP3; the host owns attachment/QQ delivery."""
        from scripts.browser_tasks import current_task
        return current_task().run("send_download", {"file_id":file_id, "send_as":"voice"})

    def download_voice(self, url, label=None):
        """Use the host's bounded public downloader, then queue a QQ voice."""
        from scripts.browser_tasks import current_task
        args = {"url":url, "kind":"audio", "send_as":"voice"}
        if label is not None:
            args["label"] = label
        return current_task().run("download_file", args)


class ToolRegistry:
    def __init__(self, root, builtin_names=(), *, memory_dir=None, config_root=None, media_dir=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "tool"
        self.builtin_names = set(builtin_names)
        self.memory_dir = Path(memory_dir) if memory_dir is not None else self.root / "memory"
        self.config_root = Path(config_root) if config_root is not None else self.root / "config"
        self.media_dir = Path(media_dir) if media_dir is not None else self.root / "media"

    def scan(self):
        plugins, status, names = [], [], set(self.builtin_names)
        if not self.directory.exists(): return plugins, status
        try:
            if self.directory.resolve() != self.directory.absolute():
                raise ToolPluginError("tool目录不能重定向到其他位置。")
            folders = sorted(self.directory.iterdir())[:128]
        except (OSError,ValueError,RuntimeError):
            return [], [{"id":"tool","error":"tool目录无法读取或包含无效链接。"}]
        for folder in folders:
            if folder.name.startswith((".","_")): continue
            try:
                if not folder.is_dir(): continue
                if not ID.fullmatch(folder.name) or folder.resolve() != folder.absolute():
                    raise ToolPluginError("工具目录名或链接无效。")
                manifest = safe_file(folder,"manifest.json")
                if not manifest.is_file(): continue
                if manifest.stat().st_size > 65536: raise ToolPluginError("manifest.json超过64KB。")
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
                json.dumps(data,allow_nan=False)
                if not isinstance(data,dict) or type(data.get("api_version")) is not int or data.get("api_version") != 1 or data.get("id") != folder.name:
                    raise ToolPluginError("api_version必须为1，id必须与目录名一致。")
                if type(data.get("enabled",True)) is not bool: raise ToolPluginError("enabled须为布尔值。")
                if not data.get("enabled",True):
                    status.append({"id":folder.name,"enabled":False});continue
                entry = safe_file(folder,data.get("entrypoint","plugin.py"))
                if entry.suffix != ".py" or entry.parent != folder or not entry.is_file(): raise ToolPluginError("Python入口须为工具根目录下存在的.py文件。")
                requires=data.get("requires",[])
                if not isinstance(requires,list) or len(requires)>20: raise ToolPluginError("requires无效。")
                for dependency in requires:
                    if not isinstance(dependency,str) or not NAME.fullmatch(dependency): raise ToolPluginError("requires只支持顶层Python模块名。")
                    if importlib.util.find_spec(dependency) is None: raise ToolPluginError("缺少Python依赖："+dependency)
                tools=data.get("tools")
                if not isinstance(tools,list) or not 1<=len(tools)<=20: raise ToolPluginError("每个插件须声明1～20个工具。")
                local=set()
                for tool in tools:
                    if not isinstance(tool,dict) or not isinstance(tool.get("name"),str) or not NAME.fullmatch(tool["name"]): raise ToolPluginError("工具name无效。")
                    if tool["name"] in names or tool["name"] in local: raise ToolPluginError("工具名称与内置或其他工具冲突。")
                    if not isinstance(tool.get("description"),str) or not 1<=len(tool["description"])<=1000: raise ToolPluginError("工具description无效。")
                    schema=tool.get("inputSchema")
                    check_schema(schema)
                    if schema["type"]!="object": raise ToolPluginError("工具根参数必须为object。")
                    when=tool.get("when",{})
                    if not isinstance(when,dict) or type(when.get("user_only",True)) is not bool: raise ToolPluginError("when无效。")
                    for key in ("keywords","force_on"):
                        values=when.get(key,[])
                        if not isinstance(values,list) or len(values)>40 or any(not isinstance(v,str) or not 1<=len(v)<=80 for v in values): raise ToolPluginError("when关键词列表无效。")
                    local.add(tool["name"])
                if len(names)+len(local)>128: raise ToolPluginError("总工具数超过128。")
                instructions=data.get("instructions","")
                if not isinstance(instructions,str) or len(instructions)>4000: raise ToolPluginError("instructions最多4000字。")
                actions=data.get("quick_actions",[])
                if not isinstance(actions,list) or len(actions)>4 or any(not isinstance(a,dict) or not isinstance(a.get("label"),str) or not 1<=len(a["label"])<=12 or not isinstance(a.get("message"),str) or not 1<=len(a["message"])<=80 for a in actions): raise ToolPluginError("quick_actions无效。")
                names.update(local)
                plugins.append({"directory":folder,"entry":entry,"manifest":data})
                status.append({"id":folder.name,"enabled":True,"version":str(data.get("version",""))[:40],"tools":sorted(local)})
            except (OSError,ValueError,TypeError,ImportError,AttributeError,RecursionError) as exc:
                status.append({"id":folder.name,"error":str(exc) if isinstance(exc,ToolPluginError) else "工具清单无法读取或格式无效。"})
        return plugins, status

    def quick_actions(self):
        plugins,_=self.scan()
        return [dict(action) for p in plugins for action in p["manifest"].get("quick_actions",[])][:4]

    @contextmanager
    def turn(self, user_text="", source="user"):
        plugins,status=self.scan()
        turn=ToolTurn(self,plugins,status,user_text,source)
        token=ACTIVE.set(turn)
        try: yield turn
        finally:
            ACTIVE.reset(token)
            turn.close()


class ToolTurn:
    def __init__(self, registry, plugins, status, user_text, source):
        self.registry,self.status,self.source=registry,status,source
        self.all_names={t["name"] for p in plugins for t in p["manifest"]["tools"]}
        self.tools={}
        self.modules={}
        self.forced_tool=None
        self.instructions=[]
        for plugin in plugins:
            eligible=False
            for tool in plugin["manifest"]["tools"]:
                when=tool.get("when",{})
                if when.get("user_only",True) and source!="user": continue
                keywords=when.get("keywords",[])
                if keywords and not any(k.casefold() in user_text.casefold() for k in keywords): continue
                self.tools[tool["name"]]=(plugin,tool)
                eligible=True
                if user_text.strip().casefold() in [t.casefold() for t in when.get("force_on",[])]:
                    self.forced_tool=tool["name"]
            if eligible and plugin["manifest"].get("instructions"):
                self.instructions.append(plugin["manifest"]["instructions"])

    @property
    def definitions(self):
        return [{"type":"function","function":{"name":t["name"],"description":t["description"],"parameters":t["inputSchema"]}} for _,t in self.tools.values()]

    def invoke(self, name, arguments):
        if name not in self.tools:
            return json.dumps({"error":"该工具未安装、已禁用，或本轮请求不满足调用条件。"},ensure_ascii=False)
        key=str(self.tools[name][0]["directory"])
        with _LOCK:
            gate=_CALL_LOCKS.get(key)
            if gate is None:
                gate=threading.RLock()
                _CALL_LOCKS[key]=gate
        with gate:
            return self._invoke(name,arguments)

    def _invoke(self, name, arguments):
        try:
            if name not in self.tools: raise ToolPluginError("该工具未安装、已禁用，或本轮请求不满足调用条件。")
            plugin,definition=self.tools[name]
            validate(arguments,definition["inputSchema"])
            if len(json.dumps(arguments,allow_nan=False))>32768: raise ToolPluginError("工具参数过大。")
            folder=plugin["directory"]
            if not folder.is_dir() or not safe_file(folder,"manifest.json").is_file(): raise ToolPluginError("工具已被移除，请在下一轮重试。")
            pid=plugin["manifest"]["id"]
            if pid not in self.modules:
                namespace="_xigpet_plugin_"+pid+"_"+uuid.uuid4().hex
                with _LOCK:
                    _ROOTS[namespace]=folder
                spec=importlib.util.spec_from_loader(namespace,FreshLoader(plugin["entry"]),is_package=True)
                spec.submodule_search_locations=[str(folder)]
                module=importlib.util.module_from_spec(spec)
                self.modules[pid]=(namespace,module)
                sys.modules[namespace]=module
                spec.loader.exec_module(module)
            module=self.modules[pid][1]
            context=ToolContext(self.registry.root,folder,pid,self.registry.memory_dir,self.registry.config_root,self.registry.media_dir,self.source)
            result=module.invoke(name,arguments,context)
            if not isinstance(result,dict): raise ToolPluginError("工具须返回JSON对象。")
            media=result.get("media",[])
            if not isinstance(media,list) or len(media)>3: raise ToolPluginError("media须为最多3张图片的列表。")
            for filename in media: media_path(context.media_dir,filename)
            encoded=json.dumps(result,ensure_ascii=False,allow_nan=False)
            if len(encoded)>32000: raise ToolPluginError("工具返回内容超过32000字。")
            return encoded
        except ToolPluginError as exc:
            return json.dumps({"error":str(exc)},ensure_ascii=False)
        except ModuleNotFoundError as exc:
            missing=exc.name if isinstance(exc.name,str) and re.fullmatch(r"[\w.]+",exc.name) else "未知模块"
            return json.dumps({"error":"工具缺少Python依赖或模块："+missing},ensure_ascii=False)
        except (Exception,SystemExit) as exc:
            return json.dumps({"error":"外部工具执行失败（"+type(exc).__name__+"），请检查工具代码。"},ensure_ascii=False)

    def close(self):
        with _LOCK:
            for namespace,_ in self.modules.values():
                for name in tuple(sys.modules):
                    if name==namespace or name.startswith(namespace+"."):
                        sys.modules.pop(name,None)
                _ROOTS.pop(namespace,None)
        self.modules.clear()


def active_turn():
    return ACTIVE.get()


if __name__ == "__main__":
    root=Path(sys.executable).parent if getattr(sys,"frozen",False) else Path(__file__).resolve().parents[1]
    _,status=ToolRegistry(root).scan()
    print(json.dumps(status,ensure_ascii=False,indent=2))
