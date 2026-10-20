# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Explicit footprint authoring operations; no automatic solver corrections."""

import math

from .layers import geometry_from_rows
from .schema import LAYER_TYPES, ModelError


def square_footprint(points):
    """Least-displacement orthogonal fit, retaining vertex order and centroid.

    Right-angle turns include concave corners. Alternating edges share one of
    two perpendicular axes. For a fixed orientation, the closest fit averages
    each edge's two transverse coordinates. The orientation minimizing that
    squared displacement is the principal axis of the alternating edge tensor.
    Validate the entire input before fitting and the entire result before use.
    """
    count = len(points)
    if count < 4 or not all(math.isfinite(v) for point in points for v in point):
        raise ModelError("Enter at least four vertices with finite X/Y coordinates.")
    edges = [(points[(i + 1) % count][0] - x, points[(i + 1) % count][1] - y)
             for i, (x, y) in enumerate(points)]
    lengths = [math.hypot(x, y) for x, y in edges]
    if not all(length > 0 and math.isfinite(length) for length in lengths):
        raise ModelError("Every edge must have a finite, nonzero length.")
    directions = [(x / length, y / length) for (x, y), length in zip(edges, lengths)]
    dots = [directions[i - 1][0] * x + directions[i - 1][1] * y
            for i, (x, y) in enumerate(directions)]
    if any(abs(dot) > math.sin(math.radians(10)) + 1e-12 for dot in dots):
        raise ModelError("Every corner must be within 10° of 90°.")
    if count % 2:
        raise ModelError("A closed right-angle perimeter needs an even number of corners.")

    def validate(footprint):
        geometry_from_rows([(0, x, y, 0, 0) for x, y in footprint], LAYER_TYPES["building"])

    validate(points)
    if all(abs(dot) <= 1e-12 for dot in dots):
        return list(points)
    # Translate and scale first to avoid map offsets and squared-length overflow.
    origin_x, origin_y = points[0]
    scale = max(lengths)
    local = [((x - origin_x) / scale, (y - origin_y) / scale) for x, y in points]
    xx, xy, yy = [], [], []
    for i, (x, y) in enumerate(edges):
        x, y = x / scale, y / scale
        sign = 1 if i % 2 == 0 else -1
        xx.append(sign * x * x)
        xy.append(sign * x * y)
        yy.append(sign * y * y)
    angle = 0.5 * math.atan2(2 * math.fsum(xy), math.fsum(xx) - math.fsum(yy))
    c, s = math.cos(angle), math.sin(angle)
    rotated = [(x * c + y * s, -x * s + y * c) for x, y in local]
    fitted = []
    for i, (x, y) in enumerate(rotated):
        vertical = (i - 1 if i % 2 == 0 else i + 1) % count
        horizontal = (i + 1 if i % 2 == 0 else i - 1) % count
        fitted.append(((x + rotated[vertical][0]) / 2,
                       (y + rotated[horizontal][1]) / 2))
    result = [(origin_x + scale * (x * c - y * s),
               origin_y + scale * (x * s + y * c)) for x, y in fitted]
    # An extreme fit must not collapse or reverse an edge (changing corner type).
    for i, (x, y) in enumerate(result):
        dx, dy = result[(i + 1) % count][0] - x, result[(i + 1) % count][1] - y
        if dx * directions[i][0] + dy * directions[i][1] <= 0:
            raise ModelError("Squaring would collapse or reverse a building edge.")
    validate(result)
    return result
