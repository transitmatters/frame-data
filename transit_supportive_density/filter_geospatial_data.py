#!/usr/bin/env python3
"""Filter qgis2web GeoJSON-JS data files in place to a configured lon/lat box.

The script is intentionally standard-library only. It preserves retained feature
objects byte-for-byte and retains a whole feature whenever its geometry
intersects the configured box; geometries are not clipped.

By default, configuration is read from filter_geospatial_data_config.json next
to this script, and paths are resolved relative to that directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable, Sequence

FEATURES_MARKER = b'"features":['


def _point_in_box(point: Sequence[float], box: dict[str, float]) -> bool:
    x, y = float(point[0]), float(point[1])
    return box["west"] <= x <= box["east"] and box["south"] <= y <= box["north"]


def _orientation(a, b, c) -> int:
    value = (float(b[1]) - float(a[1])) * (float(c[0]) - float(b[0])) - \
            (float(b[0]) - float(a[0])) * (float(c[1]) - float(b[1]))
    eps = 1e-12
    if abs(value) <= eps:
        return 0
    return 1 if value > 0 else 2


def _on_segment(a, b, c) -> bool:
    # b lies on segment a-c (assuming collinearity).
    eps = 1e-12
    return (min(float(a[0]), float(c[0])) - eps <= float(b[0]) <= max(float(a[0]), float(c[0])) + eps and
            min(float(a[1]), float(c[1])) - eps <= float(b[1]) <= max(float(a[1]), float(c[1])) + eps)


def _segments_intersect(p1, q1, p2, q2) -> bool:
    o1 = _orientation(p1, q1, p2)
    o2 = _orientation(p1, q1, q2)
    o3 = _orientation(p2, q2, p1)
    o4 = _orientation(p2, q2, q1)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _on_segment(p1, p2, q1)) or
            (o2 == 0 and _on_segment(p1, q2, q1)) or
            (o3 == 0 and _on_segment(p2, p1, q2)) or
            (o4 == 0 and _on_segment(p2, q1, q2)))


def _segment_intersects_box(a, b, box: dict[str, float]) -> bool:
    if _point_in_box(a, box) or _point_in_box(b, box):
        return True
    min_x, max_x = sorted((float(a[0]), float(b[0])))
    min_y, max_y = sorted((float(a[1]), float(b[1])))
    if max_x < box["west"] or min_x > box["east"] or max_y < box["south"] or min_y > box["north"]:
        return False
    sw = (box["west"], box["south"])
    nw = (box["west"], box["north"])
    ne = (box["east"], box["north"])
    se = (box["east"], box["south"])
    return any(_segments_intersect(a, b, c, d) for c, d in ((sw, nw), (nw, ne), (ne, se), (se, sw)))


def _line_intersects_box(coords: Sequence[Sequence[float]], box: dict[str, float]) -> bool:
    if any(_point_in_box(p, box) for p in coords):
        return True
    return any(_segment_intersects_box(coords[i - 1], coords[i], box) for i in range(1, len(coords)))


def _point_in_ring(point, ring: Sequence[Sequence[float]]) -> bool:
    x, y = float(point[0]), float(point[1])
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        # Treat a boundary hit as inside.
        if _orientation((xi, yi), (x, y), (xj, yj)) == 0 and _on_segment((xi, yi), (x, y), (xj, yj)):
            return True
        if ((yi > y) != (yj > y)):
            cross_x = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(point, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    if not rings or not _point_in_ring(point, rings[0]):
        return False
    return not any(_point_in_ring(point, hole) for hole in rings[1:])


def _polygon_intersects_box(rings, box: dict[str, float]) -> bool:
    if not rings:
        return False
    for ring in rings:
        if _line_intersects_box(ring, box):
            return True
    corners = (
        (box["west"], box["south"]),
        (box["west"], box["north"]),
        (box["east"], box["north"]),
        (box["east"], box["south"]),
    )
    return any(_point_in_polygon(corner, rings) for corner in corners)


def _iter_points(coords):
    if (isinstance(coords, (list, tuple)) and len(coords) >= 2 and
            isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float))):
        yield coords
        return
    if isinstance(coords, (list, tuple)):
        for child in coords:
            yield from _iter_points(child)


def _geometry_bounds(geometry: dict | None):
    if not geometry:
        return None
    if geometry.get("type") == "GeometryCollection":
        bounds = [_geometry_bounds(g) for g in geometry.get("geometries", [])]
        bounds = [b for b in bounds if b is not None]
        if not bounds:
            return None
        return (
            min(b[0] for b in bounds), min(b[1] for b in bounds),
            max(b[2] for b in bounds), max(b[3] for b in bounds),
        )
    iterator = _iter_points(geometry.get("coordinates"))
    try:
        first = next(iterator)
    except StopIteration:
        return None
    min_x = max_x = float(first[0])
    min_y = max_y = float(first[1])
    for point in iterator:
        x, y = float(point[0]), float(point[1])
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
    return min_x, min_y, max_x, max_y


def geometry_intersects_box(geometry: dict | None, box: dict[str, float]) -> bool:
    if not geometry:
        return False
    bounds = _geometry_bounds(geometry)
    if bounds is None:
        return False
    min_x, min_y, max_x, max_y = bounds
    if max_x < box["west"] or min_x > box["east"] or max_y < box["south"] or min_y > box["north"]:
        return False
    if (box["west"] <= min_x and max_x <= box["east"] and
            box["south"] <= min_y and max_y <= box["north"]):
        return True
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Point":
        return _point_in_box(coords, box)
    if kind == "MultiPoint":
        return any(_point_in_box(p, box) for p in coords)
    if kind == "LineString":
        return _line_intersects_box(coords, box)
    if kind == "MultiLineString":
        return any(_line_intersects_box(line, box) for line in coords)
    if kind == "Polygon":
        return _polygon_intersects_box(coords, box)
    if kind == "MultiPolygon":
        return any(_polygon_intersects_box(poly, box) for poly in coords)
    if kind == "GeometryCollection":
        return any(geometry_intersects_box(g, box) for g in geometry.get("geometries", []))
    raise ValueError(f"Unsupported GeoJSON geometry type: {kind!r}")


def _iter_feature_slices(raw: bytes, array_start: int) -> tuple[list[tuple[int, int]], int]:
    """Return raw [start,end) spans for qgis2web's compact Feature objects.

    qgis2web serializes each feature with the literal prefix {"type":"Feature",.
    bytes.find performs the large-file scan in C. Each candidate span is still
    JSON-decoded later, so malformed/unexpected source structure fails loudly.
    """
    feature_start = b'{"type":"Feature",'
    array_close = raw.rfind(b']')
    if array_close < array_start:
        raise ValueError("Features array closing bracket not found")
    starts: list[int] = []
    pos = array_start
    while True:
        pos = raw.find(feature_start, pos, array_close)
        if pos < 0:
            break
        starts.append(pos)
        pos += len(feature_start)
    if not starts:
        # Empty feature collections are valid.
        content = raw[array_start:array_close].strip()
        if content:
            raise ValueError("Could not identify qgis2web feature objects")
        return [], array_close
    spans: list[tuple[int, int]] = []
    for i, start in enumerate(starts):
        if i + 1 < len(starts):
            end = starts[i + 1] - 1  # comma immediately preceding next feature
            if raw[end:end + 1] != b',':
                raise ValueError("Unexpected feature separator; source is not compact qgis2web JSON")
        else:
            end = array_close
        spans.append((start, end))
    return spans, array_close


def filter_js_file(path: Path, box: dict[str, float]) -> tuple[int, int, int, int]:
    raw = path.read_bytes()
    marker = raw.find(FEATURES_MARKER)
    if marker < 0:
        raise ValueError(f"{path}: no {FEATURES_MARKER!r} marker found")
    array_start = marker + len(FEATURES_MARKER)
    spans, array_close = _iter_feature_slices(raw, array_start)

    kept_spans: list[tuple[int, int]] = []
    for start, end in spans:
        feature = json.loads(raw[start:end])
        if geometry_intersects_box(feature.get("geometry"), box):
            kept_spans.append((start, end))

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as out:
        out.write(raw[:array_start])
        for idx, (start, end) in enumerate(kept_spans):
            if idx:
                out.write(b",")
            out.write(raw[start:end])
        out.write(raw[array_close:])
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    return len(spans), len(kept_spans), len(raw), path.stat().st_size


def load_config(config_path: Path) -> tuple[dict[str, float], Path, list[str]]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    box = {k: float(cfg["filter_box"][k]) for k in ("west", "east", "south", "north")}
    if not (box["west"] < box["east"] and box["south"] < box["north"]):
        raise ValueError(f"Invalid filter box: {box}")
    root = config_path.parent.resolve()
    data_dir = (root / cfg.get("data_directory", "data")).resolve()
    try:
        data_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("data_directory must remain inside the web-map root") from exc
    files = list(cfg["files"])
    if not files:
        raise ValueError("Configuration has no files to filter")
    return box, data_dir, files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("filter_geospatial_data_config.json"),
        help="Path to configuration JSON (default: next to this script)",
    )
    args = parser.parse_args()
    config_path = args.config.resolve()
    box, data_dir, files = load_config(config_path)

    print(
        "Filtering whole GeoJSON features that intersect "
        f"west={box['west']}, east={box['east']}, south={box['south']}, north={box['north']}"
    )
    total_before = total_after = 0
    for filename in files:
        path = (data_dir / filename).resolve()
        try:
            path.relative_to(data_dir)
        except ValueError as exc:
            raise ValueError(f"Configured file escapes data directory: {filename}") from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        before, after, bytes_before, bytes_after = filter_js_file(path, box)
        total_before += before
        total_after += after
        print(
            f"{path.relative_to(config_path.parent)}: {before:,} -> {after:,} features; "
            f"{bytes_before:,} -> {bytes_after:,} bytes"
        )
    print(f"Total features: {total_before:,} -> {total_after:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
