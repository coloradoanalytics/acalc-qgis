# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Transactional horizontal reprojection of all model layers, including archives."""

import json
import math

from osgeo import gdal, ogr, osr
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry

from .schema import ModelError
from .storage import canonical_crs_wkt, now
from .units import length_factor


def sql_text(value):
    return "'" + value.replace("'", "''") + "'"


def change_coordinates(store, crs, unit, context):
    length_factor(unit)
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(crs.toWkt()) != 0 or not srs.IsProjected():
        raise ModelError("Choose a projected horizontal coordinate system.")
    scale = srs.GetLinearUnits()
    if not math.isfinite(scale) or scale <= 0:
        raise ModelError("The CRS must have a positive linear unit conversion.")
    wkt = canonical_crs_wkt(srs)
    metadata = store.metadata()
    extra = json.loads(metadata["metadata_json"])
    old_crs = QgsCoordinateReferenceSystem(metadata["crs_wkt"])
    reproject = not bool(srs.IsSame(osr.SpatialReference(metadata["crs_wkt"])))
    if not reproject and extra.get("elevation_unit", extra.get("input_length_unit", "m")) == unit and "input_length_unit" not in extra:
        return False
    extra["elevation_unit"] = unit
    extra.pop("input_length_unit", None)
    transform = QgsCoordinateTransform(old_crs, crs, context)
    dataset = ogr.Open(store.path, update=1)
    if dataset is None:
        raise ModelError("Could not open the model for coordinate changes.")
    if dataset.StartTransaction() != ogr.OGRERR_NONE:
        raise ModelError("Could not start the coordinate change transaction.")
    def execute(sql):
        gdal.ErrorReset()
        dataset.ExecuteSQL(sql)
        if gdal.GetLastErrorType() >= gdal.CE_Failure:
            raise ModelError(gdal.GetLastErrorMsg())
    try:
        if reproject:
            # Historical result samples retain their original coordinates and CRS.
            for run in store.calculation_runs():
                saved = dict(run["metadata"])
                if "coordinate_reference" not in saved:
                    saved["coordinate_reference"] = {key: metadata[key] for key in ("crs_wkt", "horizontal_to_m")}
                    execute("UPDATE acalc_calculation_runs SET metadata_json=" +
                                       sql_text(json.dumps(saved)) + " WHERE run_id=" + sql_text(run["run_id"]))
            for record in store.layers() + store.layers(archived=True):
                layer = dataset.GetLayerByName(record["table_name"])
                field = ogr.GeomFieldDefn("geom", layer.GetGeomType())
                field.SetSpatialRef(srs)
                if layer.AlterGeomFieldDefn(0, field, ogr.ALTER_GEOM_FIELD_DEFN_SRS_FLAG) != ogr.OGRERR_NONE:
                    raise ModelError("Could not update the layer CRS: " + record["name"])
                layer.ResetReading()
                extent = None
                for feature in layer:
                    if feature.GetGeometryRef() is None or feature.GetGeometryRef().IsEmpty():
                        raise ModelError("Cannot reproject an empty object in " + record["name"])
                    geometry = QgsGeometry()
                    geometry.fromWkb(bytes(feature.GetGeometryRef().ExportToIsoWkb()))
                    # QGIS defaults to XY-only transformation, preserving ground Z and height M.
                    if geometry.transform(transform) != 0:
                        raise ModelError("Could not reproject an object in " + record["name"])
                    if not all(math.isfinite(p.x()) and math.isfinite(p.y()) for p in geometry.vertices()):
                        raise ModelError("Reprojection produced invalid coordinates in " + record["name"])
                    bounds = geometry.boundingBox()
                    if extent is None:
                        extent = bounds
                    else:
                        extent.combineExtentWith(bounds)
                    feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
                    if layer.SetFeature(feature) != ogr.OGRERR_NONE:
                        raise ModelError("Could not save reprojected geometry in " + record["name"])
                # Flush the driver's cached bounds before replacing them with
                # the new extent (which may shrink as well as grow).
                layer.SyncToDisk()
                bounds = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()) if extent else (None,) * 4
                execute("UPDATE gpkg_contents SET " + ",".join(key + "=" + (repr(value) if value is not None else "NULL")
                        for key, value in zip(("min_x", "min_y", "max_x", "max_y"), bounds)) +
                        " WHERE table_name=" + sql_text(record["table_name"]))
        values = {"crs_wkt": wkt, "crs_authid": crs.authid(), "horizontal_unit": srs.GetLinearUnitsName(),
                  "metadata_json": json.dumps(extra), "modified_at": now()}
        execute("UPDATE acalc_project SET " + ",".join(key + "=" + sql_text(value) for key, value in values.items()) +
                           ",horizontal_to_m=" + repr(scale))
        if dataset.CommitTransaction() != ogr.OGRERR_NONE:
            raise ModelError("Could not commit the coordinate change.")
    except Exception:
        dataset.RollbackTransaction()
        raise
    finally:
        dataset = None
    return True
