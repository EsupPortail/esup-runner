# runner/app/task_handlers/encoding/__init__.py
"""
Video encoding task handlers.
Specialized handlers for different video encoding tasks using FFmpeg.
"""

from .encoding_handler import VideoEncodingHandler


def get_handler():
    """
    Get the video encoding handler instance.

    Returns:
        VideoEncodingHandler: Encoding task handler
    """
    return VideoEncodingHandler
