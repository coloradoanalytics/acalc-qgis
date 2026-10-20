# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Copy external vector features into the existing Acalc geometry contract."""

import math
from dataclasses import dataclass

from osgeo import ogr
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException,
    QgsGeometry, QgsJsonUtils, QgsProject, QgsVectorLayer, QgsWkbTypes,
)

from .layers import geometry_from_rows
from .object_geometry import building_editor_rows, feature_value
from .schema import LAYER_TYPES, ModelError, new_id
from .storage import require_name
from .trees import DEFAULT_CANOPY_HEIGHT, canopy_height


DEFAULT_HEIGHTS = {"point_source": 1.0, "line_source": 0.5, "area_source": 0.5,
                   "point_receiver": 1.5, "measurement": 1.5, "area_receiver": 1.5,
                   "barrier": 3.0, "building": 6.0, "source_building": 6.0}


@dataclass
class ImportOptions:
    kind_key: str
    name: str
    selected_only: bool = False
    name_field: str = ""
    notes_field: str = ""
    ground_mode: str = "constant"  # constant, field, geometry_z, raster
    ground_field: str = ""
    ground_z: float = 0.0
    height_field: str = ""
    height: float = 1.5
    ground_factor_field: str = ""
    ground_factor: float = 0.0
    canopy_height_field: str = ""
    canopy_height: float = DEFAULT_CANOPY_HEIGHT
    spacing: float = 10.0
    spectrum_id: str = ""
    vertical_to_m: float = 1.0  # Source fields/geometry Z only; constants already use meters.
    raster_path: str = ""
    raster_band: int = 1
    building_height_mode: str = "individual"


def compatible_kinds(layer):
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return []
    family = layer.geometryType()
    return [kind for kind in LAYER_TYPES.values()
            if QgsWkbTypes.geometryType(getattr(QgsWkbTypes, kind.geometry)) == family]


def numeric(feature, field, default, label, minimum=None, maximum=None):
    value = feature_value(feature, field) if field else default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ModelError(f"{label} must be a number" + (f" (field {field})." if field else "."))
    if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ModelError(f"{label} is outside its allowed range or is not finite.")
    return value


def prepare_import(source, options, target_crs, context):
    """Prepare every object before writing, sharing the object editor's sampler."""
    if options.ground_mode == "raster":
        from .raster_elevation import RasterElevation
        if not LAYER_TYPES[options.kind_key].ground_points:
            raise ModelError("This object type does not have ground points to sample.")
        if not options.raster_path:
            raise ModelError("Choose an elevation raster before importing.")
        with RasterElevation(options.raster_path, target_crs.toWkt(), options.raster_band) as raster:
            return _prepare_import(source, options, target_crs, context, raster)
    return _prepare_import(source, options, target_crs, context)


def _prepare_import(source, options, target_crs, context, raster=None):
    """Validate the complete copy without modifying either data source.

    Multipart features become independent objects. Curves and polygon holes
    fail explicitly, rather than introducing an undocumented approximation.
    Z is ground elevation in meters, never implicitly an object/roof elevation.
    """
    kind = LAYER_TYPES[options.kind_key]
    if not math.isfinite(options.vertical_to_m) or options.vertical_to_m <= 0:
        raise ModelError("Source elevation units need a positive conversion to meters.")
    require_name(options.name)
    if kind not in compatible_kinds(source):
        raise ModelError("Choose a vector layer with compatible point, line or polygon geometry.")
    if not source.crs().isValid() or not target_crs.isValid():
        raise ModelError("Both the source layer and the model need a valid coordinate reference system.")
    if options.ground_mode not in ("constant", "field", "geometry_z", "raster"):
        raise ModelError("Choose how to set ground elevation.")
    if kind.is_building and options.building_height_mode not in ("individual", "average"):
        raise ModelError("Choose relative per-point or average-ground building heights for import.")
    fields = [options.name_field, options.notes_field]
    if kind.ground_points and options.ground_mode == "field":
        if not options.ground_field:
            raise ModelError("Choose the ground elevation field.")
        fields.append(options.ground_field)
    if kind.height:
        fields.append(options.height_field)
    if kind.key == "ground_absorption":
        fields.append(options.ground_factor_field)
    if kind.key == "trees":
        fields.append(options.canopy_height_field)
    for field in fields:
        if field and source.fields().indexFromName(field) < 0:
            raise ModelError(f"Source field {field} is no longer available.")
    transform = QgsCoordinateTransform(source.crs(), target_crs, context)
    features = source.getSelectedFeatures() if options.selected_only else source.getFeatures()
    prepared = []
    source_count = 0
    for feature in features:
        try:
            geometry = QgsGeometry(feature.geometry())
            if geometry.isNull() or geometry.isEmpty():
                raise ModelError("Geometry is empty.")
            flat = QgsWkbTypes.flatType(geometry.wkbType())
            expected = getattr(QgsWkbTypes, kind.geometry)
            if flat not in (expected, QgsWkbTypes.multiType(expected)):
                raise ModelError("Curved or mixed geometry is unsupported. Convert it to straight, single-family geometry first.")
            if geometry.transform(transform) != 0:
                raise ModelError("Could not transform geometry to the model CRS.")
            height = numeric(feature, options.height_field, options.height, "Height (m)", 0) if kind.height else 0.0
            if options.height_field:
                height *= options.vertical_to_m
            attributes = {"enabled": 1, "notes": str(feature_value(feature, options.notes_field, "")) if options.notes_field else "",
                          "source_layer": source.name(), "source_fid": str(feature.id()),
                          "source_attributes": QgsJsonUtils.exportAttributes(feature)}
            if kind.ground_points:
                attributes.update(ground_mode="per_point")
            if kind.is_building:
                attributes.update(height_mode=options.building_height_mode, height_value=height)
            if kind.key == "barrier":
                attributes.update(height_mode="individual", height_value=height)
            if kind.key == "area_receiver":
                spacing = numeric(feature, "", options.spacing, "Point spacing (m)", 0)
                if spacing == 0:
                    raise ModelError("Point spacing must be positive.")
                attributes.update(height_m=height, spacing_m=spacing)
            if kind.key == "ground_absorption":
                attributes["ground_factor"] = numeric(feature, options.ground_factor_field, options.ground_factor, "Ground factor G", 0, 1)
            if kind.key == "trees":
                tree_height = numeric(feature, options.canopy_height_field, options.canopy_height, "Tree height (m)")
                if options.canopy_height_field:
                    tree_height *= options.vertical_to_m
                attributes["canopy_height"] = canopy_height(tree_height)
            if kind.spectrum:
                attributes["spectrum_id"] = options.spectrum_id or None
            name = str(feature_value(feature, options.name_field, "") or "").strip() if options.name_field else ""
            name = name or f"{kind.singular} {feature.id()}"
            parts = geometry.asGeometryCollection() if geometry.isMultipart() else [geometry]
            for index, part in enumerate(parts):
                if part.isEmpty():
                    raise ModelError("Geometry contains an empty part.")
                if kind.geometry == "Polygon" and part.constGet().numInteriorRings():
                    raise ModelError("Polygons with holes are unsupported. Resolve the interior boundaries before importing.")
                points = list(part.vertices())
                if kind.geometry == "Polygon":
                    points = points[:-1]
                elevations = None
                if raster is not None:
                    try:
                        elevations = raster.elevations([(point.x(), point.y()) for point in points])
                    except ModelError as error:
                        raise ModelError(f"{name}, part {index + 1}: {error}") from error
                rows = []
                for point_index, point in enumerate(points):
                    z = 0.0
                    if kind.ground_points:
                        if elevations is not None:
                            # The shared raster sampler already returns meters.
                            z = elevations[point_index]
                        elif options.ground_mode == "geometry_z":
                            z = numeric(feature, "", point.z(), "Geometry ground Z (m)")
                            z *= options.vertical_to_m
                        else:
                            z = numeric(feature, options.ground_field if options.ground_mode == "field" else "",
                                        options.ground_z, "Ground Z (m)")
                            if options.ground_mode == "field":
                                z *= options.vertical_to_m
                    rows.append((0, point.x(), point.y(), z, height))
                if kind.is_building:
                    rows = building_editor_rows(rows, options.building_height_mode, height)
                converted = geometry_from_rows(rows, kind)
                values = dict(attributes, object_id=new_id(),
                              name=f"{name} (part {index + 1})" if len(parts) > 1 else name)
                prepared.append((bytes(converted.asWkb()), values))
            source_count += 1
        except (ModelError, QgsCsException) as error:
            raise ModelError(f"{source.name()}, feature {feature.id()}: {error}") from error
    if not prepared:
        raise ModelError("No features to import. Check the layer and selected-features option.")
    return prepared, source_count


def import_layer(store, source, options, project=None):
    """Persist only a fully validated copy; register it after every write succeeds."""
    from .spectra import compatible_power
    kind = LAYER_TYPES[options.kind_key]
    if kind.spectrum and options.spectrum_id:
        spectrum = next((r for r in store.spectra("sound_power") if r["spectrum_id"] == options.spectrum_id), None)
        if spectrum is None or not compatible_power(spectrum["spectrum"], kind.key):
            raise ModelError("Choose a compatible sound power spectrum from this model.")
    prepared, source_count = prepare_import(
        source, options, QgsCoordinateReferenceSystem(store.metadata()["crs_wkt"]),
        (project or QgsProject.instance()).transformContext())

    def populate(layer):
        for name in ("source_layer", "source_fid", "source_attributes"):
            if layer.CreateField(ogr.FieldDefn(name, ogr.OFTString)) != 0:
                raise ModelError(f"Could not create import field {name}.")
        if layer.StartTransaction() != 0:
            raise ModelError("Could not start the layer import transaction.")
        try:
            for wkb, attributes in prepared:
                feature = ogr.Feature(layer.GetLayerDefn())
                geometry = ogr.CreateGeometryFromWkb(wkb)
                if geometry is None or feature.SetGeometry(geometry) != 0:
                    raise ModelError("Could not write imported geometry.")
                for name, value in attributes.items():
                    if value is not None:
                        feature.SetField(name, value)
                if layer.CreateFeature(feature) != 0:
                    raise ModelError("Could not write an imported object.")
                feature = geometry = None
            if layer.CommitTransaction() != 0:
                raise ModelError("Could not commit the layer import.")
        except Exception:
            feature = geometry = None
            layer.RollbackTransaction()
            raise

    record = store.add_layer(kind.key, options.name, populate=populate)
    return record, source_count, len(prepared)
