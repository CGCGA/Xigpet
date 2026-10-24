"""A bounded interpreter for Python drawing statements; never executes Python bytecode."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import operator
import random
import re
import threading
import time
import uuid
from pathlib import Path

from PIL.PngImagePlugin import PngInfo

from .media_tools import register_drawing, media_path, is_drawing
from .drawing_help import DRAWING_HELP
from .drawing_scene import (DrawingError, MAX_NODES, MAX_LAYERS, MAX_SOURCE_BYTES,
                            new_scene, normalize_node, identifier, number, color,
                            validate_scene, render_scene, apply_edits)


_DRAWING_LOCK = threading.RLock()


class DrawingProgram:
    def __init__(self, width=768, height=768):
        if any(type(v) is not int or not 256 <= v <= 1024 for v in (width, height)):
            raise DrawingError("画布宽高须为 256–1024 的整数。")
        self.scene = new_scene(width, height)
        self.layer = "base"
        self._ids = set()
        self._source_bytes = 1024
        self._random = random.Random()
        self._background_set = False
        self.variables = {"W": width, "H": height, "pi": math.pi, "tau": math.tau}
        self.steps = self.draws = 0
        self.deadline = time.monotonic() + 8

    def _tick(self):
        self.steps += 1
        if self.steps > 30000 or time.monotonic() > self.deadline:
            raise DrawingError("绘图计算超过步数或 8 秒时限，请简化代码。")

    def _value(self, value, depth=0, budget=None):
        budget = [1000] if budget is None else budget
        budget[0] -= 1
        if depth > 4 or budget[0] < 0:
            raise DrawingError("数据嵌套过深或项目过多。")
        if type(value) not in (str, int, float, bool, type(None), list, tuple, range):
            raise DrawingError("不支持此数据类型。")
        if isinstance(value, (int, float)) and (not math.isfinite(value) or abs(value) > 1000000):
            raise DrawingError("绘图数值过大或不是有限数。")
        if isinstance(value, (list, tuple)) and len(value) > 1000:
            raise DrawingError("列表不能超过1000项。")
        if isinstance(value, str) and len(value) > 512:
            raise DrawingError("字符串不能超过512个字符。")
        if isinstance(value, (list, tuple)):
            for part in value:
                self._value(part, depth + 1, budget)
        return value

    def _assign(self, target, value):
        if isinstance(target, ast.Name) and not target.id.startswith("_") and target.id not in {"draw", "math", "random", "W", "H"}:
            self.variables[target.id] = self._value(value)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (tuple, list)) and len(target.elts) == len(value):
            for item, part in zip(target.elts, value):
                self._assign(item, part)
        else:
            raise DrawingError("只能赋值给普通变量或解包变量。")

    def _block(self, statements):
        for node in statements:
            self._tick()
            if isinstance(node, ast.Assign):
                value = self._expr(node.value)
                for target in node.targets:
                    self._assign(target, value)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                self._assign(node.target, self._binary(node.op, self._expr(node.target), self._expr(node.value)))
            elif isinstance(node, ast.Expr):
                self._expr(node.value)
            elif isinstance(node, ast.For):
                values = self._expr(node.iter)
                if not isinstance(values, (range, tuple, list)) or len(values) > 500:
                    raise DrawingError("for 每次只能遍历不超过500项的列表或 range。")
                for value in values:
                    self._assign(node.target, value)
                    self._block(node.body)
                self._block(node.orelse)
            elif isinstance(node, ast.If):
                self._block(node.body if self._expr(node.test) else node.orelse)
            else:
                raise DrawingError(f"不支持 {type(node).__name__}；仅允许绘图、赋值、for 和 if。")

    def _binary(self, op, left, right):
        if isinstance(op, ast.Mod) and not all(isinstance(value, (int, float)) for value in (left, right)):
            raise DrawingError("% 只支持数值取模，不支持字符串格式化。")
        if isinstance(op, ast.Mult):
            for sequence, count in ((left, right), (right, left)):
                if isinstance(sequence, (str, list, tuple)) and isinstance(count, int) and len(sequence) * max(0, count) > 512:
                    raise DrawingError("重复生成的序列过大。")
        if isinstance(op, ast.Pow) and (not isinstance(right, (int, float)) or abs(right) > 8):
            raise DrawingError("幂运算的指数不能超过8。")
        functions = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                     ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                     ast.Mod: operator.mod, ast.Pow: operator.pow}
        if type(op) not in functions:
            raise DrawingError("不支持此运算。")
        return self._value(functions[type(op)](left, right))

    def _expr(self, node):
        self._tick()
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
            return self._value(node.value)
        if isinstance(node, ast.Name) and node.id in self.variables:
            return self.variables[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self._expr(item) for item in node.elts]
            return self._value(tuple(values) if isinstance(node, ast.Tuple) else values)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "math" and node.attr in {"pi", "tau"}:
            return math.pi if node.attr == "pi" else math.tau
        if isinstance(node, ast.BinOp):
            return self._binary(node.op, self._expr(node.left), self._expr(node.right))
        if isinstance(node, ast.UnaryOp):
            value = self._expr(node.operand)
            functions = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
            if type(node.op) in functions:
                return self._value(functions[type(node.op)](value))
        if isinstance(node, ast.Compare):
            left = self._expr(node.left)
            functions = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                         ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
                         ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
            for op, expression in zip(node.ops, node.comparators):
                right = self._expr(expression)
                if type(op) not in functions or not functions[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            value = None
            for item in node.values:
                value = self._expr(item)
                if (isinstance(node.op, ast.And) and not value) or (isinstance(node.op, ast.Or) and value):
                    break
            return value
        if isinstance(node, ast.IfExp):
            return self._expr(node.body if self._expr(node.test) else node.orelse)
        if isinstance(node, ast.Subscript):
            value, index = self._expr(node.value), self._expr(node.slice)
            if isinstance(value, (list, tuple, str)) and type(index) is int:
                return value[index]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for item in node.values:
                if isinstance(item, ast.Constant):
                    parts.append(item.value)
                elif isinstance(item, ast.FormattedValue) and item.format_spec is None:
                    parts.append(str(self._expr(item.value)))
                else:
                    raise DrawingError("f-string 不支持格式说明符。")
            return self._value("".join(parts))
        if isinstance(node, ast.Call):
            args = [self._expr(arg) for arg in node.args]
            if any(kw.arg is None for kw in node.keywords):
                raise DrawingError("不支持参数解包。")
            kwargs = {kw.arg: self._expr(kw.value) for kw in node.keywords}
            return self._call(node.func, args, kwargs)
        raise DrawingError(f"不支持表达式 {type(node).__name__}。")

    def _call(self, node, args, kwargs):
        functions = {"min": min, "max": max, "abs": abs, "round": round, "int": int,
                     "float": float, "str": str, "len": len, "sin": math.sin,
                     "cos": math.cos, "sqrt": math.sqrt, "radians": math.radians,
                     "uniform": self._random.uniform, "randint": self._random.randint}
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner, name = node.value.id, node.attr
            if owner == "draw":
                return self._primitive(name, args, kwargs)
            if owner == "math" and name in {"sin", "cos", "sqrt", "radians"}:
                return self._value(functions[name](*args, **kwargs))
            if owner == "random" and name in {"uniform", "randint"}:
                return self._value(functions[name](*args, **kwargs))
            if name == "append" and owner in self.variables and isinstance(self.variables[owner], list) and len(args) == 1 and not kwargs:
                if len(self.variables[owner]) >= 1000 or args[0] is self.variables[owner]:
                    raise DrawingError("列表过大或循环引用。")
                self.variables[owner].append(args[0])
                self._value(self.variables[owner])
                return None
        if isinstance(node, ast.Name):
            if node.id == "background" and len(args) == 1 and not kwargs:
                self.scene["background"] = list(color(args[0]))
                self._background_set = True
                return None
            if node.id == "layer" and len(args) == 1 and not set(kwargs) - {"opacity"}:
                name = identifier(args[0])
                existing = next((item for item in self.scene["layers"] if item["id"] == name), None)
                if existing is None:
                    if len(self.scene["layers"]) >= MAX_LAYERS:
                        raise DrawingError("最多16个图层。")
                    existing = {"id": name, "opacity": 1, "visible": True}
                    self.scene["layers"].append(existing)
                if "opacity" in kwargs:
                    existing["opacity"] = number(kwargs["opacity"], 0, 1)
                self.layer = name
                return None
            if node.id == "end_layer" and not args and not kwargs:
                self.layer = "base"
                return None
            if node.id == "range" and not kwargs and all(type(arg) is int for arg in args):
                value = range(*args)
                if len(value) > 500:
                    raise DrawingError("range 不能超过500项。")
                return value
            if node.id in functions:
                return self._value(functions[node.id](*args, **kwargs))
        raise DrawingError("只能调用文档中列出的绘图或数学函数。")

    def _primitive(self, name, args, kwargs):
        self.draws += 1
        if self.draws > MAX_NODES:
            raise DrawingError("最多2500个图形。")
        node_id = kwargs.pop("id", None)
        if node_id is None:
            node_id = f"shape_{self.draws}"
            while node_id in self._ids:
                node_id += "_"
        identifier(node_id)
        if node_id in self._ids:
            raise DrawingError("图形 ID 重复：" + node_id)
        node = normalize_node(name, args, kwargs, node_id, self.layer)
        self._source_bytes += len(json.dumps(node, ensure_ascii=False).encode("utf-8"))
        if self._source_bytes > MAX_SOURCE_BYTES:
            raise DrawingError("绘画源数据超过2 MiB，请减少图形或路径点。")
        self.scene["nodes"].append(node)
        self._ids.add(node_id)
        return None

    def run(self, code, *, render=True):
        if not isinstance(code, str) or not code.strip() or len(code) > 16000:
            raise DrawingError("绘图代码须为1–16000个字符。")
        code = re.sub(r"^```(?:python)?\s*\n|\n```\s*$", "", code.strip())
        try:
            tree = ast.parse(code)
            if sum(1 for _ in ast.walk(tree)) > 3000:
                raise DrawingError("绘图代码结构过大。")
            self._block(tree.body)
            if not self.draws and not self._background_set:
                raise DrawingError("代码没有画任何内容。")
            return render_scene(self.scene) if render else self.scene
        except DrawingError:
            raise
        except (ValueError, TypeError, SyntaxError, ArithmeticError, IndexError, RecursionError) as exc:
            raise DrawingError(f"绘图代码错误：{str(exc)[:250]}") from exc


def _save_artwork(folder, scene, *, title, keywords="", mood="", parent=None):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise DrawingError("请提供1–100字的绘画标题。")
    scene = validate_scene(scene)
    folder = Path(folder)
    output = folder / "drawings"
    if folder.is_symlink() or output.is_symlink():
        raise DrawingError("绘画输出目录不能是符号链接。")
    output.mkdir(parents=True, exist_ok=True)
    payload = {"format": 1, "title": title.strip(), "keywords": str(keywords or title)[:300],
               "mood": str(mood or "")[:80], "parent": parent, "scene": scene}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_SOURCE_BYTES:
        raise DrawingError("绘画源数据超过2 MiB，请减少图形或路径点。")
    safe_title = re.sub(r"[^\w\u4e00-\u9fff -]", "_", title.strip())[:40].strip() or "untitled"
    path = output / f"drawing_{safe_title}_{uuid.uuid4().hex}.png"
    source = path.with_suffix(".drawing.json")
    created = []
    image = render_scene(scene)
    try:
        metadata = PngInfo()
        metadata.add_text("Title", title.strip())
        metadata.add_text("DrawingSource", source.name)
        for target, binary in ((source, data), (path, None)):
            with target.open("xb") as stream:
                created.append(target)
                if binary is None:
                    image.save(stream, format="PNG", pnginfo=metadata)
                else:
                    stream.write(binary)
        filename = "drawings/" + path.name
        register_drawing(folder, filename, title=title.strip(), keywords=payload["keywords"], mood=payload["mood"])
    except (OSError, ValueError) as exc:
        for target in created:
            target.unlink(missing_ok=True)
        raise DrawingError("无法保存绘画图片或源数据：" + str(exc)[:160]) from exc
    finally:
        image.close()
    return {"sent": filename, "title": title.strip(), "keywords": payload["keywords"], "mood": payload["mood"],
            "width": scene["width"], "height": scene["height"], "editable": True,
            "revision": hashlib.sha256(data).hexdigest(), "node_count": len(scene["nodes"]),
            "layer_count": len(scene["layers"]), "parent": parent}


def draw_picture(folder: Path, *, title, code, width=768, height=768, keywords="", mood=""):
    # Lock before allocating any pixel buffers. Chat, QQ and idle requests share
    # this renderer, so concurrent callers queue without duplicating canvases.
    with _DRAWING_LOCK:
        program = DrawingProgram(width, height)
        program.run(code, render=False)
        return _save_artwork(folder, program.scene, title=title, keywords=keywords, mood=mood)


def _load_artwork(folder, filename):
    try:
        path = media_path(Path(folder), filename)
        if not is_drawing(filename) or path.suffix.lower() != ".png":
            raise DrawingError("请选择自己的 PNG 画作。")
        source = path.with_suffix(".drawing.json")
        if source.is_symlink():
            raise DrawingError("绘画源文件不能是符号链接。")
        if not source.is_file():
            raise DrawingError("这幅旧画没有可编辑源数据；可以继续发送原图，或重新创作。")
        with source.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise DrawingError("绘画源文件超过2 MiB。")
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("format") != 1:
            raise DrawingError("绘画源文件格式无效。")
        payload["scene"] = validate_scene(payload.get("scene"))
        return payload, hashlib.sha256(data).hexdigest(), "drawings/" + path.name
    except DrawingError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise DrawingError("无法读取绘画源数据：" + str(exc)[:160]) from exc


def read_drawing(folder: Path, filename, *, ids=None, offset=0, limit=30):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise DrawingError("offset须为非负整数，limit须为1–40。")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 40):
        raise DrawingError("ids最多40项。")
    if ids is not None:
        ids = [identifier(item) for item in ids]
    with _DRAWING_LOCK:
        payload, revision, filename = _load_artwork(folder, filename)
    scene = payload["scene"]
    nodes = scene["nodes"]
    if ids is not None:
        missing = set(ids) - {node["id"] for node in nodes}
        if missing:
            raise DrawingError("找不到图形 ID：" + ", ".join(sorted(missing)))
        nodes = [node for node in nodes if node["id"] in ids]
    selected, cost = [], 0
    for node in nodes[offset:offset + limit]:
        size = len(json.dumps(node, ensure_ascii=False))
        if selected and cost + size > 20000:
            break
        selected.append(node)
        cost += size
    next_offset = offset + len(selected)
    return {"file": filename, "title": payload.get("title", "一幅小画"), "revision": revision,
            "width": scene["width"], "height": scene["height"], "background": scene["background"],
            "layers": scene["layers"], "node_count": len(scene["nodes"]), "matched": len(nodes),
            "nodes": selected, "next_offset": next_offset if next_offset < len(nodes) else None}


def edit_drawing(folder: Path, filename, *, expect_revision, ops, title=None):
    with _DRAWING_LOCK:
        payload, revision, filename = _load_artwork(folder, filename)
        if not isinstance(expect_revision, str) or expect_revision != revision:
            raise DrawingError("画作源数据已变化，请重新read_drawing后再修改。")
        scene = apply_edits(payload["scene"], ops)
        return _save_artwork(folder, scene, title=title if title is not None else payload.get("title"),
                             keywords=payload.get("keywords", ""), mood=payload.get("mood", ""), parent=filename)


