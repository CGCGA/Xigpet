"""Validated drawing commands and a bounded, supersampled Pillow renderer."""

from __future__ import annotations

import math
import re
import time
from copy import deepcopy

from PIL import Image, ImageColor, ImageDraw, ImageFont
from functools import lru_cache
from pathlib import Path
import os


MAX_NODES = 2500
MAX_LAYERS = 16
MAX_SOURCE_BYTES = 2 * 1024 * 1024
ANTIALIAS = 2
IDENTIFIER = re.compile(r"[\w\u4e00-\u9fff .-]{1,64}")


class DrawingError(ValueError):
    pass


def number(value, low=-1000000, high=1000000):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise DrawingError(f"绘图数值须在 {low} 到 {high} 之间。")
    return value


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DrawingError("图形或图层 ID 须为1–64个字母、数字、中文、空格或 ._-。")
    return value


def color(value):
    if value == "transparent":
        return (0, 0, 0, 0)
    try:
        if isinstance(value, str) and len(value) <= 100:
            return ImageColor.getcolor(value, "RGBA")
        if isinstance(value, (tuple, list)) and len(value) in (3, 4):
            if all(type(v) is int and 0 <= v <= 255 for v in value):
                return tuple(value) + ((255,) if len(value) == 3 else ())
    except (ValueError, TypeError):
        pass
    raise DrawingError("颜色须为颜色名、#RRGGBB、#RRGGBBAA 或 RGB/RGBA 数值。")


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise DrawingError("坐标须为 (x, y)。")
    return [number(v) for v in value]


def box(value):
    if not isinstance(value, (list, tuple)):
        raise DrawingError("边框须为 (x1,y1,x2,y2)。")
    if len(value) == 2:
        value = point(value[0]) + point(value[1])
    if len(value) != 4:
        raise DrawingError("边框须为 (x1,y1,x2,y2)。")
    result = [number(v) for v in value]
    if result[2] < result[0] or result[3] < result[1]:
        raise DrawingError("边框右下角不能小于左上角。")
    return result


def points(value):
    if not isinstance(value, (tuple, list)) or not value or len(value) > 300:
        raise DrawingError("点序列须为1–300个坐标。")
    if isinstance(value[0], (int, float)):
        if len(value) % 2:
            raise DrawingError("坐标数量须为偶数。")
        return [point(value[i:i + 2]) for i in range(0, len(value), 2)]
    return [point(item) for item in value]


def path_commands(value):
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 128:
        raise DrawingError("路径须有1–128条 M/L/Q/C/Z 指令。")
    result = []
    lengths = {"M": 2, "L": 2, "Q": 4, "C": 6, "Z": 0}
    for entry in value:
        if not isinstance(entry, (tuple, list)) or not entry or not isinstance(entry[0], str):
            raise DrawingError("路径指令示例：('M',x,y)、('C',x1,y1,x2,y2,x,y)。")
        op = entry[0].upper()
        if op not in lengths or len(entry) != lengths[op] + 1 or (not result and op != "M"):
            raise DrawingError("路径以 M 开始，支持绝对坐标 M/L/Q/C/Z。")
        result.append([op] + [number(v) for v in entry[1:]])
    return result


FIELDS = {
    "rectangle": ["fill", "outline", "width"],
    "rounded_rectangle": ["radius", "fill", "outline", "width"],
    "ellipse": ["fill", "outline", "width"],
    "polygon": ["fill", "outline", "width"],
    "line": ["fill", "width", "joint"],
    "point": ["fill"],
    "arc": ["start", "end", "fill", "width"],
    "pieslice": ["start", "end", "fill", "outline", "width"],
    "text": ["fill", "font_size", "anchor", "align"],
    "path": ["fill", "outline", "width"],
    "gradient": ["colors", "direction", "shape", "radius"],
}
BOX_KINDS = {"rectangle", "rounded_rectangle", "ellipse", "arc", "pieslice", "gradient"}


def normalize_node(kind, args, style, node_id, layer):
    if not isinstance(kind, str) or kind not in FIELDS or not isinstance(args, (list, tuple)) or not args or not isinstance(style, dict):
        raise DrawingError("不支持此绘图方法或参数。")
    style = deepcopy(style)
    positional = 2 if kind == "text" else 1
    if len(args) < positional or len(args) > positional + len(FIELDS[kind]):
        raise DrawingError("绘图位置参数数量不正确。")
    for key, value in zip(FIELDS[kind], args[positional:]):
        if key in style:
            raise DrawingError("重复的绘图参数：" + key)
        style[key] = value
    if set(style) - set(FIELDS[kind]) - {"opacity"}:
        raise DrawingError("绘图参数不在允许列表中。")
    if kind in BOX_KINDS:
        geometry = box(args[0])
    elif kind == "text":
        geometry = point(args[0])
    elif kind == "path":
        geometry = path_commands(args[0])
    else:
        geometry = points(args[0])
        if kind == "polygon" and len(geometry) < 3:
            raise DrawingError("多边形至少需要3个点。")
        if kind == "line" and len(geometry) < 2:
            raise DrawingError("线段至少需要2个点。")
    for key in ("fill", "outline"):
        if style.get(key) is not None:
            style[key] = list(color(style[key]))
    if "width" in style and (type(style["width"]) is not int or not 1 <= style["width"] <= 100):
        raise DrawingError("线宽须为1–100的整数。")
    if "radius" in style:
        number(style["radius"], 0, 512)
    if "opacity" in style:
        number(style["opacity"], 0, 1)
    for key in ("start", "end"):
        if key in style:
            number(style[key])
        elif kind in {"arc", "pieslice"}:
            raise DrawingError("圆弧或扇形需要 start 和 end 角度。")
    if "joint" in style and style["joint"] not in (None, "curve"):
        raise DrawingError("joint 仅支持 curve。")
    new_args = [geometry]
    if kind == "text":
        if not isinstance(args[1], str) or len(args[1]) > 200:
            raise DrawingError("绘图文字不能超过200字。")
        size = style.get("font_size", 24)
        if type(size) is not int or not 8 <= size <= 96:
            raise DrawingError("字号须为8–96的整数。")
        if "anchor" in style and style["anchor"] not in tuple(a + b for a in "lmr" for b in "atmsbd"):
            raise DrawingError("文字 anchor 不正确。")
        if "align" in style and style["align"] not in ("left", "center", "right"):
            raise DrawingError("文字 align 不正确。")
        new_args.append(args[1])
    if kind == "gradient":
        colors = style.get("colors")
        if not isinstance(colors, (list, tuple)) or not 2 <= len(colors) <= 8:
            raise DrawingError("渐变须提供2–8个 colors。")
        style["colors"] = [list(color(c)) for c in colors]
        if style.get("direction", "vertical") not in ("vertical", "horizontal"):
            raise DrawingError("渐变 direction 为 vertical 或 horizontal。")
        if style.get("shape", "rectangle") not in ("rectangle", "rounded_rectangle", "ellipse"):
            raise DrawingError("渐变 shape 为 rectangle、rounded_rectangle 或 ellipse。")
    return {"id": identifier(node_id), "layer": identifier(layer), "kind": kind,
            "args": new_args, "style": style}


def new_scene(width, height):
    if any(type(v) is not int or not 256 <= v <= 1024 for v in (width, height)):
        raise DrawingError("画布宽高须为256–1024的整数。")
    return {"format": 1, "width": width, "height": height, "background": "#f1f7fc",
            "layers": [{"id": "base", "opacity": 1, "visible": True}], "nodes": []}


def validate_scene(scene):
    if not isinstance(scene, dict) or scene.get("format") != 1:
        raise DrawingError("不支持此绘画源文件格式。")
    result = new_scene(scene.get("width"), scene.get("height"))
    result["background"] = list(color(scene.get("background", "#f1f7fc")))
    layers = scene.get("layers")
    nodes = scene.get("nodes")
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_LAYERS:
        raise DrawingError("画作须有1–16个图层。")
    result["layers"] = []
    layer_ids = set()
    for layer in layers:
        if not isinstance(layer, dict):
            raise DrawingError("图层格式无效。")
        name = identifier(layer.get("id"))
        if name in layer_ids or type(layer.get("visible", True)) is not bool:
            raise DrawingError("图层 ID 重复或 visible 不是布尔值。")
        layer_ids.add(name)
        result["layers"].append({"id": name, "opacity": number(layer.get("opacity", 1), 0, 1),
                                  "visible": layer.get("visible", True)})
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
        raise DrawingError("画作最多2500个图形。")
    seen = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise DrawingError("图形格式无效。")
        item = normalize_node(node.get("kind"), node.get("args"), node.get("style", {}),
                              node.get("id"), node.get("layer", "base"))
        if item["id"] in seen or item["layer"] not in layer_ids:
            raise DrawingError("图形 ID 重复或所属图层不存在。")
        seen.add(item["id"])
        result["nodes"].append(item)
    return result


@lru_cache(maxsize=24)
def font(size):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(str(fonts / filename), size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow 10.0 has a fixed-size default font.
        return ImageFont.load_default()


def flatten_path(commands, scale):
    paths, current, cursor, start = [], [], (0, 0), (0, 0)
    for command in commands:
        op, coords = command[0], command[1:]
        pts = [(coords[i] * scale, coords[i + 1] * scale) for i in range(0, len(coords), 2)]
        if op == "M":
            if current:
                paths.append(current)
            cursor = start = pts[0]
            current = [cursor]
        elif op == "Z":
            current.append(start)
            cursor = start
        elif op == "L":
            cursor = pts[0]
            current.append(cursor)
        else:
            controls = [cursor] + pts
            length = sum(math.dist(a, b) for a, b in zip(controls, controls[1:]))
            # Sampling is bounded independently of user coordinates.
            steps = max(8, min(256, math.ceil(length / 3)))
            for i in range(1, steps + 1):
                t, u = i / steps, 1 - i / steps
                if op == "Q":
                    weights = (u * u, 2 * u * t, t * t)
                else:
                    weights = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
                current.append(tuple(sum(p[axis] * w for p, w in zip(controls, weights)) for axis in (0, 1)))
            cursor = pts[-1]
    if current:
        paths.append(current)
    return paths


def _gradient(target, xy, style, origin, scale):
    direction = style.get("direction", "vertical")
    vertical = direction == "vertical"
    axis = 1 if vertical else 0
    length = target.height if vertical else target.width
    start, end = xy[axis], xy[axis + 2]
    colors = style["colors"]
    samples = []
    for i in range(length):
        t = max(0, min(1, (origin[axis] + i - start) / max(1, end - start))) * (len(colors) - 1)
        index = min(len(colors) - 2, int(t))
        fraction = t - index
        samples.append(tuple(round(a + (b - a) * fraction) for a, b in zip(colors[index], colors[index + 1])))
    strip = Image.new("RGBA", (1, length) if vertical else (length, 1))
    strip.putdata(samples)
    gradient = strip.resize(target.size)
    strip.close()
    mask = Image.new("L", target.size)
    painter = ImageDraw.Draw(mask)
    local = (xy[0] - origin[0], xy[1] - origin[1], xy[2] - origin[0], xy[3] - origin[1])
    shape = style.get("shape", "rectangle")
    options = {"radius": style.get("radius", 0) * scale} if shape == "rounded_rectangle" else {}
    getattr(painter, shape)(local, fill=255, **options)
    target.paste(gradient, (0, 0), mask)
    mask.close()
    gradient.close()


def _paint_node(target, node, scale):
    kind, style = node["kind"], dict(node["style"])
    opacity = style.pop("opacity", 1)
    if opacity == 0:
        return
    coords = node["args"][0]
    paths = None
    text_font = None
    if kind in BOX_KINDS:
        xy = [v * scale for v in coords]
        bounds = xy
    elif kind == "text":
        xy = [v * scale for v in coords]
        text_font = font(style.get("font_size", 24) * scale)
        bounds = ImageDraw.Draw(target).textbbox(tuple(xy), node["args"][1], font=text_font,
                   anchor=style.get("anchor"), align=style.get("align", "left"))
    else:
        paths = flatten_path(coords, scale) if kind == "path" else [[(p[0] * scale, p[1] * scale) for p in coords]]
        all_points = [p for path in paths for p in path]
        bounds = (min(p[0] for p in all_points), min(p[1] for p in all_points),
                  max(p[0] for p in all_points), max(p[1] for p in all_points))
    pad = style.get("width", 1) * scale + 3
    left, top = max(0, math.floor(bounds[0] - pad)), max(0, math.floor(bounds[1] - pad))
    right, bottom = min(target.width, math.ceil(bounds[2] + pad + 1)), min(target.height, math.ceil(bounds[3] + pad + 1))
    if right <= left or bottom <= top:
        return
    # Only the clipped area of one command is temporary, rather than a full
    # canvas per shape. Alpha composites correctly even within the same layer.
    tile = Image.new("RGBA", (right - left, bottom - top))
    try:
        painter = ImageDraw.Draw(tile)
        if kind == "gradient":
            _gradient(tile, xy, style, (left, top), scale)
        elif kind == "text":
            style.pop("font_size", None)
            style["fill"] = tuple(style.get("fill") or (0, 0, 0, 255))
            painter.text((xy[0] - left, xy[1] - top), node["args"][1], font=text_font, **style)
        else:
            for key in ("fill", "outline"):
                if style.get(key) is not None:
                    style[key] = tuple(style[key])
            if "width" in style:
                style["width"] *= scale
            elif kind != "point":
                style["width"] = scale
            if "radius" in style:
                style["radius"] *= scale
            if kind in BOX_KINDS:
                local = (xy[0] - left, xy[1] - top, xy[2] - left, xy[3] - top)
                getattr(painter, kind)(local, **style)
            else:
                for path in paths:
                    local = [(p[0] - left, p[1] - top) for p in path]
                    if kind == "path":
                        if style.get("fill") is not None and len(local) >= 3:
                            painter.polygon(local, fill=style["fill"])
                        if style.get("outline") is not None and len(local) >= 2:
                            painter.line(local, fill=style["outline"], width=style.get("width", scale), joint="curve")
                    elif kind == "point":
                        # A logical point occupies a full pixel before downsampling.
                        for x, y in local:
                            painter.rectangle((x, y, x + scale - 1, y + scale - 1), fill=style.get("fill"))
                    else:
                        getattr(painter, kind)(local, **style)
        if opacity != 1:
            alpha = tile.getchannel("A")
            adjusted = alpha.point([round(i * opacity) for i in range(256)])
            tile.putalpha(adjusted)
            alpha.close()
            adjusted.close()
        target.alpha_composite(tile, (left, top))
    finally:
        tile.close()


def render_scene(raw_scene):
    scene = validate_scene(raw_scene)
    scale = ANTIALIAS
    size = (scene["width"] * scale, scene["height"] * scale)
    canvas = Image.new("RGBA", size, tuple(scene["background"]))
    deadline = time.monotonic() + 20
    try:
        for layer in scene["layers"]:
            if not layer["visible"] or layer["opacity"] == 0:
                continue
            nodes = [node for node in scene["nodes"] if node["layer"] == layer["id"]]
            if not nodes:
                continue
            # Fully opaque layers can composite commands directly onto the
            # canvas. Only group opacity needs an isolated full-size buffer.
            surface = canvas if layer["opacity"] == 1 else Image.new("RGBA", size)
            try:
                for node in nodes:
                    if time.monotonic() > deadline:
                        raise DrawingError("绘图渲染超过20秒，请减少图形或图层。")
                    _paint_node(surface, node, scale)
                if layer["opacity"] != 1:
                    alpha = surface.getchannel("A")
                    adjusted = alpha.point([round(i * layer["opacity"]) for i in range(256)])
                    surface.putalpha(adjusted)
                    alpha.close()
                    adjusted.close()
                if surface is not canvas:
                    canvas.alpha_composite(surface)
            finally:
                if surface is not canvas:
                    surface.close()
        return canvas.resize((scene["width"], scene["height"]), Image.Resampling.LANCZOS)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DrawingError("绘图渲染失败：" + str(exc)[:200]) from exc
    finally:
        canvas.close()


def apply_edits(raw_scene, ops):
    scene = validate_scene(raw_scene)
    if not isinstance(ops, list) or not 1 <= len(ops) <= 100:
        raise DrawingError("每次修改须提供1–100个 ops。")
    for op in ops:
        if not isinstance(op, dict):
            raise DrawingError("修改操作须为对象。")
        kind = op.get("op")
        if kind == "background":
            scene["background"] = list(color(op.get("color")))
            continue
        if kind == "layer":
            name = identifier(op.get("id"))
            layer = next((item for item in scene["layers"] if item["id"] == name), None)
            if layer is None:
                layer = {"id": name, "opacity": 1, "visible": True}
                scene["layers"].append(layer)
            for key in ("opacity", "visible"):
                if key in op:
                    layer[key] = op[key]
            continue
        if kind == "add":
            scene["nodes"].append(normalize_node(op.get("kind"), op.get("args"), op.get("style", {}),
                                                 op.get("id"), op.get("layer", "base")))
            continue
        node = next((item for item in scene["nodes"] if item["id"] == op.get("id")), None)
        if node is None:
            raise DrawingError("找不到图形 ID：" + str(op.get("id")))
        if kind == "remove":
            scene["nodes"].remove(node)
        elif kind == "set":
            if "args" in op:
                node["args"] = op["args"]
            if "style" in op:
                if not isinstance(op["style"], dict):
                    raise DrawingError("style 须为对象。")
                node["style"].update(op["style"])
            if "layer" in op:
                node["layer"] = op["layer"]
        elif kind == "move":
            dx, dy = number(op.get("dx", 0)), number(op.get("dy", 0))
            coords = node["args"][0]
            if node["kind"] == "path":
                for command in coords:
                    for i in range(1, len(command), 2):
                        command[i] += dx
                        command[i + 1] += dy
            elif node["kind"] in BOX_KINDS or node["kind"] == "text":
                for i in range(0, len(coords), 2):
                    coords[i] += dx
                    coords[i + 1] += dy
            else:
                for p in coords:
                    p[0] += dx
                    p[1] += dy
        elif kind == "order":
            if op.get("position") not in ("front", "back"):
                raise DrawingError("position 须为 front 或 back。")
            scene["nodes"].remove(node)
            scene["nodes"].insert(len(scene["nodes"]) if op["position"] == "front" else 0, node)
        else:
            raise DrawingError("不支持的修改操作：" + str(kind))
    return validate_scene(scene)
