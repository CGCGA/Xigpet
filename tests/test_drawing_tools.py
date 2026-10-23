import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.drawing_tools import DrawingError, DrawingProgram, draw_picture


class DrawingTests(unittest.TestCase):
    def test_draws_a_real_png_with_loops_math_text_and_rgb_colors(self):
        code = """background('#edf6ff')
draw.ellipse((48,32,336,320),fill=(140,190,235),outline='#29485e',width=3)
points=[]
for i in range(5):
    a=math.tau*i/5
    points.append((192+75*cos(a),176+75*sin(a)))
draw.polygon(points,fill='#ffce88')
draw.text((192,370),'送给你',fill='#29485e',font_size=24,anchor='mm')
"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            folder = Path(directory) / "media"
            result = draw_picture(folder, title="小星星", code=code, width=384, height=448)
            self.assertEqual(result["title"], "小星星")
            self.assertTrue(result["sent"].startswith("drawings/drawing_"))
            with Image.open(folder / result["sent"]) as image:
                self.assertEqual(image.size, (384, 448))
                self.assertEqual(image.getpixel((0, 0)), (237, 246, 255))
                self.assertNotEqual(image.getpixel((192, 176)), image.getpixel((0, 0)))

    def test_rejects_imports_io_reflection_and_unbounded_code(self):
        programs = [
            "import os", "open('config/deepseek_key.txt')", "__import__('os')",
            "draw.__class__", "while True:\n    background('red')",
            "for i in range(1000000):\n    background('red')",
            "x='%1000000000s' % 'a'", "x='a'*1000000",
            "a=[]\nb=[a]\na.append(b)",
            "a=[0]\nfor i in range(500):\n    a=[a,a]",
            "def f():\n    return 1", "x=2**1000000",
        ]
        for code in programs:
            with self.subTest(code=code):
                with self.assertRaises(DrawingError):
                    DrawingProgram(256, 256).run(code)

    def test_calculation_and_canvas_limits_do_not_create_output_files(self):
        with self.assertRaises(DrawingError):
            DrawingProgram(100000, 100000)
        with self.assertRaisesRegex(DrawingError, "步数"):
            DrawingProgram(256, 256).run("for i in range(500):\n    for j in range(500):\n        x=0")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            folder = Path(directory) / "media"
            with self.assertRaises(DrawingError):
                draw_picture(folder, title="未完成", code="draw.ellipse((0,0,1,1),fill='not-a-color')")
            self.assertFalse(folder.exists())

    def test_python_fences_and_small_conditionals_are_accepted(self):
        image = DrawingProgram(256, 256).run("```python\nbackground('white')\nfor i in range(4):\n    if i%2==0:\n        draw.rectangle((i*40,0,i*40+30,50),fill='blue')\n```")
        self.assertEqual(image.getpixel((5, 5)), (0, 0, 255))
        self.assertEqual(image.getpixel((45, 5)), (255, 255, 255))
