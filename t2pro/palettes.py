"""Colour palettes for thermal imagery.

Each palette is a 256x3 uint8 lookup table built by linear interpolation
between control points, so adding one is a matter of listing stops.
"""

import numpy as np

_STOPS = {
    # name:        [(position 0..1, (r, g, b)), ...]
    "white-hot":   [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
    "black-hot":   [(0.0, (255, 255, 255)), (1.0, (0, 0, 0))],
    "ironbow":     [(0.00, (0, 0, 20)), (0.20, (60, 0, 110)), (0.40, (150, 20, 120)),
                    (0.60, (230, 70, 60)), (0.80, (255, 165, 10)), (1.00, (255, 255, 220))],
    "lava":        [(0.00, (0, 0, 0)), (0.35, (140, 20, 0)), (0.70, (255, 120, 0)),
                    (1.00, (255, 255, 190))],
    "arctic":      [(0.00, (0, 10, 40)), (0.35, (0, 90, 160)), (0.65, (110, 200, 220)),
                    (1.00, (255, 255, 255))],
    "rainbow":     [(0.00, (0, 0, 80)), (0.20, (0, 90, 220)), (0.40, (0, 200, 160)),
                    (0.60, (150, 230, 30)), (0.80, (255, 160, 0)), (1.00, (255, 30, 30))],
    "medical":     [(0.00, (0, 0, 0)), (0.15, (40, 0, 90)), (0.35, (0, 110, 190)),
                    (0.55, (0, 190, 90)), (0.72, (240, 230, 40)), (0.88, (240, 90, 20)),
                    (1.00, (255, 255, 255))],
    "amber":       [(0.00, (10, 5, 0)), (0.55, (150, 70, 0)), (1.00, (255, 220, 130))],
    "sepia":       [(0.00, (0, 0, 0)), (0.5, (120, 85, 60)), (1.00, (255, 240, 220))],
    "green-hot":   [(0.00, (0, 0, 0)), (0.6, (0, 160, 40)), (1.00, (220, 255, 190))],
}

NAMES = tuple(_STOPS)
DEFAULT = "ironbow"


def _build(stops):
    xs = np.linspace(0.0, 1.0, 256)
    pos = np.array([p for p, _ in stops], dtype=np.float64)
    lut = np.empty((256, 3), dtype=np.uint8)
    for ch in range(3):
        vals = np.array([c[ch] for _, c in stops], dtype=np.float64)
        # Round rather than truncate: astype() alone loses a count to float
        # error, which stops black-hot from being an exact mirror of white-hot.
        lut[:, ch] = np.clip(np.rint(np.interp(xs, pos, vals)), 0, 255).astype(np.uint8)
    return lut


_CACHE = {name: _build(stops) for name, stops in _STOPS.items()}


def lut(name):
    """Return the 256x3 uint8 LUT for `name`."""
    try:
        return _CACHE[name]
    except KeyError:
        raise KeyError(
            f"unknown palette {name!r}; available: {', '.join(NAMES)}") from None


def apply(gray8, name=DEFAULT):
    """Map a uint8 array through a palette, returning an RGB array."""
    return lut(name)[gray8]


def normalize(image, low=1.0, high=99.0, span=None, floor=0.0):
    """Scale a uint16 thermal image to uint8.

    `span` pins the mapping to an explicit (min, max) in raw counts, which keeps
    colours stable across frames.  Otherwise the given percentiles are used,
    which maximises contrast per frame.

    `floor` is the narrowest window the autoscaler is allowed to open to, in raw
    counts, centred on the frame.  Without it a featureless scene is stretched
    to full contrast no matter how little is actually in it, which is what makes
    a covered lens or a blank wall look patterned and grainy: measured here on a
    uniform scene the 1-99 window is only 67.6 counts wide, so the 2.5 counts
    RMS of temporal noise land 9.5 grey levels apart and the residual shading
    (16.5 counts RMS, see `T2Pro.capture_shading`) fills the palette end to end.
    A floor says "below this much real thermal contrast, show a flat field",
    which is the truthful rendering.  Ordinary scenes span hundreds to thousands
    of counts and are unaffected.
    """
    a = image.astype(np.float32)
    if span is not None:
        lo, hi = float(span[0]), float(span[1])
    else:
        lo, hi = np.percentile(a, [low, high])
    if hi - lo < 1e-6:
        hi = lo + 1.0
    if floor > hi - lo:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - 0.5 * floor, mid + 0.5 * floor
    return np.clip(np.rint((a - lo) * (255.0 / (hi - lo))), 0, 255).astype(np.uint8)


def render(image, name=DEFAULT, low=1.0, high=99.0, span=None, floor=0.0):
    """uint16 thermal image -> RGB uint8 image in the named palette."""
    return apply(normalize(image, low, high, span, floor), name)
