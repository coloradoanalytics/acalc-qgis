# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""On-demand raster interpolation for authoring ground Z (never solver input)."""

import math

from osgeo import gdal, osr

from .schema import ModelError


class RasterElevation:
    """Read only the neighbouring pixels needed for each model coordinate.

    Bilinear weights are relative to pixel centres. At the outer half pixel,
    clamp to the nearest centre. Invalid neighbours are omitted and remaining
    weights renormalized; no valid weighted neighbours or an out-of-extent point
    fails the operation. No vertical datum transformation is implied.
    """

    def __init__(self, path, model_wkt, band_number=1):
        self.dataset = None
        try:
            self.dataset = gdal.Open(path, gdal.GA_ReadOnly)
            if self.dataset is None:
                raise ModelError("Cannot open the elevation raster.")
            if type(band_number) is not int or not 1 <= band_number <= self.dataset.RasterCount:
                raise ModelError("The elevation raster does not have that band.")
            source, target = osr.SpatialReference(), osr.SpatialReference()
            if (source.ImportFromWkt(model_wkt) != 0 or
                    not self.dataset.GetProjection() or
                    target.ImportFromWkt(self.dataset.GetProjection()) != 0):
                raise ModelError("The model and elevation raster must declare their coordinate systems.")
            for crs in (source, target):
                crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            self.transform = osr.CoordinateTransformation(source, target)
            affine = self.dataset.GetGeoTransform(can_return_null=True)
            self.inverse = gdal.InvGeoTransform(affine) if affine else None
            if self.inverse is None:
                raise ModelError("The elevation raster must have an invertible affine geotransform.")
            self.band = self.dataset.GetRasterBand(band_number)
            self.mask = self.band.GetMaskBand()
            self.nodata = self.band.GetNoDataValue()
            self.scale = self.band.GetScale()
            self.offset = self.band.GetOffset()
            self.scale = 1.0 if self.scale is None else self.scale
            self.offset = 0.0 if self.offset is None else self.offset
            unit = self.band.GetUnitType().strip().lower()
            units = {"": 1.0, "m": 1.0, "metre": 1.0, "metres": 1.0,
                     "meter": 1.0, "meters": 1.0, "ft": 0.3048, "foot": 0.3048,
                     "feet": 0.3048, "us survey foot": 1200 / 3937,
                     "us_survey_foot": 1200 / 3937}
            if unit not in units:
                raise ModelError(f"Unsupported elevation raster vertical unit: {unit}.")
            self.vertical_scale = units[unit]
            if not all(math.isfinite(v) for v in (*self.inverse, self.scale, self.offset)):
                raise ModelError("The elevation raster has invalid coordinate or value scaling.")
        except Exception as error:
            self.close()
            if isinstance(error, ModelError):
                raise
            raise ModelError(f"Cannot read elevation raster: {error}") from error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.mask = self.band = self.dataset = None

    def elevation(self, x, y, *, strict=False):
        try:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ModelError("Ground point X and Y must be finite numbers.")
            rx, ry, _ = self.transform.TransformPoint(x, y)
            px, py = gdal.ApplyGeoTransform(self.inverse, rx, ry)
            width, height = self.dataset.RasterXSize, self.dataset.RasterYSize
            if not (math.isfinite(px) and math.isfinite(py) and
                    -1e-9 <= px <= width + 1e-9 and -1e-9 <= py <= height + 1e-9):
                raise ModelError("Ground point is outside the elevation raster.")
            cx = min(width - 1, max(0.0, px - 0.5))
            cy = min(height - 1, max(0.0, py - 0.5))
            x0, y0 = math.floor(cx), math.floor(cy)
            nx, ny = min(2, width - x0), min(2, height - y0)
            values = self.band.ReadAsArray(x0, y0, nx, ny)
            valid = self.mask.ReadAsArray(x0, y0, nx, ny)
            if values is None or valid is None:
                raise ModelError("Could not read elevation raster values.")
            total, weight_sum = 0.0, 0.0
            for row in range(ny):
                for col in range(nx):
                    weight = ((1 - (cx - x0)) if col == 0 else cx - x0) * ((1 - (cy - y0)) if row == 0 else cy - y0)
                    value = float(values[row, col])
                    if weight <= 0:
                        continue
                    if (not valid[row, col] or not math.isfinite(value)
                            or (self.nodata is not None and value == self.nodata)):
                        if strict:
                            raise ModelError("The terrain boundary contains missing elevation data (NoData). Draw a box with complete coverage.")
                        continue
                    total += weight * value
                    weight_sum += weight
            if weight_sum == 0:
                raise ModelError("No valid elevation values near this ground point (NoData).")
            result = (total / weight_sum * self.scale + self.offset) * self.vertical_scale
            if not math.isfinite(result):
                raise ModelError("Interpolated ground elevation is not finite.")
            return result
        except (RuntimeError, ValueError, OverflowError) as error:
            if isinstance(error, ModelError):
                raise
            raise ModelError(f"Cannot sample elevation raster: {error}") from error

    def elevations(self, points):
        result = []
        for index, (x, y) in enumerate(points, 1):
            try:
                result.append(self.elevation(x, y))
            except ModelError as error:
                raise ModelError(f"Ground point {index}: {error}") from error
        return result
