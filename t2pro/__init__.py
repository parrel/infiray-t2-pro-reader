"""Read and control the InfiRay T2 Pro thermal camera (iOS/MFi variant)."""

from . import palettes, protocol
from .device import AutoFFC, Frame, T2Pro

__all__ = ["AutoFFC", "Frame", "T2Pro", "palettes", "protocol"]
