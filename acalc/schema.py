# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Shared, engine-independent model vocabulary. No QGIS dependency."""

from dataclasses import dataclass
import secrets
import time
import uuid

SCHEMA_VERSION = 15

BUILDING_TYPES = frozenset(("building", "source_building"))
SOURCE_TYPES = frozenset(("point_source", "line_source", "area_source", "source_building"))


class ModelError(ValueError):
    pass


def new_id():
    """UUIDv7, including on the Python versions bundled with QGIS 3."""
    value = ((time.time_ns() // 1_000_000) << 80) | (7 << 76)
    value |= secrets.randbits(12) << 64
    value |= 2 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


@dataclass(frozen=True)
class LayerType:
    key: str
    title: str
    singular: str
    geometry: str
    height: bool
    color: str
    spectrum: bool = False
    elevation: bool = True

    @property
    def height_controls(self):
        return self.is_building or self.key == "barrier"

    @property
    def is_building(self):
        return self.key in BUILDING_TYPES

    @property
    def is_source(self):
        return self.key in SOURCE_TYPES

    @property
    def ground_points(self):
        # Area perimeters retain legacy ZM storage, but only define an XY grid.
        return self.elevation and self.key != "area_receiver"

    @property
    def dimensions(self):
        return "ZM" if self.height else "Z" if self.elevation else ""


LAYER_TYPES = {
    item.key: item for item in (
        LayerType("point_source", "Point sources", "Point source", "Point", True, "#d35f36", True),
        LayerType("line_source", "Line sources", "Line source", "LineString", True, "#d68c26", True),
        LayerType("area_source", "Area sources", "Area source", "Polygon", True, "#d68c26", True),
        LayerType("source_building", "Source Buildings", "Source building", "Polygon", True, "#b86b35"),
        LayerType("point_receiver", "Point receivers", "Point receiver", "Point", True, "#246ac4"),
        LayerType("area_receiver", "Area receivers", "Area receiver", "Polygon", True, "#246ac4"),
        LayerType("measurement", "Measurements", "Measurement", "Point", True, "#16887d"),
        LayerType("barrier", "Barriers", "Barrier", "LineString", True, "#a23f72"),
        LayerType("building", "Buildings", "Building", "Polygon", True, "#7b6e60"),
        LayerType("ground_absorption", "Ground absorption areas", "Ground absorption area", "Polygon", False, "#57945c", elevation=False),
        LayerType("trees", "Trees", "Trees area", "Polygon", False, "#2d713d", elevation=False),
        LayerType("terrain_point", "Terrain points", "Terrain point", "Point", False, "#4f8a4b"),
        LayerType("terrain_line", "Terrain lines", "Terrain line", "LineString", False, "#779344"),
    )
}


def definition(layer_ids=()):
    return {"schema_version": SCHEMA_VERSION, "layer_ids": list(layer_ids)}
