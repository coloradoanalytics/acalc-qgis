# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Area result objects, engine-delivered GeoTIFFs and legacy raster projections.

Current runs keep each area's calculated grid as the engine's GeoTIFF, saved
beside the model in ``<model>_results/<run_id>/``. Older runs stored every
sample in the GeoPackage; their disposable projections are rebuilt from those
samples. Either way pixels are centred on samples; missing/excluded samples
are NoData, never interpolated values.
"""

import json
import math
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from osgeo import gdal, ogr

from .schema import ModelError


NODATA = -3.4028234663852886e38
BANDS = (63, 125, 250, 500, 1000, 2000, 4000, 8000)
BAND_NAMES = ("dBA", *(f"{hz} Hz" for hz in BANDS))
COLORS = ("#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8",
          "#ffffbf", "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026")


def geometry_from_snapshot(value):
    blob = bytes.fromhex(value)
    if len(blob) < 8 or blob[:3] != b"GP\x00" or (blob[3] >> 1 & 7) > 4:
        raise ModelError("Invalid saved area result geometry.")
    offset = 8 + (0, 32, 48, 48, 64)[blob[3] >> 1 & 7]
    geometry = ogr.CreateGeometryFromWkb(blob[offset:])
    if geometry is None or geometry.IsEmpty():
        raise ModelError("Invalid saved area result geometry.")
    return geometry


def results_directory(store):
    """The model's sibling folder of engine-delivered result rasters."""
    path = Path(store.path)
    return path.with_name(path.stem + "_results")


def raster_file(store, relative):
    """Resolve a saved raster reference, refusing paths outside the results folder."""
    if not isinstance(relative, str) or not relative:
        raise ModelError("Invalid saved area raster reference.")
    root = results_directory(store).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ModelError("Invalid saved area raster reference.")
    return path


def raster_file_name(name, object_id):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")[:40] or "area"
    return f"{stem}_{uuid.UUID(object_id).hex[:8]}.tif"


def remove_run_rasters(store, run_ids):
    """Best effort: a raster still open elsewhere (Windows) is pruned later."""
    root = results_directory(store)
    for run_id in run_ids:
        try:
            directory = root / str(uuid.UUID(run_id))
        except (ValueError, TypeError, AttributeError):
            continue
        shutil.rmtree(directory, ignore_errors=True)


def prune_result_rasters(store, keep_run_ids):
    """Remove owned run folders (UUID names only) no saved run references."""
    root = results_directory(store)
    if not root.is_dir():
        return
    keep = set(keep_run_ids)
    for child in root.iterdir():
        try:
            owned = child.is_dir() and str(uuid.UUID(child.name)) == child.name
        except ValueError:
            owned = False
        if owned and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


def grouped_results(store, run, results):
    """One display object per area, including empty areas snapshotted by the engine."""
    areas, points = {}, []
    for snapshot in run.get("metadata", {}).get("area_receivers") or []:
        key = (snapshot["layer_id"], snapshot["object_id"])
        areas[key] = _area(*key, snapshot["name"], snapshot)
    for raster in run.get("metadata", {}).get("area_rasters") or []:
        key = (raster["layer_id"], raster["object_id"])
        if key in areas:
            areas[key]["raster"] = dict(raster, file=str(raster_file(store, raster["path"])))
    for result in results:
        parent = result["metadata"].get("area_receiver_id")
        if parent:
            key = (result["layer_id"], parent)
            if key not in areas:
                areas[key] = _area(*key, result["receiver_name"].rsplit(" [", 1)[0], None)
            areas[key]["samples"].append(result)
        else:
            points.append(result)
    # Legacy runs did not preserve the boundary/spacing. Recover only from an
    # unchanged model; stale results must never borrow edited input geometry.
    if not run["stale"]:
        layers = {r["layer_id"]: r for r in store.layers()}
        with store.connection(readonly=True) as db:
            for (layer_id, object_id), area in areas.items():
                layer = layers.get(layer_id)
                if area["snapshot"] is None and layer and layer["feature_type"] == "area_receiver":
                    table = layer["table_name"].replace('"', '""')
                    row = db.execute(f'SELECT name,geom,height_m,spacing_m FROM "{table}" WHERE object_id=?', (object_id,)).fetchone()
                    if row:
                        area["snapshot"] = dict(name=row[0], geometry_gpkg_hex=row[1].hex(), height_m=row[2], spacing_m=row[3])
                        area["receiver_name"] = row[0]
    for area in areas.values():
        area["coordinate_reference"] = run.get("metadata", {}).get("coordinate_reference") or store.metadata()
    return points + sorted(areas.values(), key=lambda area: (area["layer_id"], area["receiver_name"].casefold(), area["object_id"]))


def _area(layer_id, object_id, name, snapshot):
    return dict(layer_id=layer_id, object_id=object_id, receiver_name=name, is_area=True,
                samples=[], snapshot=snapshot, broadband_a_db=None, octave_levels=[None]*8)


def area_table_name(variant_id, layer_id, object_id):
    return "acalc_area_" + "_".join(uuid.UUID(value).hex for value in (variant_id, layer_id, object_id))


class AreaSurface:
    def __init__(self, area, horizontal_scale):
        self.area = area
        horizontal_scale = area.get("coordinate_reference", {}).get("horizontal_to_m", horizontal_scale)
        self.samples = area["samples"]
        self.boundary = None
        self.spacing = None
        snapshot = area["snapshot"]
        if area.get("raster"):
            self._load_raster(area["raster"], snapshot)
            return
        self.positions = np.array([(geometry.GetX(), geometry.GetY()) for geometry in
                                   (geometry_from_snapshot(r["metadata"]["geometry_gpkg_hex"]) for r in self.samples)], dtype=float).reshape((-1, 2))
        if snapshot:
            self.boundary = geometry_from_snapshot(snapshot["geometry_gpkg_hex"])
            self.spacing = float(snapshot["spacing_m"]) / horizontal_scale
            xmin, xmax, ymin, ymax = self.boundary.GetEnvelope()
        elif self.samples:
            # Grid indices let multi-point legacy results recover spacing.
            for axis, field in enumerate(("grid_column", "grid_row")):
                first = self.samples[0]["metadata"][field]
                for i, result in enumerate(self.samples[1:], 1):
                    delta = result["metadata"][field] - first
                    if delta:
                        self.spacing = abs((self.positions[i, axis] - self.positions[0, axis]) / delta)
                        break
                if self.spacing:
                    break
            if not self.spacing:
                raise ModelError("Recalculate this legacy area to save its grid spacing and boundary.")
            xmin, ymin = self.positions.min(axis=0)
            xmax, ymax = self.positions.max(axis=0)
        else:
            raise ModelError("This area has no saved grid geometry. Recalculate to create its surface.")
        if not math.isfinite(self.spacing) or self.spacing <= 0:
            raise ModelError("Invalid saved area grid spacing.")
        self.columns = int(math.floor((xmax-xmin)/self.spacing + 1e-7)) + 1
        self.rows = int(math.floor((ymax-ymin)/self.spacing + 1e-7)) + 1
        if self.columns < 1 or self.rows < 1 or self.columns*self.rows > 1000000:
            raise ModelError("Saved area grid exceeds the supported size.")
        self.origin = (xmin, ymin)
        self.transform = (xmin-self.spacing/2, self.spacing, 0,
                          ymin+(self.rows-0.5)*self.spacing, 0, -self.spacing)
        self.band_values = np.full((9, self.rows, self.columns), NODATA, dtype=np.float32)
        self.values = self.band_values[0]  # The default display remains dBA.
        self.sample_count = len(self.samples)
        for position, result in zip(self.positions, self.samples):
            column, row = np.rint((position-np.array(self.origin))/self.spacing).astype(int)
            if not (0 <= column < self.columns and 0 <= row < self.rows):
                raise ModelError("Saved area sample lies outside its grid.")
            for band, level in enumerate((result["broadband_a_db"], *result["octave_levels"])):
                if level is not None:
                    self.band_values[band, self.rows-1-row, column] = level

    def _load_raster(self, raster, snapshot):
        """Read the saved engine GeoTIFF; pixels, not rows, hold the samples."""
        dataset = gdal.Open(raster["file"])
        if dataset is None or dataset.RasterCount != 9:
            raise ModelError("Could not read the saved area raster. Recalculate to restore it.")
        self.file = raster["file"]
        self.columns, self.rows = dataset.RasterXSize, dataset.RasterYSize
        self.transform = dataset.GetGeoTransform()
        self.spacing = self.transform[1]
        self.origin = (self.transform[0] + self.spacing/2, self.transform[3] - (self.rows-0.5)*self.spacing)
        self.band_values = dataset.ReadAsArray().astype(np.float32).reshape((9, self.rows, self.columns))
        dataset = None
        self.values = self.band_values[0]
        if snapshot:
            self.boundary = geometry_from_snapshot(snapshot["geometry_gpkg_hex"])
        self.sample_count = int(raster.get("receiver_count", 0))
        self.positions = np.empty((0, 2))

    def sample_at(self, x, y):
        """The calculated point nearest (x, y) as (x, y, result), or None."""
        if not hasattr(self, "file"):
            index = self.nearest(x, y)
            return None if index is None else (*self.positions[index], self.samples[index])
        if self.boundary:
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint_2D(x, y)
            if not self.boundary.Intersects(point):
                return None
        column = int(math.floor((x - self.transform[0]) / self.spacing))
        pixel_row = int(math.floor((self.transform[3] - y) / self.spacing))
        if not (0 <= column < self.columns and 0 <= pixel_row < self.rows):
            return None
        levels = [None if value == NODATA else float(value) for value in self.band_values[:, pixel_row, column]]
        if all(level is None for level in levels):
            return None  # Excluded, outside the grid, or zero energy in every band.
        row = self.rows - 1 - pixel_row
        result = dict(broadband_a_db=levels[0], octave_levels=levels[1:],
                      metadata=dict(grid_column=column, grid_row=row))
        return self.origin[0] + column*self.spacing, self.origin[1] + row*self.spacing, result

    def nearest(self, x, y):
        if not len(self.positions):
            return None
        if self.boundary:
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint_2D(x, y)
            if not self.boundary.Intersects(point):
                return None
        elif not (self.transform[0] <= x <= self.transform[0]+self.columns*self.spacing and
                  self.transform[3]-self.rows*self.spacing <= y <= self.transform[3]):
            return None
        distances = np.square(self.positions[:, 0]-x) + np.square(self.positions[:, 1]-y)
        return int(np.argmin(distances))

    def dataset(self, wkt, band_index=None):
        indices = range(9) if band_index is None else [band_index]
        dataset = gdal.GetDriverByName("MEM").Create("", self.columns, self.rows, len(indices), gdal.GDT_Float32)
        dataset.SetGeoTransform(self.transform)
        dataset.SetProjection(wkt)
        for output_index, index in enumerate(indices, 1):
            band = dataset.GetRasterBand(output_index)
            band.SetNoDataValue(NODATA)
            band.SetDescription(BAND_NAMES[index])
            band.SetUnitType("dBA" if index == 0 else "dB")
            band.WriteArray(self.band_values[index])
        return dataset


def write_projection(db, store, record, surface):
    """Generate with GDAL, then copy its tiles/metadata in the caller's transaction.

    This keeps replacement atomic and never gives GDAL a writable input model.
    """
    # GeoPackage float coverage tables are single-band. Store nine owned
    # coverages together and expose them as one inline VRT (no sidecar files).
    for index in range(9):
        _write_band_projection(db, store, record, surface, index)


def band_table_names(table):
    return [table, *(f"{table}_{hz}" for hz in BANDS)]


def owned_band_tables(db, table):
    """Find only this area's octave coverages; never claim unrelated tables."""
    tables = []
    for hz, child in zip(BANDS, band_table_names(table)[1:]):
        row = db.execute("SELECT description FROM gpkg_contents WHERE table_name=?", (child,)).fetchone()
        try:
            if row and json.loads(row[0]) == {"acalc_area_band": 1, "parent_table": table, "frequency_hz": hz}:
                tables.append(child)
        except (ValueError, TypeError):
            pass
    return tables


def _write_band_projection(db, store, record, surface, index):
    table = band_table_names(record["table_name"])[index]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        raise ModelError("The area result band table is already used by another dataset.")
    description = ({"acalc_result_layer": 5, **record} if index == 0 else
                   {"acalc_area_band": 1, "parent_table": record["table_name"], "frequency_hz": BANDS[index-1]})
    unit = "dBA" if index == 0 else "dB"
    quantity = "A-weighted sound pressure level" if index == 0 else f"{BANDS[index-1]} Hz unweighted sound pressure level"
    wkt = surface.area.get("coordinate_reference", store.metadata())["crs_wkt"]
    srs = db.execute("SELECT srs_id FROM gpkg_spatial_ref_sys WHERE definition=?", (wkt,)).fetchone()
    if srs is None:
        raise ModelError("The model CRS is missing from its GeoPackage.")
    with tempfile.TemporaryDirectory(prefix="acalc-area-") as directory:
        path = str(Path(directory)/"surface.gpkg")
        dataset = gdal.Translate(path, surface.dataset(wkt, index), format="GPKG", creationOptions=[
            "RASTER_TABLE="+table, "TILE_FORMAT=TIFF", "UOM="+unit, "FIELD_NAME=sound_level",
            "QUANTITY_DEFINITION="+quantity, "PRECISION=0.1"])
        if dataset is None:
            raise ModelError("Could not create the area result raster.")
        dataset = None
        with closing(sqlite3.connect(path)) as source:
            metadata = ("gpkg_tile_matrix_set", "gpkg_tile_matrix", "gpkg_2d_gridded_coverage_ancillary", "gpkg_2d_gridded_tile_ancillary")
            for name in (*metadata, table):
                sql = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
                db.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
            contents = list(source.execute("SELECT * FROM gpkg_contents WHERE table_name=?", (table,)).fetchone())
            contents[2:5] = [table, json.dumps(description, sort_keys=True), record["finished_at"]]
            contents[-1] = srs[0]
            db.execute("INSERT INTO gpkg_contents VALUES(?,?,?,?,?,?,?,?,?,?)", contents)
            for name in (table, *metadata):
                columns = [row[1] for row in source.execute(f'PRAGMA table_info("{name}")')]
                if name in metadata and "id" in columns:
                    columns.remove("id")
                names = ",".join('"'+column+'"' for column in columns)
                rows = [list(row) for row in source.execute(f'SELECT {names} FROM "{name}"')]
                if "srs_id" in columns:
                    for row in rows:
                        row[columns.index("srs_id")] = srs[0]
                db.executemany(f'INSERT INTO "{name}" ({names}) VALUES({",".join("?" for _ in columns)})', rows)
            for row in source.execute("SELECT * FROM gpkg_extensions WHERE extension_name='gpkg_2d_gridded_coverage'"):
                exists = db.execute("SELECT 1 FROM gpkg_extensions WHERE table_name IS ? AND column_name IS ? AND extension_name=?", row[:3]).fetchone()
                if not exists:
                    db.execute("INSERT INTO gpkg_extensions VALUES(?,?,?,?,?)", row)


def style_surface(layer, mode="areas", interval=5, band=None):
    from qgis.PyQt.QtGui import QColor
    from qgis.core import (Qgis, QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer,
                           QgsRasterContourRenderer, QgsLineSymbol, QgsRasterDataProvider)
    opacity = layer.opacity()
    band = int(layer.customProperty("acalc/surface_band", 1)) if band is None else band
    unit = "dBA" if band == 1 else "dB"
    layer.setResamplingStage(Qgis.RasterResamplingStage.Provider)
    provider = layer.dataProvider()
    provider.enableProviderResampling(True)
    # QGIS 3.42 moved this enum from QgsRasterDataProvider to Qgis.
    resampling = getattr(Qgis, "RasterResamplingMethod", None)
    if resampling is None:
        resampling = QgsRasterDataProvider.ResamplingMethod
    provider.setZoomedInResamplingMethod(resampling.Bilinear if mode == "lines" else resampling.Nearest)
    if mode == "lines":
        renderer = QgsRasterContourRenderer(layer.dataProvider())
        renderer.setInputBand(band)
        renderer.setContourInterval(interval)
        renderer.setContourIndexInterval(interval*2)
        renderer.setDownscale(1)
        renderer.setContourSymbol(QgsLineSymbol.createSimple({"line_color": "#50627c", "line_width": "0.35"}))
        renderer.setContourIndexSymbol(QgsLineSymbol.createSimple({"line_color": "#26384f", "line_width": "0.65"}))
    else:
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Discrete)
        levels = list(range(30, 81, 5))
        ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(level, QColor(color), f"≤ {level} {unit}")
                                   for level, color in zip(levels, COLORS)] +
                                [QgsColorRampShader.ColorRampItem(float("inf"), QColor(COLORS[-1]), f"> 80 {unit}")])
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
    renderer.setOpacity(opacity)
    layer.setRenderer(renderer)
    layer.setCustomProperty("acalc/surface_mode", mode)
    layer.setCustomProperty("acalc/surface_band", band)
    layer.triggerRepaint()


def _file_reference(record):
    """Relative GeoTIFF path for an engine-delivered area record, else None."""
    table = record.get("table_name") or ""
    return record.get("raster_file") or (table[5:] if table.startswith("file:") else None)


def raster_source(store, record):
    if _file_reference(record):
        return str(raster_file(store, _file_reference(record)))
    source = f'GPKG:{store.path}:{record["table_name"]}'
    if not record.get("raster_bands"):
        return source
    dataset = gdal.Open(source)
    if dataset is None:
        raise ModelError("Could not read the saved area raster.")
    root = ET.Element("VRTDataset", rasterXSize=str(dataset.RasterXSize), rasterYSize=str(dataset.RasterYSize))
    ET.SubElement(root, "SRS").text = dataset.GetProjection()
    ET.SubElement(root, "GeoTransform").text = ",".join(map(str, dataset.GetGeoTransform()))
    dataset = None
    for index, (table, name) in enumerate(zip(band_table_names(record["table_name"]), BAND_NAMES), 1):
        band = ET.SubElement(root, "VRTRasterBand", dataType="Float32", band=str(index))
        ET.SubElement(band, "Description").text = name
        ET.SubElement(band, "UnitType").text = "dBA" if index == 1 else "dB"
        ET.SubElement(band, "NoDataValue").text = str(NODATA)
        simple = ET.SubElement(band, "SimpleSource")
        ET.SubElement(simple, "SourceFilename", relativeToVRT="0").text = f'GPKG:{store.path}:{table}'
        ET.SubElement(simple, "SourceBand").text = "1"
    return ET.tostring(root, encoding="unicode")


def matches_raster_source(source, store, record):
    if _file_reference(record):
        try:
            return Path(source).resolve() == raster_file(store, _file_reference(record))
        except (ModelError, OSError, ValueError):
            return False
    expected = f'GPKG:{store.path}:{record["table_name"]}'
    if source == expected:
        return True  # Also migrates the earlier dBA-only layer in place.
    if source.startswith("<VRTDataset"):
        try:
            root = ET.fromstring(source)
            return root.findtext("VRTRasterBand/SimpleSource/SourceFilename") == expected
        except ET.ParseError:
            pass
    return False


def load_surface_layer(store, record):
    from qgis.core import QgsRasterLayer
    layer = QgsRasterLayer(raster_source(store, record), record["name"], "gdal")
    if not layer.isValid():
        raise ModelError("Could not load area result surface: " + record["name"])
    style_surface(layer)
    return layer
