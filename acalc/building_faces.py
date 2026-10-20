# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building-local face frames and attached source authoring (meters, no Qt)."""

import json
import math
import uuid

from .schema import ModelError


_EPSILON = 1e-8  # The building engine's XY tolerance, in meters.
# A point is either on its surface (an opening/louvre/panel: host emission, no
# host reflection) or stands clear of it as an ordinary point the host reflects.
# The minimum standoff is Acalc engineering policy, matching the engine.
MIN_STANDOFF_M = 0.05


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (abs(_cross(a, b, p)) <= _EPSILON * math.hypot(b[0] - a[0], b[1] - a[1])
            and min(a[0], b[0]) - _EPSILON <= p[0] <= max(a[0], b[0]) + _EPSILON
            and min(a[1], b[1]) - _EPSILON <= p[1] <= max(a[1], b[1]) + _EPSILON)


def _segments_cross(a, b, c, d):
    u, v, w, x = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    return (((u > 0 > v or v > 0 > u) and (w > 0 > x or x > 0 > w))
            or _on_segment(a, b, c) or _on_segment(a, b, d)
            or _on_segment(c, d, a) or _on_segment(c, d, b))


def _triangulate(points, local_coordinates=False):
    """Return canonical perimeter and triangle indices into authored points."""
    count = len(points)
    area = 0.0
    for i, p in enumerate(points):
        q = points[(i + 1) % count]
        if math.hypot(p[0] - q[0], p[1] - q[1]) <= _EPSILON:
            raise ModelError("The building perimeter has a repeated vertex.")
        area += _cross(points[0], p, q)
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segments_cross(p, q, points[j], points[(j + 1) % count]):
                raise ModelError("The building perimeter crosses or touches itself.")
    if not math.isfinite(area) or abs(area) <= _EPSILON * _EPSILON:
        raise ModelError("The building needs a finite footprint with a nonzero area.")
    perimeter = list(range(count))
    if area < 0:
        perimeter.reverse()
    first = perimeter.index(0) if local_coordinates else min(range(count), key=lambda i: points[perimeter[i]][:2])
    perimeter = perimeter[first:] + perimeter[:first]
    remaining = list(perimeter)
    triangles = []
    def side(a, b, p):
        value = _cross(points[a], points[b], points[p])
        tolerance = _EPSILON * math.dist(points[a][:2], points[b][:2]) if local_coordinates else 0
        return 0 if abs(value) <= tolerance else value

    while len(remaining) > 3:
        for i, b in enumerate(remaining):
            a, c = remaining[i - 1], remaining[(i + 1) % len(remaining)]
            if side(a, b, c) <= 0:
                continue
            if any(side(a, b, p) >= 0
                   and side(b, c, p) >= 0
                   and side(c, a, p) >= 0
                   for p in remaining if p not in (a, b, c)):
                continue
            triangles.append((a, b, c))
            del remaining[i]
            break
        else:
            raise ModelError("The building perimeter has degenerate edges.")
    if side(*remaining) <= 0:
        raise ModelError("The building perimeter has degenerate edges.")
    triangles.append(tuple(remaining))
    return tuple(perimeter), tuple(triangles)


def _normal(points):
    a = points[0]
    for b, c in zip(points[1:-1], points[2:]):
        u = tuple(b[i] - a[i] for i in range(3))
        v = tuple(c[i] - a[i] for i in range(3))
        u_scale, v_scale = max(abs(value) for value in u), max(abs(value) for value in v)
        if not u_scale or not v_scale:
            continue
        u, v = tuple(value / u_scale for value in u), tuple(value / v_scale for value in v)
        normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        length = math.hypot(*normal)
        if length:
            return tuple(value / length for value in normal)
    return (0.0, 0.0, 0.0)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def subtract(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _roof_frame(polygon, normal, right):
    down = (right[1] * normal[2] - right[2] * normal[1],
            right[2] * normal[0] - right[0] * normal[2],
            right[0] * normal[1] - right[1] * normal[0])
    origin = polygon[0]
    left = min(dot(subtract(p, origin), right) for p in polygon)
    top = min(dot(subtract(p, origin), down) for p in polygon)
    origin = tuple(origin[i] + left * right[i] + top * down[i] for i in range(3))
    return dict(polygon=polygon, origin=origin, right=right, down=down)


def _top_edge_axis(polygon, frame):
    # Choose in the supplied face-on view, before aligning it to an edge.
    # Midpoint height defines "top"; leftmost midpoint breaks equal-height ties.
    candidates = []
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        midpoint = tuple((a[j] + b[j]) / 2 - frame['origin'][j] for j in range(3))
        candidates.append((dot(midpoint, frame['down']), dot(midpoint, frame['right']), a, b))
    top = min(item[0] for item in candidates)
    _, _, a, b = min((item for item in candidates if item[0] <= top + _EPSILON),
                     key=lambda item: (item[1], min(item[2:]), max(item[2:])))
    right = subtract(b, a)
    x, y = dot(right, frame['right']), dot(right, frame['down'])
    if x < -_EPSILON or abs(x) <= _EPSILON and y < 0:
        right = tuple(-v for v in right)
    length = math.hypot(*right)
    return tuple(v / length for v in right)


def building_faces(vertices, perimeter, triangles):
    """Stable face IDs use authored vertex indices; frames are viewed outside.

    Walls start at their left roof vertex, X horizontal right and Y down.
    Roof X follows the top edge in the building's first-edge face-on view, with
    perpendicular Y down and the bounding box's top-left as origin. A planar roof is one face;
    nonplanar roofs expose their canonical triangular facets separately.
    """
    count = len(vertices) // 2
    axis = subtract(vertices[1], vertices[0])
    length = math.hypot(*axis[:2])
    building_right = (axis[0] / length, axis[1] / length, 0)
    result = {}
    for i, a in enumerate(perimeter):
        b = perimeter[(i + 1) % count]
        polygon = tuple(vertices[j] for j in (a, b, b + count, a + count))
        vector = subtract(vertices[b], vertices[a])
        width = math.hypot(*vector[:2])
        right = (vector[0] / width, vector[1] / width, 0)
        result[f"wall:{a}:{b}"] = dict(name=f"Wall {a + 1}–{b + 1}", polygon=polygon,
                                      origin=vertices[a + count], right=right, down=(0, 0, -1))
    roof = tuple(vertices[i + count] for i in perimeter)
    normal = _normal(tuple(vertices[i + count] for i in triangles[0]))
    planar = all(abs(dot(subtract(p, roof[0]), normal)) <= _EPSILON for p in roof)
    roofs = [("roof", "Roof", roof)] if planar else [
        ("roof:" + ":".join(map(str, sorted(t))), f"Roof facet {i + 1}", tuple(vertices[j + count] for j in t))
        for i, t in enumerate(triangles)]
    for key, name, polygon in roofs:
        normal = _normal(polygon)
        if normal[2] < 0:
            normal = tuple(-v for v in normal)
        right = (1 - normal[0] ** 2, -normal[0] * normal[1], -normal[0] * normal[2])
        length = math.hypot(*right)
        right = tuple(v / length for v in right)
        legacy = _roof_frame(polygon, normal, right)
        top_edge = _roof_frame(polygon, normal, _top_edge_axis(polygon, legacy))
        projection = dot(building_right, normal)
        right = tuple(building_right[i] - projection * normal[i] for i in range(3))
        length = math.hypot(*right)
        right = tuple(value / length for value in right)
        local = _roof_frame(polygon, normal, right)
        result[key] = dict(name=name, legacy=legacy, top_edge=top_edge,
                           **_roof_frame(polygon, normal, _top_edge_axis(polygon, local)))
    frames = list(result.values()) + [face[key] for face in result.values()
                                     for key in ('legacy', 'top_edge') if key in face]
    for face in frames:
        face["uv"] = tuple((dot(subtract(p, face["origin"]), face["right"]),
                            dot(subtract(p, face["origin"]), face["down"])) for p in face["polygon"])
        face["width"] = max(p[0] for p in face["uv"])
        face["height"] = max(p[1] for p in face["uv"])
    return result


def read_face_sources(raw):
    if raw is None or raw == "":
        return []
    try:
        sources = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(sources, list):
            raise ValueError()
        seen = set()
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError()
            point = source.get("type") == "point"
            fields = {"id", "name", "face", "vertex_count", "x_m", "y_m", "spectrum_id"}
            fields |= {"type", "distance_m"} if point else {"width_m", "height_m"}
            if set(source) - {"frame", "mounting"} != fields or (not point and "mounting" in source):
                raise ValueError()
            if str(uuid.UUID(source["id"])) != source["id"] or source["id"] in seen:
                raise ValueError()
            seen.add(source["id"])
            if not isinstance(source["name"], str) or not source["name"].strip():
                raise ValueError()
            if type(source["vertex_count"]) is not int or source["vertex_count"] < 3:
                raise ValueError()
            if not isinstance(source["face"], str) or not source["face"].startswith(("wall:", "roof")):
                raise ValueError()
            if "frame" in source and (source["frame"] not in ("roof_top_edge", "building_local") or not source["face"].startswith("roof")):
                raise ValueError()
            if source["spectrum_id"] is not None and str(uuid.UUID(source["spectrum_id"])) != source["spectrum_id"]:
                raise ValueError()
            for key in (("x_m", "y_m", "distance_m") if point else ("x_m", "y_m", "width_m", "height_m")):
                value = source[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError()
            if not point and (source["width_m"] <= 0 or source["height_m"] <= 0):
                raise ValueError()
            if point and "mounting" in source and (
                    source["mounting"] not in ("surface", "standoff")
                    or (source["mounting"] == "surface") != (source["distance_m"] == 0)
                    or source["mounting"] == "standoff" and source["distance_m"] < MIN_STANDOFF_M):
                raise ValueError()
        return sources
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ModelError(f"Invalid building face sources: use named points or rectangles with finite nonnegative meter offsets, positive rectangle dimensions, valid spectrum references, and surface points at zero distance or standoff points at least {MIN_STANDOFF_M:g} m from the surface.") from error


def point_mounting(source):
    """Return a point's declared mounting: surface or standoff.

    Schema-13 upgrades declare every legacy point whose distance had one
    meaning. A legacy displacement below the minimum standoff stays undeclared
    and must be reviewed rather than silently moved onto or away from the surface.
    """
    mounting = source.get("mounting")
    if mounting is None:
        raise ModelError(f"{source['name']}: declare this building-mounted point source as a surface point "
                         f"or a standoff point at least {MIN_STANDOFF_M:g} m from its surface.")
    if mounting == "standoff" and source["distance_m"] < MIN_STANDOFF_M:
        raise ModelError(f"{source['name']}: a standoff point source must be at least {MIN_STANDOFF_M:g} m from "
                         "its surface; set it on the surface or increase its distance.")
    return mounting


def face_point(face, x, y):
    return tuple(face["origin"][i] + x * face["right"][i] + y * face["down"][i] for i in range(3))


def faces_from_rows(rows, horizontal_scale):
    """Resolved stored rows to local meter frames, for layer validation."""
    if len(rows) < 3 or any(row[0] != 0 for row in rows):
        raise ModelError("Building sources need a single perimeter with at least three vertices.")
    anchor = rows[0]
    base = tuple(((x - anchor[1]) * horizontal_scale, (y - anchor[2]) * horizontal_scale, z - anchor[3])
                 for _, x, y, z, _ in rows)
    roof = tuple((p[0], p[1], p[2] + row[4]) for p, row in zip(base, rows))
    perimeter, triangles = _triangulate(base, local_coordinates=True)
    return building_faces(base + roof, perimeter, triangles)


def source_face(source, faces):
    """Resolve versioned roof frames without reinterpreting saved offsets."""
    face = faces.get(source["face"])
    if face is not None:
        key = {None: 'legacy', 'roof_top_edge': 'top_edge'}.get(source.get('frame'))
        if key in face:
            face = dict(face[key], name=face['name'])
    return face


def source_polygon(source, faces, vertex_count):
    """Return a point or rectangle; validate its footprint against the face."""
    face = source_face(source, faces)
    if face is None or source["vertex_count"] != vertex_count:
        raise ModelError(f"{source['name']}: the attached face changed. Remove or reposition its sources before changing the perimeter topology.")
    x, y = source['x_m'], source['y_m']
    boundary = face["uv"]

    def inside(p):
        result = False
        for i, a in enumerate(boundary):
            b = boundary[(i + 1) % len(boundary)]
            if _on_segment(a, b, p):
                return True
            if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                result = not result
        return result

    if source.get('type') == 'point':
        point_mounting(source)
        if not inside((x, y)):
            raise ModelError(f"{source['name']}: the source position must lie inside {face['name']}.")
        # Outside-view X right / Y down: down × right points out of the solid.
        r, d = face['right'], face['down']
        normal = (d[1]*r[2] - d[2]*r[1], d[2]*r[0] - d[0]*r[2], d[0]*r[1] - d[1]*r[0])
        point = face_point(face, x, y)
        return (tuple(point[i] + source['distance_m'] * normal[i] for i in range(3)),)

    w, h = source['width_m'], source['height_m']
    rectangle = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    # Split rectangle edges at every face boundary intersection, then test all
    # intervals. Corner-only containment misses a concave notch crossing an edge.
    valid = all(inside(p) for p in rectangle)
    for i, a in enumerate(rectangle):
        b = rectangle[(i + 1) % 4]
        ts = [0, 1]
        for j, c in enumerate(boundary):
            d = boundary[(j + 1) % len(boundary)]
            ab, cd, ac = subtract(b, a), subtract(d, c), subtract(c, a)
            cross = lambda u, v: u[0] * v[1] - u[1] * v[0]
            denominator = cross(ab, cd)
            if abs(denominator) > 1e-15:
                t, u = cross(ac, cd) / denominator, cross(ac, ab) / denominator
                if 0 <= t <= 1 and 0 <= u <= 1:
                    ts.append(t)
        ts.sort()
        valid = valid and all(inside((a[0] + (b[0] - a[0]) * (t + u) / 2,
                                      a[1] + (b[1] - a[1]) * (t + u) / 2)) for t, u in zip(ts, ts[1:]))
    if not valid:
        raise ModelError(f"{source['name']}: the source rectangle must fit entirely inside {face['name']}.")
    return tuple(face_point(face, *p) for p in rectangle)
