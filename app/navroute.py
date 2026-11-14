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


def _decode_polyline(text: str, precision: int, cut: bool = False) -> list[list[float]] | None:
    """Google's polyline algorithm; None when the text is not one. With
    ``cut``, a text that stops partway through a point gives the points
    before it (a line cut short), where it would otherwise be refused."""
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
                        if cut and len(pts) >= 2:
                            i = n + 1          # leave both loops with what was read
                            break
                        return None
                    b = ord(text[i]) - 63
                    i += 1
                    if b < 0 or b > 63:
                        return None
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                if i > n:
                    break
                vals.append(~(result >> 1) if result & 1 else result >> 1)
            if i > n:
                break
            lat += vals[0]
            lon += vals[1]
            pts.append([round(lat / factor, 6), round(lon / factor, 6)])
    except (TypeError, ValueError):
        return None
    if not pts:
        return None
    # Latitude first, as Google's format has it; longitude first is read too
    # (a longitude over 90, as anywhere in Malaysia, cannot be a latitude).
    for y_i, x_i in ((0, 1), (1, 0)):
        if all(-90.0 <= p[y_i] <= 90.0 and -180.0 <= p[x_i] <= 180.0 for p in pts):
            return [[p[y_i], p[x_i]] for p in pts]
    return None


ROUTE_MAX_STEP_DEG = 0.5   # ~55 km: a road route's points are far closer than this


def _plausible(pts: list[list[float]] | None) -> bool:
    """A road route: at least two points, none a long jump from the last.
    Random bytes read as a polyline make wild jumps, and a line drawn from
    them would be a route the car never planned. Near the destination the
    car sends only a couple of points, so two are enough; a stray reading is
    still caught by the near-the-car check when the route is fetched."""
    if not pts or len(pts) < 2:
        return False
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) > ROUTE_MAX_STEP_DEG or abs(a[1] - b[1]) > ROUTE_MAX_STEP_DEG:
            return False
    return True


def _thin(pts: list[list[float]], keep: int) -> list[list[float]]:
    if len(pts) <= keep:
        return pts
    step = (len(pts) - 1) / (keep - 1)
    return [pts[round(k * step)] for k in range(keep)]


def _b64_variants(raw: str) -> list[bytes]:
    """Every way the text might be base64: standard or URL-safe alphabet,
    padded or not, with any whitespace dropped."""
    text = "".join(raw.split())
    out: list[bytes] = []
    for alt in (None, b"-_"):
        try:
            data = base64.b64decode(text + "=" * (-len(text) % 4), altchars=alt, validate=True)
        except (binascii.Error, ValueError):
            continue
        if data and data not in out:
            out.append(data)
    return out


def _inflate(data: bytes) -> bytes | None:
    """zlib, gzip or raw deflate, else None."""
    for wbits in (15, 31, -15):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    return None


POLY_CHARS = frozenset(range(63, 127))


def _poly_runs(data: bytes, least: int = 8) -> list[str]:
    """Stretches of the bytes made only of polyline characters, longest first:
    the polyline itself when it is wrapped in something else."""
    runs, start = [], None
    for i, c in enumerate(data + b"\x00"):
        if c in POLY_CHARS:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= least:
                runs.append(data[start:i].decode("ascii"))
            start = None
    return sorted(runs, key=len, reverse=True)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("varint")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, i


def _proto_coords(data: bytes, depth: int = 0) -> list[list[float]]:
    """Coordinates in a protobuf message, read without its schema: every pair
    of doubles (or floats) that lies on the globe, in order, through nested
    messages. Empty when the bytes are not a well-formed message."""
    import struct

    pts: list[list[float]] = []
    nums: list[float] = []
    i = 0
    try:
        while i < len(data):
            key, i = _varint(data, i)
            wt = key & 7
            if wt == 0:
                _, i = _varint(data, i)
            elif wt == 1:
                nums.append(struct.unpack_from("<d", data, i)[0]); i += 8
            elif wt == 5:
                nums.append(struct.unpack_from("<f", data, i)[0]); i += 4
            elif wt == 2:
                n, i = _varint(data, i)
                if i + n > len(data):
                    return []
                if depth < 4:
                    pts += _proto_coords(data[i:i + n], depth + 1)
                i += n
            else:
                return []
    except (ValueError, struct.error):
        return []
    for k in range(0, len(nums) - 1, 2):
        y, x = nums[k], nums[k + 1]
        if math.isfinite(y) and math.isfinite(x) and -90 <= y <= 90 and -180 <= x <= 180 and (y, x) != (0, 0):
            pts.append([round(y, 6), round(x, 6)])
    return pts


def decode_route_info(raw) -> tuple[list[list[float]] | None, str]:
    """(points, how they were read), or (None, a short description of the
    bytes) when no reading gives a route. The car's RouteLine is documented
    as a base64 polyline at precision 6; the other readings are there because
    what a car actually sends has not always matched the documentation."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > ROUTE_MAX_CHARS:
        return None, "empty"

    def poly(text: str, how: str, cut: bool = False):
        for precision in (6, 5):
            pts = _decode_polyline(text, precision, cut)
            if _plausible(pts) and len(pts) >= 2:
                return _thin(pts, ROUTE_MAX_POINTS), (
                    f"{how}, polyline precision {precision}" + (", cut short" if cut else ""))
        return None

    raw = raw.strip()
    blobs = _b64_variants(raw)
    got = _read_blobs(blobs, poly, False)
    if got:
        return got
    got = poly(raw, "plain")
    if got:
        return got
    # Nothing reads whole: a line that stops partway through its last point
    # (as a short one near the destination has been seen to, 14 Nov 2026:
    # "Route unreadable (124 chars)" 0.2 km from home) is read up to there.
    got = _read_blobs(blobs, poly, True) or poly(raw, "plain", True)
    if got:
        return got
    head = blobs[0][:12].hex(" ") if blobs else ""
    return None, (f"not readable: base64 to {len(blobs[0])} bytes, starting {head}" if blobs
                  else "not readable: not base64")


def _read_blobs(blobs: list[bytes], poly, cut: bool):
    for k, data in enumerate(blobs):
        tag = "base64" if k == 0 else "base64 (url-safe)"
        for body, how in ((data, tag), (_inflate(data), tag + ", compressed")):
            if not body:
                continue
            try:
                got = poly(body.decode("ascii"), how, cut)
                if got:
                    return got
            except UnicodeDecodeError:
                pass
            for run in _poly_runs(body)[:3]:
                got = poly(run, how + ", wrapped", cut)
                if got:
                    return got
            if cut:
                continue
            pts = _proto_coords(body)
            if _plausible(pts) and len(pts) >= 2:
                return _thin(pts, ROUTE_MAX_POINTS), how + ", coordinate pairs"
    return None


def decode_route(raw) -> list[list[float]] | None:
    """[[lat, lon], ...] for a RouteLine value, or None."""
    return decode_route_info(raw)[0]


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
