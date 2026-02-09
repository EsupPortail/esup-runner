#!/usr/bin/env python3
"""Generate a PNG diagram of the project's directory tree.

Dependency-light: requires Pillow (PIL).

Default output: docs/tree.png

Usage:
  uv run scripts/generate_tree_diagram.py
  uv run scripts/generate_tree_diagram.py --max-depth 6 --out docs/tree.png
"""

from __future__ import annotations

import argparse
import fnmatch
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Sequence

from PIL import Image, ImageDraw, ImageFont

DEFAULT_IGNORES = [
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.so",
]


@dataclass(frozen=True)
class RenderConfig:
    font_size: int = 16
    padding_x: int = 30
    padding_y: int = 30
    line_spacing: float = 1.25
    bg: tuple[int, int, int] = (255, 255, 255)
    fg: tuple[int, int, int] = (20, 20, 30)
    max_width_px: int = 5200
    max_height_px: int = 5200


def _load_monospace_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _should_ignore(name: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        if pat == name:
            return True
        if any(ch in pat for ch in "*?[") and fnmatch.fnmatch(name, pat):
            return True
    return False


def _iter_children(path: Path, ignore: Sequence[str]) -> List[Path]:
    try:
        children = list(path.iterdir())
    except Exception:
        return []

    filtered = [p for p in children if not _should_ignore(p.name, ignore)]
    # Directories first, then files; stable sort by name.
    filtered.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    return filtered


def build_tree_lines(
    root: Path,
    ignore: Sequence[str],
    max_depth: int,
    max_lines: int,
) -> List[str]:
    lines: List[str] = []

    def walk(dir_path: Path, prefix: str, depth: int):
        if max_lines > 0 and len(lines) >= max_lines:
            return

        if max_depth >= 0 and depth > max_depth:
            return

        # Only show directories in the tree.
        children = [p for p in _iter_children(dir_path, ignore) if p.is_dir()]
        count = len(children)

        for idx, child in enumerate(children):
            if max_lines > 0 and len(lines) >= max_lines:
                return

            is_last = idx == count - 1
            branch = "└── " if is_last else "├── "
            name = child.name + "/"
            lines.append(prefix + branch + name)

            extension = "    " if is_last else "│   "
            walk(child, prefix + extension, depth + 1)

    # Root line
    root_name = root.name + "/"
    lines.append(root_name)
    walk(root, "", 1)

    if max_lines > 0 and len(lines) >= max_lines:
        lines.append("… (tree truncated: max-lines reached)")

    return lines


def render_png(lines: Sequence[str], out_path: Path, cfg: RenderConfig, title: str) -> None:
    font = _load_monospace_font(cfg.font_size)

    # Create a temporary draw context to measure text.
    tmp = Image.new("RGB", (10, 10), cfg.bg)
    draw = ImageDraw.Draw(tmp)

    line_h = int(cfg.font_size * cfg.line_spacing)

    # Prepend title lines.
    all_lines = [title, ""] + list(lines)

    widths = [int(draw.textlength(line, font=font)) for line in all_lines]
    max_w = max(widths) if widths else 0

    img_w = min(cfg.max_width_px, max_w + cfg.padding_x * 2)
    img_h = min(cfg.max_height_px, len(all_lines) * line_h + cfg.padding_y * 2)

    img = Image.new("RGB", (img_w, img_h), cfg.bg)
    draw = ImageDraw.Draw(img)

    x = cfg.padding_x
    y = cfg.padding_y

    for i, line in enumerate(all_lines):
        if y + line_h > img_h - cfg.padding_y:
            draw.text((x, y), "… (image truncated: max height reached)", font=font, fill=cfg.fg)
            break
        draw.text((x, y), line, font=font, fill=cfg.fg)
        y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate a PNG diagram of the project tree")
    p.add_argument(
        "--root",
        default=None,
        help="Project root directory (default: repository root)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output PNG path (default: docs/tree.png)",
    )
    p.add_argument(
        "--max-depth",
        type=int,
        default=8,
        help="Maximum depth to traverse (default: 8). Use -1 for unlimited.",
    )
    p.add_argument(
        "--max-lines",
        type=int,
        default=1200,
        help="Maximum number of lines to render (default: 1200). Use 0 for unlimited.",
    )
    p.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Add ignore pattern (can be repeated).",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    default_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).resolve() if args.root else default_root

    out_path = Path(args.out).resolve() if args.out else (default_root / "docs" / "tree.png")

    ignore = list(DEFAULT_IGNORES) + list(args.ignore or [])

    title = f"Directory tree — {root.name} — {datetime.now().isoformat(timespec='seconds')}"
    lines = build_tree_lines(
        root=root, ignore=ignore, max_depth=args.max_depth, max_lines=args.max_lines
    )

    render_png(lines=lines, out_path=out_path, cfg=RenderConfig(), title=title)
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
