"""CLI parsing helpers for the studio runtime."""

from __future__ import annotations

import argparse
from typing import Optional


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the studio generator script."""
    parser = argparse.ArgumentParser(description="Studio base video generator")
    parser.add_argument("--xml-url", required=True, help="Mediapackage XML URL")
    parser.add_argument("--base-dir", required=True, help="Base directory for input files")
    parser.add_argument(
        "--work-dir", required=True, help="Working directory for intermediate files"
    )
    parser.add_argument("--output-file", required=True, help="Output video file path")
    parser.add_argument("--debug", required=False, help="Run script in debug mode")
    parser.add_argument(
        "--presenter", required=False, help="Override presenter layout (mid, piph, pipb)"
    )
    parser.add_argument(
        "--encoding-type", required=False, help="CPU or GPU encoding type (Ex: CPU)"
    )
    parser.add_argument(
        "--hwaccel-device", required=False, help="HW acceleration device index (Ex: 0)"
    )
    parser.add_argument(
        "--cuda-visible-devices", required=False, help="CUDA visible devices (Ex: 0,1)"
    )
    parser.add_argument(
        "--cuda-device-order", required=False, help="CUDA device order (Ex: PCI_BUS_ID)"
    )
    parser.add_argument("--cuda-path", required=False, help="CUDA installation path")
    parser.add_argument(
        "--force-cpu", required=False, help="Force CPU pipeline even if GPU requested"
    )
    parser.add_argument("--studio-crf", required=False, help="CRF for libx264/NVENC if applicable")
    parser.add_argument("--studio-preset", required=False, help="x264 preset or NVENC preset")
    parser.add_argument("--studio-audio-bitrate", required=False, help="Audio bitrate, e.g., 128k")
    parser.add_argument(
        "--studio-allow-nvenc",
        required=False,
        help="Allow NVENC in studio generation even for WebM/VP8/VP9/AV1 inputs (default: false)",
    )
    return parser


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for studio generation."""
    return build_arg_parser().parse_args(argv)
