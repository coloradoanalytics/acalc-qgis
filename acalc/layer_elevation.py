# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Undoable, whole-layer ground elevation authoring."""

from contextlib import nullcontext
import math

from qgis.core import QgsUnitTypes

from .layers import geometry_from_rows, initialize_added_object_fields, validate_geometry, vertex_rows
from .object_geometry import building_editor_rows, feature_value
from .raster_elevation import RasterElevation
from .schema import LAYER_TYPES, ModelError


def set_layer_z_values(layer, *, value=None, raster_record=None):
    """Prepare every object before applying one edit command; values are meters.

    Building height modes retain
    their authored meaning, including absolute per-point roof elevations.
    Nothing is committed to the provider here.
    """
    kind = LAYER_TYPES[layer.customProperty("acalc/feature_type")]
    if not kind.ground_points:
        raise ModelError("This layer type has no editable ground Z values.")
    if (value is None) == (raster_record is None):
        raise ModelError("Choose either a fixed Z value or an elevation raster.")
    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ModelError("The Z value must be a finite number.")
    attributes = {"ground_mode": "per_point" if raster_record is not None or kind.geometry == "Point" else "uniform"}
    indices = {name: layer.fields().indexFromName(name) for name in attributes}
    if any(index < 0 for index in indices.values()):
        raise ModelError("The layer is missing ground elevation fields. Reopen the model before setting Z values.")
    context = (RasterElevation(raster_record["path"], layer.crs().toWkt(), raster_record["band"])
               if raster_record is not None else nullcontext(None))
    updates = []
    with context as raster:
        for feature in layer.getFeatures():
            try:
                validate_geometry(feature.geometry(), kind)
                rows = vertex_rows(feature.geometry(), kind)
                zs = raster.elevations([(row[1], row[2]) for row in rows]) if raster else [value] * len(rows)
                mode = feature_value(feature, "height_mode", "individual")
                updated = [(ring, x, y, z, old_z + height if kind.height_controls and mode == "absolute_individual" else height)
                           for (ring, x, y, old_z, height), z in zip(rows, zs)]
                if kind.height_controls:
                    updated = building_editor_rows(updated, mode, float(feature_value(feature, "height_value", 3 if kind.key == "barrier" else 6)))
                if kind.is_building:
                    from .building_faces import faces_from_rows, read_face_sources, source_polygon
                    sources = read_face_sources(feature_value(feature, "face_sources"))
                    if sources:
                        scale = QgsUnitTypes.fromUnitToUnitFactor(layer.crs().mapUnits(), QgsUnitTypes.DistanceMeters)
                        faces = faces_from_rows(updated, scale)
                        for source in sources:
                            source_polygon(source, faces, len(updated))
                updates.append((feature.id(), geometry_from_rows(updated, kind)))
            except (ModelError, ValueError) as error:
                raise ModelError(f'{feature_value(feature, "name", "Object")} (feature {feature.id()}): {error}') from error
    if not updates:
        raise ModelError("There are no objects on this layer.")
    was_editable = layer.isEditable()
    if not was_editable and not layer.startEditing():
        raise ModelError("The layer could not be opened for editing. Check that the GeoPackage is writable.")
    selected_ids = layer.selectedFeatureIds()
    layer.beginEditCommand("Acalc: set layer Z values")
    try:
        remapped = initialize_added_object_fields(layer, kind, own_edit_command=False)
        for fid, geometry in updates:
            fid = remapped.get(fid, fid)
            if not layer.changeGeometry(fid, geometry):
                raise ModelError(f"Could not set Z values for feature {fid}.")
            for name, attribute in attributes.items():
                if not layer.changeAttributeValue(fid, indices[name], attribute):
                    raise ModelError(f"Could not update {name} for feature {fid}.")
        layer.endEditCommand()
    except Exception:
        layer.destroyEditCommand()
        if not was_editable:
            layer.rollBack()
        layer.selectByIds(selected_ids)
        raise
    layer.triggerRepaint()
    return len(updates)
