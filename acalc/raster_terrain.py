# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Adaptive raster-to-TIN authoring; generated geometry is ordinary model terrain.

No raster is read by the solver. GEOS triangulates in local XY coordinates;
maximum residuals at native pixel centres and sampled box edges drive refinement.
The final mesh edges are saved as constraints so another triangulator cannot
silently change the checked surface (notably at cocircular raster points).
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import QgsGeometry, QgsPointXY

from .raster_elevation import RasterElevation
from .schema import ModelError, new_id

MAX_CANDIDATES = 1_000_000
MAX_VERTICES = 20_000
ALGORITHM = "adaptive-geos-tin-v1"


class GenerationCancelled(Exception):
    pass


def checkpoint(progress, message):
    if progress and progress(message) is False:
        raise GenerationCancelled()


def validate_options(bounds, tolerance):
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ModelError("Draw a nonempty terrain box on the map.")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ModelError("Maximum elevation deviation must be greater than zero.")


def raster_candidates(record, model_wkt, bounds, progress=None):
    """Read native centres in the box, plus its perimeter at <= half-pixel steps.

    Reject missing interior pixels and invalid boundary interpolation; holes must
    never silently become filled triangles. Boundary sampling uses the same
    native bilinear interpolation, units and scale/offset as object authoring.
    """
    xmin, ymin, xmax, ymax = bounds
    corners = np.array([[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]])
    with RasterElevation(record["path"], model_wkt, record["band"]) as raster:
        source = osr.SpatialReference(wkt=raster.dataset.GetProjection())
        target = osr.SpatialReference(wkt=model_wkt)
        for srs in (source, target):
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        forward = osr.CoordinateTransformation(source, target)
        affine = raster.dataset.GetGeoTransform()
        perimeter = []
        pixel_outline = []
        # Piecewise transformed boundary, not just four transformed corners.
        for a, b in zip(corners, np.roll(corners, -1, axis=0)):
            anchors = np.linspace(a, b, 33)
            transformed = raster.transform.TransformPoints(anchors.tolist())
            pixels = np.array([gdal.ApplyGeoTransform(raster.inverse, x, y) for x, y, *_ in transformed])
            if not np.isfinite(pixels).all():
                raise ModelError("Could not transform the terrain box into the raster CRS.")
            pixel_outline.extend(pixels)
            for i in range(32):
                steps = max(1, math.ceil(float(np.max(np.abs(pixels[i + 1] - pixels[i]))) * 2))
                if len(perimeter) + steps > MAX_CANDIDATES:
                    raise ModelError("This box is too large. Draw a smaller terrain area.")
                perimeter.extend(np.linspace(anchors[i], anchors[i + 1], steps, endpoint=False).tolist())
        checkpoint(progress, "Reading the terrain boundary…")
        boundary = []
        for i, (x, y) in enumerate(perimeter):
            if i % 256 == 0:
                checkpoint(progress, "Checking raster coverage along the box…")
            boundary.append((x, y, raster.elevation(x, y, strict=True)))
        pixels = np.array(pixel_outline)
        lo = np.floor(pixels.min(axis=0)).astype(int) - 2
        hi = np.ceil(pixels.max(axis=0)).astype(int) + 2
        x0, y0 = max(0, int(lo[0])), max(0, int(lo[1]))
        x1, y1 = min(raster.dataset.RasterXSize, int(hi[0])), min(raster.dataset.RasterYSize, int(hi[1]))
        if x1 <= x0 or y1 <= y0:
            raise ModelError("The terrain box is outside the elevation raster.")
        # Bound allocations before loading a window. Highly rotated/reprojected
        # boxes can have a larger source window than their actual selected area.
        if (x1 - x0) * (y1 - y0) > MAX_CANDIDATES * 4:
            raise ModelError("This box covers too many raster pixels. Draw a smaller terrain area.")
        chunks = [np.asarray(boundary, dtype=float)]
        count = len(boundary)
        digest = hashlib.sha256()
        digest.update(np.asarray(boundary, dtype='<f8').tobytes())
        for row in range(y0, y1, 64):
            checkpoint(progress, f"Reading raster elevations ({row - y0:,} of {y1 - y0:,} rows)…")
            height = min(64, y1 - row)
            raw = raster.band.ReadAsArray(x0, row, x1 - x0, height)
            mask = raster.mask.ReadAsArray(x0, row, x1 - x0, height)
            if raw is None or mask is None:
                raise ModelError("Could not read the selected raster area.")
            yy, xx = np.mgrid[row:row + height, x0:x1].astype(float)
            xx += .5
            yy += .5
            rx = affine[0] + xx * affine[1] + yy * affine[2]
            ry = affine[3] + xx * affine[4] + yy * affine[5]
            xy = np.asarray(forward.TransformPoints(np.column_stack((rx.ravel(), ry.ravel())).tolist()))[:, :2]
            if not np.isfinite(xy).all():
                raise ModelError("Could not transform raster pixels into the model CRS.")
            inside = ((xy[:, 0] >= xmin) & (xy[:, 0] <= xmax)
                      & (xy[:, 1] >= ymin) & (xy[:, 1] <= ymax))
            values = np.asarray(raw, dtype=float).ravel()
            valid = (mask.ravel() != 0) & np.isfinite(values)
            if raster.nodata is not None:
                valid &= values != raster.nodata
            if np.any(inside & ~valid):
                raise ModelError("The terrain box contains missing elevation data (NoData). Draw a box with complete coverage.")
            zs = (values[inside] * raster.scale + raster.offset) * raster.vertical_scale
            if not np.isfinite(zs).all():
                raise ModelError("The raster contains invalid scaled elevations.")
            chunk = np.column_stack((xy[inside], zs))
            count += len(chunk)
            if count > MAX_CANDIDATES:
                raise ModelError("The terrain box exceeds 1,000,000 elevation samples. Draw a smaller area.")
            digest.update(np.asarray(chunk, dtype='<f8').tobytes())
            chunks.append(chunk)
        points = np.concatenate(chunks)
        _, indices = np.unique(points[:, :2], axis=0, return_index=True)
        points = points[np.sort(indices)]
        return points, dict(source_sample_sha256=digest.hexdigest(),
                            raster_crs_wkt=source.ExportToWkt(), raster_geotransform=list(affine),
                            raster_vertical_unit=raster.band.GetUnitType() or "m",
                            raster_scale=raster.scale, raster_offset=raster.offset)


class CandidateIndex:
    """Uniform XY bins avoid scanning the full raster for each triangle."""
    def __init__(self, xy):
        self.xy = xy
        self.size = max(1, min(256, int(math.sqrt(len(xy) / 128))))
        cells = np.minimum(self.size - 1, np.maximum(0, (xy * self.size).astype(int)))
        keys = cells[:, 1] * self.size + cells[:, 0]
        order = np.argsort(keys, kind='stable')
        offsets = np.searchsorted(keys[order], np.arange(self.size * self.size + 1))
        self.bins = [order[a:b] for a, b in zip(offsets[:-1], offsets[1:])]

    def query(self, triangle):
        lo = np.maximum(0, np.floor(triangle.min(axis=0) * self.size).astype(int))
        hi = np.minimum(self.size - 1, np.floor(triangle.max(axis=0) * self.size).astype(int))
        return np.concatenate([self.bins[y * self.size + x]
                               for y in range(lo[1], hi[1] + 1) for x in range(lo[0], hi[0] + 1)])


@dataclass
class TerrainMesh:
    vertices: np.ndarray
    triangles: list
    candidate_count: int
    max_error: float
    metadata: dict

    def lines(self):
        """Walk all mesh edges, chunked below the API's 10,000-vertex limit.

        A depth-first walk traverses each edge in both directions. Duplicate
        constraints are deduplicated by the terrain engine. No connector ever
        crosses a triangle, unlike concatenating independent triangle rings.
        """
        edges = sorted({tuple(sorted((t[i], t[(i + 1) % 3]))) for t in self.triangles for i in range(3)})
        adjacent = [[] for _ in self.vertices]
        for a, b in edges:
            adjacent[a].append(b)
            adjacent[b].append(a)
        used, stack, walk = set(), [edges[0][0]], [edges[0][0]]
        while stack:
            a = stack[-1]
            if adjacent[a]:
                b = adjacent[a].pop()
                edge = (min(a, b), max(a, b))
                if edge in used:
                    continue
                used.add(edge)
                stack.append(b)
                walk.append(b)
            else:
                stack.pop()
                if stack:
                    walk.append(stack[-1])
        for start in range(0, len(walk) - 1, 999):
            yield self.vertices[walk[start:start + 1000]]


def simplify(points, bounds, tolerance, progress=None, max_vertices=MAX_VERTICES):
    """Greedy insertion of the worst residuals, in batches of up to 128.

    Each pass checks the new triangulation. Unchanged triangles reuse their
    residuals. The tolerance describes checked samples, not continuous DEM or
    acoustic error. Resource limits fail explicitly without publishing a mesh.
    """
    validate_options(bounds, tolerance)
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ModelError("Terrain samples must contain finite X, Y and Z values.")
    xmin, ymin, xmax, ymax = bounds
    origin = np.array([xmin, ymin])
    span = max(xmax - xmin, ymax - ymin)
    # Translate only for triangulation: dividing an integer raster lattice by
    # an arbitrary span can turn exactly collinear points into numerical slivers.
    xy = points[:, :2] - origin
    lookup = {tuple(p): i for i, p in enumerate(xy)}
    selected = set()
    for corner in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)):
        key = tuple(np.asarray(corner) - origin)
        if key not in lookup:
            raise ModelError("The terrain samples do not cover the complete box.")
        selected.add(lookup[key])
    index = CandidateIndex(xy / span)
    cache = {}
    while True:
        checkpoint(progress, f"Refining terrain: {len(selected):,} vertices…")
        ids = sorted(selected)
        geometry = QgsGeometry.fromMultiPointXY([QgsPointXY(*xy[i]) for i in ids]).delaunayTriangulation()
        if geometry.isEmpty():
            raise ModelError("Could not triangulate the selected terrain area.")
        triangles = [tuple(lookup[(p.x(), p.y())] for p in polygon.asPolygon()[0][:3]) for polygon in geometry.asGeometryCollection()]
        if not triangles:
            raise ModelError("The terrain area did not produce any triangles.")
        new_cache, additions, max_error = {}, {}, 0.0
        for number, tri in enumerate(triangles):
            if number % 256 == 0:
                checkpoint(progress, f"Checking terrain error: {len(selected):,} vertices, {number:,}/{len(triangles):,} triangles…")
            key = tuple(sorted(tri))
            if key in cache:
                error, worst = cache[key]
            else:
                a, b, c = xy[list(tri)]
                candidates = index.query(np.array([a, b, c]) / span)
                delta = xy[candidates] - a
                u, v = b - a, c - a
                determinant = u[0] * v[1] - u[1] * v[0]
                if abs(determinant) < 1e-24:
                    raise ModelError("The terrain contains a degenerate triangle. Use a less narrow box.")
                w1 = (delta[:, 0] * v[1] - delta[:, 1] * v[0]) / determinant
                w2 = (u[0] * delta[:, 1] - u[1] * delta[:, 0]) / determinant
                inside = (w1 >= -1e-12) & (w2 >= -1e-12) & (w1 + w2 <= 1 + 1e-12)
                candidates, w1, w2 = candidates[inside], w1[inside], w2[inside]
                z0, z1, z2 = points[list(tri), 2]
                residuals = np.abs(points[candidates, 2] - (z0 + w1 * (z1 - z0) + w2 * (z2 - z0)))
                at = int(np.argmax(residuals)) if len(residuals) else None
                error, worst = (float(residuals[at]), int(candidates[at])) if at is not None else (0.0, None)
            new_cache[key] = (error, worst)
            max_error = max(max_error, error)
            if error > tolerance and worst not in selected:
                additions[worst] = max(error, additions.get(worst, 0))
        if max_error <= tolerance:
            mapping = {original: i for i, original in enumerate(ids)}
            return TerrainMesh(points[ids], [tuple(mapping[i] for i in t) for t in triangles], len(points), max_error, {})
        if not additions:
            raise ModelError("The requested tolerance is below numerical precision for this terrain.")
        room = max_vertices - len(selected)
        if room <= 0:
            raise ModelError(f"Terrain needs more than {max_vertices:,} vertices (remaining error {max_error:.3g} m). Increase the tolerance or draw a smaller box.")
        selected.update(sorted(additions, key=lambda i: (-additions[i], i))[:min(128, room)])
        cache = new_cache


def generate(record, model_wkt, bounds, tolerance, progress=None):
    validate_options(bounds, tolerance)
    points, provenance = raster_candidates(record, model_wkt, bounds, progress)
    mesh = simplify(points, bounds, tolerance, progress)
    mesh.metadata = dict(provenance, algorithm=ALGORITHM, source_path=str(Path(record['path']).resolve()),
                         source_band=record['band'], bounds=list(bounds), model_crs_wkt=model_wkt,
                         tolerance_m=tolerance, max_error_m=mesh.max_error,
                         candidate_count=mesh.candidate_count, vertex_count=len(mesh.vertices),
                         triangle_count=len(mesh.triangles))
    return mesh


def save_terrain(store, name, mesh):
    """Publish a fully generated layer and its provenance together."""
    payload = json.dumps(mesh.metadata, allow_nan=False)

    def populate(layer):
        if layer.StartTransaction() != 0:
            raise ModelError("Could not start the terrain import transaction.")
        try:
            for number, points in enumerate(mesh.lines(), 1):
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetField('object_id', new_id())
                feature.SetField('name', f'Terrain mesh {number}')
                feature.SetField('enabled', 1)
                feature.SetField('ground_mode', 'per_point')
                geometry = ogr.Geometry(ogr.wkbLineString25D)
                for x, y, z in points:
                    geometry.AddPoint(float(x), float(y), float(z))
                feature.SetGeometry(geometry)
                if layer.CreateFeature(feature) != 0:
                    raise ModelError("Could not write the generated terrain.")
                feature = geometry = None
            if layer.CommitTransaction() != 0:
                raise ModelError("Could not commit the generated terrain.")
        except Exception:
            feature = geometry = None
            layer.RollbackTransaction()
            raise

    def register(db, layer_id):
        db.execute('''CREATE TABLE IF NOT EXISTS acalc_raster_terrain (
            layer_id TEXT PRIMARY KEY REFERENCES acalc_layers(layer_id),
            raster_id TEXT REFERENCES acalc_elevation_rasters(raster_id) ON DELETE SET NULL,
            generation_json TEXT NOT NULL)''')
        db.execute("""INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description)
            VALUES ('acalc_raster_terrain','attributes','acalc_raster_terrain','Raster terrain generation provenance')""")
        # Keep the nullable legacy source column for compatibility; imports own their provenance.
        db.execute('INSERT INTO acalc_raster_terrain (layer_id,generation_json) VALUES (?,?)', (layer_id, payload))

    return store.add_layer('terrain_line', name, populate=populate, register=register)
