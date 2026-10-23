"""Application version, shared by source and frozen distributions."""

import json
import sys
from pathlib import Path


REPOSITORY = "CGCGA/Xigpet"
_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
VERSION = json.loads((_root / "VERSION.json").read_text(encoding="utf-8"))["version"]
