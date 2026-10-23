"""Regenerate the built-in sticker samples (requires Pillow)."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw


OUT = Path(__file__).resolve().parents[1] / "media"
OUT.mkdir(exist_ok=True)


def sticker(color: str, mood: str, *, wink: bool = False, shift: int = 0) -> Image.Image:
    image = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.ellipse((48, 32 + shift, 312, 296 + shift), fill=color, outline="#41405C", width=7)
    pen.ellipse((75, 91 + shift, 115, 135 + shift), fill="#FFFFFF99")
    if mood == "surprised":
        pen.ellipse((118, 140 + shift, 140, 164 + shift), fill="#38364F")
        pen.ellipse((220, 140 + shift, 242, 164 + shift), fill="#38364F")
        pen.ellipse((165, 191 + shift, 195, 232 + shift), fill="#684265")
    else:
        if wink:
            pen.arc((107, 135 + shift, 145, 160 + shift), 200, 340, fill="#38364F", width=7)
        else:
            pen.ellipse((119, 139 + shift, 138, 164 + shift), fill="#38364F")
        pen.ellipse((223, 139 + shift, 242, 164 + shift), fill="#38364F")
        if mood == "comfort":
            pen.arc((155, 188 + shift, 206, 211 + shift), 190, 350, fill="#5A4A64", width=6)
        else:
            pen.arc((149, 178 + shift, 211, 228 + shift), 5, 175, fill="#5A415C", width=8)
        pen.ellipse((84, 180 + shift, 119, 200 + shift), fill="#FF7D9F88")
        pen.ellipse((241, 180 + shift, 276, 200 + shift), fill="#FF7D9F88")
    if mood == "hungry":
        pen.rounded_rectangle((247, 205 + shift, 256, 326 + shift), radius=4, fill="#9B7B71")
        pen.ellipse((239, 187 + shift, 264, 225 + shift), outline="#9B7B71", width=7)
    if mood == "comfort":
        pen.arc((17, 210 + shift, 110, 290 + shift), 190, 350, fill="#41405C", width=13)
        pen.arc((250, 210 + shift, 343, 290 + shift), 190, 350, fill="#41405C", width=13)
    else:
        for x, y in ((46, 68), (296, 66), (39, 235)):
            pen.line((x, y - 12, x, y + 12), fill="#FFD974", width=6)
            pen.line((x - 12, y, x + 12, y), fill="#FFD974", width=6)
    return image


for filename, color, mood in (
    ("happy.png", "#F9BACF", "happy"),
    ("comfort.png", "#A7CEF7", "comfort"),
    ("hungry.png", "#FFCD90", "hungry"),
    ("surprised.png", "#CBB9F7", "surprised"),
):
    sticker(color, mood).save(OUT / filename)

frames = [sticker("#F9BACF", "happy", wink=i in (2, 3), shift=-4 if i in (2, 3) else 0) for i in range(6)]
frames[0].save(OUT / "wink.gif", save_all=True, append_images=frames[1:], duration=[180, 180, 150, 150, 180, 400], loop=0, disposal=2)
