"""Runtime CLI argument parsing for encoding script.

Defines the public command-line surface consumed by encoding and studio tasks.
Keeps parser construction isolated from the encoding workflow implementation.
"""

import argparse
from typing import Optional


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the encoding script."""
    parser = argparse.ArgumentParser(description="Video encoding script")
    parser.add_argument("--encoding-type", required=True, help="CPU or GPU encoding type (Ex: CPU)")
    parser.add_argument("--base-dir", required=True, help="Base directory for input files")
    parser.add_argument("--input-file", required=True, help="Name of input file to encode")
    parser.add_argument("--work-dir", required=True, help="Work directory for output files")
    parser.add_argument("--debug", required=False, help="Run script in debug mode")
    parser.add_argument(
        "--hwaccel-device", required=False, help="HWACCEL_DEVICE parameter for GPU encoding (Ex: 0)"
    )
    parser.add_argument(
        "--cuda-visible-devices",
        required=False,
        help="CUDA_VISIBLE_DEVICES parameter for GPU encoding (Ex: 0,1)",
    )
    parser.add_argument(
        "--cuda-device-order",
        required=False,
        help="CUDA_DEVICE_ORDER parameter for GPU encoding (Ex: PCI_BUS_ID)",
    )
    parser.add_argument(
        "--cuda-path",
        required=False,
        help="CUDA_PATH parameter for GPU encoding (Ex: /usr/local/cuda-13.2)",
    )
    parser.add_argument(
        "--rendition",
        required=False,
        help=(
            "Rendition configuration JSON string "
            "(Ex: "
            '\'{"360":{"resolution":"640x360","video_bitrate":"750k","audio_bitrate":"96k","encode_mp4":true}}\''
            ")"
        ),
    )
    parser.add_argument(
        "--cut",
        required=False,
        help='Cut configuration JSON string (Ex: \'{"start": "00:00:05", "end": "00:00:17", "duration": "00:00:17"}\')',
    )
    parser.add_argument(
        "--dressing",
        required=False,
        help='Dressing configuration JSON string (Ex: \'{"watermark": "https://pod.univ.fr/media/files/xxx/watermark.png", "watermark_position": "En haut \\u00e0 droite", "watermark_position_orig": "top_right", "watermark_opacity": "100"}"\')',
    )
    parser.add_argument(
        "--video-id",
        required=False,
        help="Optional external video identifier used for tracing (Ex: 12345).",
    )
    parser.add_argument(
        "--video-slug",
        required=False,
        help="Optional external video slug used for tracing (Ex: introduction-python).",
    )
    parser.add_argument(
        "--video-title",
        required=False,
        help="Optional external video title used for tracing.",
    )
    return parser


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the encoding script."""
    return _build_arg_parser().parse_args(argv)
