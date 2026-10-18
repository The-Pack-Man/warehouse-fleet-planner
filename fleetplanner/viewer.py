"""Build a standalone replay page: the viewer HTML with a run log embedded."""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

PLACEHOLDER = "/*__RUNLOG__*/null"


def build_html(log: dict[str, Any], name: str | None = None) -> str:
    template = files("fleetplanner").joinpath("viewer.html").read_text(encoding="utf-8")
    data = dict(log, name=name) if name else log
    # Escape "</" so the JSON can't close the <script> tag early.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return template.replace(PLACEHOLDER, payload, 1)


def write_html(log: dict[str, Any], path: str | Path, name: str | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html(log, name), encoding="utf-8")
    return path
