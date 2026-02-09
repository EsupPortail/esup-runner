#!/usr/bin/env python3
"""Generate a PNG diagram summarizing this project's architecture.

This script is intentionally dependency-light: it only requires Pillow.
Output: docs/architecture.png

Usage:
  uv run scripts/generate_architecture_diagram.py
  uv run scripts/generate_architecture_diagram.py --max-depth 6 --out docs/architecture.png
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:  # pragma: no cover
    print("Missing dependency: Pillow")
    print("Install with: uv pip install Pillow")
    raise SystemExit(2)


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    title: str
    lines: Tuple[str, ...] = ()

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def top(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, self.y1)

    @property
    def bottom(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, self.y2)

    @property
    def left(self) -> tuple[int, int]:
        return (self.x1, (self.y1 + self.y2) // 2)

    @property
    def right(self) -> tuple[int, int]:
        return (self.x2, (self.y1 + self.y2) // 2)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []

    def width(s: str) -> int:
        return int(draw.textlength(s, font=font))

    for w in words:
        trial = (" ".join(cur + [w])).strip()
        if trial and width(trial) <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
                cur = [w]
            else:
                lines.append(w)
                cur = []
    if cur:
        lines.append(" ".join(cur))
    return lines


def draw_box(
    draw: ImageDraw.ImageDraw,
    box: Box,
    title_font: ImageFont.ImageFont,
    body_font: ImageFont.ImageFont,
    fill: tuple[int, int, int] = (250, 250, 252),
    outline: tuple[int, int, int] = (40, 40, 60),
    header_fill: tuple[int, int, int] = (235, 235, 245),
):
    radius = 12
    draw.rounded_rectangle(
        [box.x1, box.y1, box.x2, box.y2], radius=radius, fill=fill, outline=outline, width=2
    )

    header_h = 36
    draw.rounded_rectangle(
        [box.x1, box.y1, box.x2, box.y1 + header_h],
        radius=radius,
        fill=header_fill,
        outline=outline,
        width=2,
    )

    title_x = box.x1 + 12
    title_y = box.y1 + 8
    draw.text((title_x, title_y), box.title, font=title_font, fill=(10, 10, 20))

    max_text_width = (box.x2 - box.x1) - 24
    y = box.y1 + header_h + 10

    for raw in box.lines:
        for line in _wrap(draw, raw, body_font, max_text_width):
            draw.text((box.x1 + 12, y), line, font=body_font, fill=(20, 20, 30))
            y += int(body_font.size * 1.25)


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    label: str | None,
    font: ImageFont.ImageFont,
    color: tuple[int, int, int] = (30, 30, 60),
):
    # Main line
    draw.line([start, end], fill=color, width=3)

    # Arrow head
    import math

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    angle = math.atan2(dy, dx)

    head_len = 14
    head_ang = math.radians(24)

    p1 = (
        int(end[0] - head_len * math.cos(angle - head_ang)),
        int(end[1] - head_len * math.sin(angle - head_ang)),
    )
    p2 = (
        int(end[0] - head_len * math.cos(angle + head_ang)),
        int(end[1] - head_len * math.sin(angle + head_ang)),
    )
    draw.polygon([end, p1, p2], fill=color)

    if label:
        mid = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
        pad = 6
        text_w = int(draw.textlength(label, font=font))
        text_h = int(font.size * 1.25)
        rect = [
            mid[0] - text_w // 2 - pad,
            mid[1] - text_h // 2 - pad,
            mid[0] + text_w // 2 + pad,
            mid[1] + text_h // 2 + pad,
        ]
        draw.rounded_rectangle(
            rect, radius=8, fill=(255, 255, 255), outline=(200, 200, 210), width=1
        )
        draw.text((rect[0] + pad, rect[1] + pad), label, font=font, fill=(30, 30, 60))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    out_path = root / "docs" / "architecture.png"

    W, H = 1900, 1100
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(20)
    body_font = _load_font(16)
    small_font = _load_font(14)
    big_title_font = _load_font(28)

    # Title
    draw.text(
        (40, 24), "ESUP Runner — Architecture Overview", font=big_title_font, fill=(10, 10, 20)
    )
    draw.text(
        (40, 62),
        "Logical view (multi-process launcher → FastAPI → dispatch → handlers → result storage)",
        font=body_font,
        fill=(60, 60, 80),
    )

    # Components for this repository (Runner Manager)
    clients = Box(
        40,
        120,
        520,
        270,
        "Clients / External Systems",
        (
            "Trigger tasks through the Manager API",
            "Check status (dashboard / endpoints)",
        ),
    )

    launcher = Box(
        600,
        120,
        1040,
        270,
        "Launcher (launcher.py)",
        (
            "Dev: Uvicorn (reload)",
            "Prod: Gunicorn + UvicornWorker",
        ),
    )

    manager_api = Box(
        560,
        320,
        1840,
        1040,
        "Runner Manager — FastAPI (app/main.py)",
        (
            "Lifespan: include routers + start/stop background services",
            "Exposure: /manager/*, /runner/*, /task/*, /admin, /logs",
        ),
    )

    api_layer = Box(
        600,
        400,
        980,
        580,
        "API (FastAPI routes)",
        (
            "app/api/routes/runner.py: register + heartbeat",
            "app/api/routes/task.py: execute + completion + UI /tasks",
            "app/api/routes/admin.py: dashboard / pages",
        ),
    )

    core_layer = Box(
        1010,
        400,
        1330,
        580,
        "Core",
        (
            "config (.env), auth (token/admin), state (runners/tasks)",
            "persistence: DailyJSONPersistence (data/YYYY-MM-DD/*.json)",
        ),
    )

    services = Box(
        600,
        640,
        980,
        820,
        "Services",
        (
            "background_service: start/stop async tasks",
            "runner_service: monitor heartbeats",
            "task_service: cleanup + timeouts",
        ),
    )

    runners = Box(
        1010,
        640,
        1800,
        820,
        "Runners (agents)",
        (
            "Register: POST /runner/register",
            "Heartbeat: POST /runner/heartbeat/{runner_id}",
            "Execute: POST {runner.url}/task/run (called by manager)",
        ),
    )

    storage = Box(
        1010,
        860,
        1800,
        1010,
        "Persistence / Data",
        (
            "data/YYYY-MM-DD/*.json (1 file per task)",
            "FileLock for atomic writes",
        ),
    )

    # Draw boxes
    for b in [clients, launcher, manager_api, api_layer, core_layer, services, runners, storage]:
        draw_box(draw, b, title_font=title_font, body_font=body_font)

    # Arrows (high-level)
    draw_arrow(
        draw,
        clients.right,
        (launcher.x1, (launcher.y1 + launcher.y2) // 2),
        "start service",
        small_font,
    )
    draw_arrow(draw, launcher.bottom, manager_api.top, "serve API", small_font)

    # Client interactions
    draw_arrow(draw, clients.bottom, (api_layer.x1 + 40, api_layer.y1), "/task/execute", small_font)
    draw_arrow(
        draw, clients.bottom, (api_layer.x2 - 40, api_layer.y1), "/admin + /tasks", small_font
    )

    # Internal flows
    draw_arrow(draw, api_layer.bottom, services.top, "async services", small_font)
    draw_arrow(draw, api_layer.right, core_layer.left, "auth + state", small_font)
    draw_arrow(draw, services.right, runners.left, "delegate", small_font)
    draw_arrow(draw, core_layer.bottom, storage.top, "save/load", small_font)

    # Legend
    legend = Box(
        40,
        320,
        520,
        520,
        "Legend",
        (
            "Arrows: main flows (call / delegation / persistence)",
            "Boxes: major Runner Manager modules",
        ),
    )
    draw_box(draw, legend, title_font=title_font, body_font=body_font)

    # Footer
    draw.text(
        (40, 1060),
        f"Generated by scripts/generate_architecture_diagram.py → {out_path.relative_to(root)}",
        font=small_font,
        fill=(90, 90, 110),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
