"""A bounded interpreter for Python drawing statements; never executes Python bytecode."""

from __future__ import annotations

import ast
import math
import operator
import os
import random
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

from .media_tools import register_drawing


DRAWING_HELP = """通过 draw_picture 创作程序绘图。传 title（给作品取名）、code、可选 keywords（主题标签）、mood（情绪标签）、width/height（256–1024，默认768）。
code 是受限 Python 绘图语句，不需要 import，不要创建 Image 或保存文件，程序会自动保存并发送 PNG。
已有 W、H、pi、tau；支持变量、四则运算、if、for/range（单次最多500项）、列表/元组、列表.append、下标和简单 f-string。
可调用 draw.rectangle、draw.rounded_rectangle、draw.ellipse、draw.polygon、draw.line、draw.arc、draw.pieslice、draw.point，参数与 Pillow 对应；draw.text((x,y), '文字', fill='颜色', font_size=24, anchor='mm')。
background('颜色') 设置底色。支持 sin/cos/sqrt/radians（也可 math.sin 等）、randint/uniform（也可 random.uniform）、min/max/abs/round/int/float/str/len。
没有 import、while、函数/类定义、文件、网络、进程、任意属性访问。坐标数值不得超过1000000；最多2500次绘图、30000步计算，超限会失败。
示例：
background('#eef7ff')
draw.ellipse((W*.25,H*.2,W*.75,H*.7),fill='#9ac9ef',outline='#529bd3',width=4)
for i in range(5):
    x=80+i*140
    draw.line([(x,600),(x+30,550)],fill='#88b58d',width=6)
draw.text((W/2,H*.83),'送给你',fill='#29485e',font_size=32,anchor='mm')
成功后自然说出画了什么，图片由程序单独发送；失败时根据错误修改代码，不能声称已经画好。"""


class DrawingError(ValueError):
    pass


@lru_cache(maxsize=64)
def _font(size):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(str(fonts / filename), size)
        except OSError:
            pass
    return ImageFont.load_default()


class DrawingProgram:
    def __init__(self, width=768, height=768):
        if any(type(v) is not int or not 256 <= v <= 1024 for v in (width, height)):
            raise DrawingError("画布宽高须为 256–1024 的整数。")
        self.image = Image.new("RGB", (width, height), "#f1f7fc")
        self.draw = ImageDraw.Draw(self.image)
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
                     "uniform": random.uniform, "randint": random.randint}
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
                return self._primitive("rectangle", [(0, 0, *self.image.size)], {"fill": args[0]})
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
        if self.draws > 2500:
            raise DrawingError("最多2500次绘图调用。")
        allowed = {"rectangle", "rounded_rectangle", "ellipse", "polygon", "line", "arc", "pieslice", "point", "text"}
        if name not in allowed or not args:
            raise DrawingError("不支持此绘图方法。")
        if any(key not in {"fill", "outline", "width", "radius", "joint", "start", "end", "font_size", "anchor", "align"} for key in kwargs):
            raise DrawingError("绘图参数不在允许列表中。")
        if "width" in kwargs and (type(kwargs["width"]) is not int or not 1 <= kwargs["width"] <= 100):
            raise DrawingError("线宽须为1–100。")
        if "radius" in kwargs and not 0 <= kwargs["radius"] <= 512:
            raise DrawingError("圆角半径须为0–512。")
        if len(args[0]) > 300:
            raise DrawingError("单次绘图的点过多。")
        if name == "text":
            if len(args) != 2 or not isinstance(args[1], str) or len(args[1]) > 200:
                raise DrawingError("文字绘图须提供坐标和不超过200字的文字。")
            size = kwargs.pop("font_size", 24)
            if type(size) is not int or not 8 <= size <= 96:
                raise DrawingError("字号须为8–96。")
            kwargs["font"] = _font(size)
        method = getattr(self.draw, name)  # name is restricted to the fixed set above.
        method(*args, **kwargs)
        return None

    def run(self, code):
        if not isinstance(code, str) or not code.strip() or len(code) > 16000:
            raise DrawingError("绘图代码须为1–16000个字符。")
        code = re.sub(r"^```(?:python)?\s*\n|\n```\s*$", "", code.strip())
        try:
            tree = ast.parse(code)
            if sum(1 for _ in ast.walk(tree)) > 3000:
                raise DrawingError("绘图代码结构过大。")
            self._block(tree.body)
            if not self.draws:
                raise DrawingError("代码没有画任何内容。")
            return self.image
        except DrawingError:
            raise
        except (ValueError, TypeError, SyntaxError, ArithmeticError, IndexError, RecursionError) as exc:
            raise DrawingError(f"绘图代码错误：{str(exc)[:250]}") from exc


def draw_picture(folder: Path, *, title, code, width=768, height=768, keywords="", mood=""):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise DrawingError("请提供1–100字的绘画标题。")
    program = DrawingProgram(width, height)
    image = program.run(code)
    if folder.is_symlink():
        raise DrawingError("绘画输出目录不能是符号链接。")
    output = folder / "drawings"
    if output.is_symlink():
        raise DrawingError("绘画输出目录不能是符号链接。")
    output.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\u4e00-\u9fff -]", "_", title.strip())[:40].strip() or "untitled"
    name = f"drawing_{safe_title}_{uuid.uuid4().hex}.png"
    filename = "drawings/" + name
    path = output / name
    written = False
    try:
        metadata = PngInfo()
        metadata.add_text("Title", title.strip())
        with path.open("xb") as target:
            written = True
            image.save(target, format="PNG", pnginfo=metadata)
        register_drawing(folder, filename, title=title.strip(), keywords=keywords, mood=mood)
    except (OSError, ValueError) as exc:
        if written:
            path.unlink(missing_ok=True)
        raise DrawingError("无法保存绘画图片或作品索引。") from exc
    return {"sent": filename, "title": title.strip(), "keywords": str(keywords or title), "mood": str(mood),
            "width": width, "height": height}
