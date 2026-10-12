"""Marker package for the Live Runner task type.

Live is intentionally not a :class:`BaseTaskHandler`: its long-running,
MediaMTX-backed lifecycle is implemented by ``app.services.live_runtime``.
This package keeps the four supported Runner task types visible together.
"""
