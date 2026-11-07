"""The car's navigation route (Fleet Telemetry's RouteLine), as map points.

Tesla sends the active route as a Google encoded polyline at precision 6,
wrapped in base64. Some firmware may send the polyline unwrapped, so both are
read. Nothing here raises: a value that does not decode to a sensible route is
no route, and the map simply draws none.
"""

from __future__ import annotations

import base64
import binascii
import math
import zlib

ROUTE_MAX_POINTS = 1500       # sent to the phone; a long route is thinned to this
ROUTE_MAX_CHARS = 400_000     # a RouteLine longer than this is not believed


def _decode_polyline(text: str, precision: int) -> list[list[float]] | None:
    """Google's polyline algorithm; None when the text is not one."""
    factor = 10.0 ** precision
    pts: list[list[float]] = []
    lat = lon = 0
    i, n = 0, len(text)
    try:
        while i < n:
            vals = []
            for _ in range(2):
                shift = result = 0
                while True:
                    if i >= n:
                        return None
                    b = ord(text[i]) - 63
                    i += 1
                    if b < 0 or b > 63:
                        return None
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                vals.append(~(result >> 1) if result & 1 else result >> 1)
            lat += vals[0]
            lon += vals[1]
            y, x = lat / factor, lon / factor
            if not (-90.0 <= y <= 90.0 and -180.0 <= x <= 180.0):
                return None
            pts.append([round(y, 6), round(x, 6)])
    except (TypeError, ValueError):
        return None
    return pts or None


def _plausible(pts: list[list[float]] | None) -> bool:
    """Consecutive points of a road route are never continents apart."""
    if not pts:
        return False
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) > 5.0 or abs(a[1] - b[1]) > 5.0:
            return False
    return True


def _thin(pts: list[list[float]], keep: int) -> list[list[float]]:
    if len(pts) <= keep:
        return pts
    step = (len(pts) - 1) / (keep - 1)
    return [pts[round(k * step)] for k in range(keep)]


def decode_route(raw) -> list[list[float]] | None:
    """[[lat, lon], ...] for a RouteLine value, or None."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > ROUTE_MAX_CHARS:
        return None
    raw = raw.strip()
    candidates: list[str] = []
    try:
        dec = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("ascii")
        candidates.append(dec)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    candidates.append(raw)
    for text in candidates:
        for precision in (6, 5):
            pts = _decode_polyline(text, precision)
            if _plausible(pts):
                return _thin(pts, ROUTE_MAX_POINTS)
    return None


def route_version(raw) -> str | None:
    """A short tag that changes when the route does: the phone fetches the
    points again only then, not every second."""
    if not isinstance(raw, str) or not raw:
        return None
    return format(zlib.crc32(raw.encode("utf-8", "replace")) & 0xFFFFFFFF, "08x")


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    """Initial compass bearing from a to b (lat, lon), or None if they meet."""
    if a == b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
