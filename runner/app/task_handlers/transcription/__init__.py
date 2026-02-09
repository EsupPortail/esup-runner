"""
Transcription task handler package.
Provides automatic registration via get_handler().
"""

from .transcription_handler import TranscriptionHandler


def get_handler():
    return TranscriptionHandler
