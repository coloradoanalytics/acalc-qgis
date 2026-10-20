# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""GeoPackage persistence. Model metadata is transactional SQLite; geometry is OGR.

Connections are short-lived so QGIS can edit feature tables independently.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from osgeo import ogr, osr

from .schema import BUILDING_TYPES, LAYER_TYPES, SCHEMA_VERSION, ModelError, definition, new_id
from .directivity_storage import DirectivityStore
from .spectra import SPECTRUM_KINDS, validate_spectrum, compatible_power, upgrade_spectrum
from .measurement import ALL_MEASUREMENT_FIELDS, MEASUREMENT_FIELDS, resolved_measurement
from .calculation_settings import ISO_ENGINE, validate_settings, default_settings


CALCULATION_TABLES = ("acalc_calculation_runs", "acalc_receiver_results")
CALCULATION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS acalc_calculation_runs (
        run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        variant_id TEXT NOT NULL,
        variant_name TEXT NOT NULL,
        engine TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        standard TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        project_modified_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status='complete'),
        settings_json TEXT NOT NULL CHECK(json_valid(settings_json)),
        source_count INTEGER NOT NULL CHECK(source_count >= 0),
        receiver_count INTEGER NOT NULL CHECK(receiver_count >= 0),
        elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds >= 0),
        warnings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warnings_json)),
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json))
    )""",
    """CREATE TABLE IF NOT EXISTS acalc_receiver_results (
        run_id TEXT NOT NULL REFERENCES acalc_calculation_runs(run_id) ON DELETE CASCADE,
        layer_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        receiver_name TEXT NOT NULL,
        octave_levels_json TEXT NOT NULL CHECK(json_valid(octave_levels_json)
            AND json_type(octave_levels_json)='array' AND json_array_length(octave_levels_json)=8),
        broadband_z_db REAL,
        broadband_a_db REAL,
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
        PRIMARY KEY(run_id,layer_id,object_id)
    )""",
    """CREATE INDEX IF NOT EXISTS acalc_runs_variant
        ON acalc_calculation_runs(variant_id,finished_at DESC,run_id DESC)""",
    """CREATE INDEX IF NOT EXISTS acalc_results_receiver
        ON acalc_receiver_results(layer_id,object_id,run_id)""",
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def require_name(value):
    if not isinstance(value, str):
        raise ModelError("Please enter a name.")
    value = value.strip()
    if not value:
        raise ModelError("Please enter a name.")
    return value


def canonical_crs_wkt(srs):
    """Use the same WKT1 representation as GDAL's GeoPackage CRS registry.

    WKT2 can omit nested EPSG identifiers which GDAL restores when registering a
    known projected CRS. Consult the EPSG record only after proving that its full
    definition matches the authoritative input; a code alone is insufficient.
    """
    authority, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
    if authority == "EPSG" and code and code.isdigit():
        registered = osr.SpatialReference()
        try:
            if registered.ImportFromEPSG(int(code)) == 0 and srs.IsSame(registered):
                return registered.ExportToWkt()
        except (RuntimeError, OverflowError):
            pass
    return srs.ExportToWkt()


class ModelStore(DirectivityStore):
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        self.migration_backup = None

    @contextmanager
    def connection(self, readonly=False):
        # URI quoting is handled by pathlib, including spaces, # and ?.
        uri = Path(self.path).as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise ModelError(str(error)) from error
        finally:
            connection.close()

    @classmethod
    def create(cls, path, name, crs_wkt, crs_authid="", author="", description=""):
        name = require_name(name)
        path = Path(path).expanduser().resolve()
        if path.exists():
            raise ModelError("That file already exists. Choose a new filename.")
        srs = osr.SpatialReference()
        if srs.ImportFromWkt(crs_wkt) != 0 or not srs.IsProjected():
            raise ModelError("Choose a projected coordinate system for the model.")
        if not math.isfinite(srs.GetLinearUnits()) or srs.GetLinearUnits() <= 0:
            raise ModelError("The model CRS must declare a positive linear-unit conversion to meters.")
        # Match GDAL's GeoPackage definition serialization, including when QGIS
        # supplied WKT2. Engine readers can compare these authoritative records
        # without an independent CRS library or an authority-code assumption.
        crs_wkt = canonical_crs_wkt(srs)
        # Build in the destination directory and publish without overwriting a file.
        fd, temporary = tempfile.mkstemp(suffix=".gpkg", dir=path.parent)
        os.close(fd)
        os.unlink(temporary)
        try:
            dataset = ogr.GetDriverByName("GPKG").CreateDataSource(temporary)
            if dataset is None:
                raise ModelError("Could not create the GeoPackage.")
            # OGR needs a contents table for a valid, empty GeoPackage.
            dataset.CreateLayer("acalc_spectra", geom_type=ogr.wkbNone)
            dataset = None
            store = cls(temporary)
            with store.connection() as db:
                db.executescript("""
                    CREATE TABLE acalc_project (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_version INTEGER NOT NULL,
                        project_id TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                        author TEXT NOT NULL DEFAULT '',
                        crs_wkt TEXT NOT NULL, crs_authid TEXT NOT NULL,
                        horizontal_unit TEXT NOT NULL,
                        horizontal_to_m REAL NOT NULL CHECK(horizontal_to_m>0),
                        vertical_unit TEXT NOT NULL CHECK(vertical_unit='m'),
                        vertical_datum TEXT NOT NULL DEFAULT 'Unspecified',
                        created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE acalc_layers (
                        layer_id TEXT PRIMARY KEY, table_name TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL, feature_type TEXT NOT NULL,
                        sort_order INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE acalc_variants (
                        variant_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        definition_json TEXT NOT NULL, created_at TEXT NOT NULL, modified_at TEXT NOT NULL
                    );
                    ALTER TABLE acalc_spectra ADD COLUMN spectrum_id TEXT;
                    ALTER TABLE acalc_spectra ADD COLUMN name TEXT;
                    ALTER TABLE acalc_spectra ADD COLUMN spectrum_json TEXT;
                    CREATE UNIQUE INDEX acalc_spectra_id ON acalc_spectra(spectrum_id);
                """)
                store._create_calculation_schema(db)
                store._create_calculation_options(db)
                store._create_raster_schema(db)
                store._create_directivity_schema(db)
                timestamp = now()
                db.execute("INSERT INTO acalc_project VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (SCHEMA_VERSION, new_id(), name, description, author, crs_wkt,
                            crs_authid, srs.GetLinearUnitsName(), srs.GetLinearUnits(), "m", "Unspecified", timestamp, timestamp, "{}"))
                db.execute("INSERT INTO acalc_variants VALUES (?,?,?,?,?)",
                           (new_id(), "Default", json.dumps(definition()), timestamp, timestamp))
                for table in ("acalc_project", "acalc_layers", "acalc_variants"):
                    db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description) VALUES (?,'attributes',?,?)",
                               (table, table, f"Acalc model metadata; schema version {SCHEMA_VERSION}"))
                store._create_input_triggers(db)
            # Hard link is atomic and fails if another process created the target.
            os.link(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return cls.open(path)

    @classmethod
    def open(cls, path):
        store = cls(path)
        if not Path(store.path).is_file():
            raise ModelError("The project file does not exist.")
        try:
            with store.connection(readonly=True) as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != 0x47504B47:
                    raise ModelError("This file is not a GeoPackage.")
                rows = db.execute("SELECT * FROM acalc_project").fetchall()
                if len(rows) != 1 or rows[0]["schema_version"] not in range(1, SCHEMA_VERSION + 1):
                    raise ModelError(f"Unsupported Acalc model schema. This plugin supports version {SCHEMA_VERSION} and upgrades versions 1–{SCHEMA_VERSION - 1}.")
                project = dict(rows[0])
                version = project["schema_version"]
                require_name(project["name"])
                srs = osr.SpatialReference()
                if srs.ImportFromWkt(project["crs_wkt"]) != 0 or not srs.IsProjected():
                    raise ModelError("The model must use a projected coordinate system.")
                horizontal_to_m = srs.GetLinearUnits()
                if not math.isfinite(horizontal_to_m) or horizontal_to_m <= 0:
                    raise ModelError("The model CRS must declare a positive linear-unit conversion to meters.")
                if version >= 3:
                    conversion = project.get("horizontal_to_m")
                    if (type(conversion) not in (float, int) or not math.isfinite(conversion)
                            or not math.isclose(conversion, horizontal_to_m, rel_tol=1e-12, abs_tol=0)):
                        raise ModelError("The stored horizontal unit conversion does not match the model CRS.")
                    store._validate_calculation_schema(db, version)
                if version >= 5:
                    store._read_calculation(db)
                if project["vertical_unit"] != "m" or not isinstance(json.loads(project["metadata_json"]), dict):
                    raise ModelError("Invalid model metadata or vertical units.")
                if json.loads(project["metadata_json"]).get("elevation_unit", json.loads(project["metadata_json"]).get("input_length_unit", "m")) not in ("m", "ft"):
                    raise ModelError("Invalid project elevation unit; choose meters or feet.")
                layers = db.execute("SELECT * FROM acalc_layers").fetchall()
                for layer in layers:
                    kind = LAYER_TYPES.get(layer["feature_type"])
                    table = layer["table_name"]
                    if kind is None or not re.fullmatch(r"model_[a-f0-9]{32}", table):
                        raise ModelError("The model contains an unsupported layer definition.")
                    geometry = db.execute("SELECT * FROM gpkg_geometry_columns WHERE table_name=?", (table,)).fetchone()
                    if (geometry is None or geometry["geometry_type_name"] != kind.geometry.upper()
                            or geometry["column_name"] != "geom"
                            or geometry["z"] != int(kind.elevation) or geometry["m"] != int(kind.height)):
                        raise ModelError(f"Invalid geometry schema for {layer['name']}.")
                    crs_row = db.execute("SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id=?", (geometry["srs_id"],)).fetchone()
                    layer_srs = osr.SpatialReference()
                    if crs_row is None or layer_srs.ImportFromWkt(crs_row[0]) != 0 or not srs.IsSame(layer_srs):
                        raise ModelError(f"The CRS of {layer['name']} does not match the model.")
                    columns = {r["name"]: r for r in db.execute(f'PRAGMA table_info("{table}")')}
                    fields = set(columns)
                    required = {"fid", "geom", "object_id", "name", "notes", "enabled"}
                    if kind.spectrum:
                        required.add("spectrum_id")
                    if version >= 9 and kind.key == "point_source":
                        required.update(("directivity_id", "directivity_orientation"))
                    if version >= 8 and (kind.is_building or kind.key == "barrier"):
                        required.add("absorption_spectrum_id")
                    if version >= 11 and kind.is_building:
                        required.add("face_sources")
                    if kind.key == "area_receiver":
                        required.update(("height_m", "spacing_m"))
                    if kind.key == "measurement":
                        required.update(ALL_MEASUREMENT_FIELDS)
                    if kind.key == "ground_absorption":
                        required.add("ground_factor")
                    if kind.key == "trees":
                        required.add("canopy_height")
                    if version >= 4 and kind.elevation:
                        if version < 12:
                            required.add("infer_z")
                        if kind.is_building:
                            required.update(("height_mode", "height_value"))
                    if version >= 15 and kind.key == "barrier":
                        required.update(("height_mode", "height_value"))
                    if not required <= fields:
                        raise ModelError(f"Missing required fields in {layer['name']}.")
                    if version >= 11 and kind.is_building:
                        column = columns["face_sources"]
                        if column["type"].strip().upper() != "TEXT" or column["notnull"]:
                            raise ModelError(f"{layer['name']}: face_sources must be nullable TEXT.")
                    if version >= 9 and kind.key == "point_source":
                        for field in ("directivity_id", "directivity_orientation"):
                            if columns[field]["type"].strip().upper() != "TEXT" or columns[field]["notnull"]:
                                raise ModelError(f"{layer['name']}: {field} must be nullable TEXT.")
                    if version >= 8 and (kind.is_building or kind.key == "barrier"):
                        column = columns["absorption_spectrum_id"]
                        if column["type"].strip().upper() != "TEXT" or column["notnull"]:
                            raise ModelError(f"Invalid surface material field in {layer['name']}: absorption_spectrum_id must be nullable TEXT.")
                active_ids = {r["layer_id"] for r in layers if not r["archived"]}
                terrain_ids = ({row[0] for row in db.execute("SELECT group_id FROM acalc_terrain_groups WHERE is_base=0")}
                               if version == 6 else None)
                if version >= 7:
                    store._validate_raster_schema(db)
                configs = db.execute("SELECT * FROM " + ("acalc_variants" if version >= 5 else "acalc_configurations")).fetchall()
                if not configs:
                    raise ModelError("A model must contain at least one variant.")
                for config in configs:
                    require_name(config["name"])
                    store.validate_definition(json.loads(config["definition_json"]), active_ids, version, terrain_ids)
                if version == 1:
                    # Schema 1 never defined the contents of its reserved catalog.
                    # Do not reinterpret third-party values or references on upgrade.
                    if db.execute("SELECT COUNT(*) FROM acalc_spectra").fetchone()[0]:
                        raise ModelError("Cannot upgrade a schema 1 model with records in its reserved spectrum table.")
                    for layer in layers:
                        if LAYER_TYPES[layer["feature_type"]].spectrum and db.execute(
                                f'SELECT 1 FROM "{layer["table_name"]}" WHERE spectrum_id IS NOT NULL AND spectrum_id != \'\' LIMIT 1').fetchone():
                            raise ModelError("Cannot upgrade a schema 1 model with undefined spectrum references.")
                else:
                    store._read_spectra(db, version)
                if version >= 9:
                    store._read_directivities(db)
        except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ModelError(f"Not a valid Acalc model GeoPackage: {error}") from error
        except ModelError as error:
            if "no such table" in str(error):
                raise ModelError("This GeoPackage is not an Acalc model. Create a new Acalc project first.") from error
            raise
        if version < SCHEMA_VERSION:
            store._upgrade_model(version, horizontal_to_m, canonical_crs_wkt(srs))
        store._ensure_measurement_input_triggers()
        store._ensure_ground_mode()
        store._ensure_reflection_coefficients()
        store.discard_calculation_history()
        return store

    def _ensure_measurement_input_triggers(self):
        """Track existing Measurement layers now that they are prediction inputs."""
        with self.connection(readonly=True) as db:
            triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            tables = [row[0] for row in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type='measurement'")
                      if any(f"acalc_touch_{row[0]}_{event}" not in triggers for event in ("insert", "update", "delete"))]
        if tables:
            with self.connection() as db:
                for table in tables:
                    self._create_input_triggers(db, table)
                # Earlier runs omitted these receivers; keep those results but
                # require a fresh calculation before claiming model agreement.
                db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def _ensure_reflection_coefficients(self):
        """Append nullable surface overrides to existing schema-7 models.

        NULL inherits the shared default. Never rewrite saved geometry or
        existing coefficients; the engine version marks old results stale.
        """
        with self.connection(readonly=True) as db:
            tables = [row["table_name"] for row in db.execute(
                "SELECT table_name FROM acalc_layers WHERE feature_type IN ('building','source_building','barrier')")
                if "reflection_coefficient" not in {field["name"] for field in db.execute(
                    f'PRAGMA table_info("{row["table_name"]}")')}]
        if not tables:
            return
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in tables:
                fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if "reflection_coefficient" not in fields:
                    db.execute(f'ALTER TABLE "{table}" ADD COLUMN reflection_coefficient REAL')

    def _ensure_ground_mode(self):
        """Ensure explicit ground authoring metadata exists."""
        with self.connection(readonly=True) as db:
            tables = [row["table_name"] for row in db.execute(
                "SELECT table_name,feature_type FROM acalc_layers")
                if LAYER_TYPES[row["feature_type"]].ground_points and "ground_mode" not in {field["name"] for field in db.execute(
                    f'PRAGMA table_info("{row["table_name"]}")')}]
        if not tables:
            return
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in tables:
                # Another opener may have appended the field since the read.
                fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if "ground_mode" not in fields:
                    db.execute(f'ALTER TABLE "{table}" ADD COLUMN ground_mode TEXT')

    @staticmethod
    def _remove_z_inference(db):
        """One-way legacy migration; never reinterpret placeholder Z as ground."""
        for layer in db.execute("SELECT table_name,feature_type FROM acalc_layers").fetchall():
            table = layer["table_name"]
            kind = LAYER_TYPES[layer["feature_type"]]
            fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            # Attribute-only migration leaves the spatial index unchanged. GDAL's
            # RTree UPDATE triggers nevertheless call spatial SQL functions even
            # for attribute updates; suspend and restore their exact definitions.
            spatial_triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND name LIKE 'rtree_%'", (table,)).fetchall()
            for trigger in spatial_triggers:
                db.execute('DROP TRIGGER "' + trigger['name'].replace('"', '""') + '"')
            if kind.ground_points:
                if "ground_mode" not in fields:
                    db.execute(f'ALTER TABLE "{table}" ADD COLUMN ground_mode TEXT')
                if "infer_z" in fields:
                    db.execute(f'''UPDATE "{table}" SET ground_mode=CASE
                        WHEN infer_z != 0 THEN 'unresolved'
                        WHEN ground_mode='terrain' THEN 'per_point' ELSE ground_mode END''')
                else:
                    db.execute(f'''UPDATE "{table}" SET ground_mode='per_point' WHERE ground_mode IS NULL''')
            elif "ground_mode" in fields:
                db.execute(f'ALTER TABLE "{table}" DROP COLUMN ground_mode')
            if "infer_z" in fields:
                db.execute(f'ALTER TABLE "{table}" DROP COLUMN infer_z')
            for trigger in spatial_triggers:
                db.execute(trigger['sql'])

    def discard_calculation_history(self):
        """Reduce files written by older versions to one result set per variant."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            obsolete = db.execute("""SELECT old.run_id FROM acalc_calculation_runs AS old WHERE EXISTS (
                SELECT 1 FROM acalc_calculation_runs AS newer
                WHERE newer.variant_id=old.variant_id AND
                (newer.finished_at>old.finished_at OR
                 (newer.finished_at=old.finished_at AND newer.run_id>old.run_id)))""").fetchall()
            db.executemany("DELETE FROM acalc_receiver_results WHERE run_id=?", obsolete)
            db.executemany("DELETE FROM acalc_calculation_runs WHERE run_id=?", obsolete)

    def _upgrade_model(self, version, horizontal_to_m, canonical_crs_wkt):
        """Back up a validated legacy file, then update metadata in one transaction."""
        backup = Path(self.path).with_name(Path(self.path).stem + f".schema-{version}-backup-{new_id()}.gpkg")
        # SQLite backup includes committed WAL contents, unlike a filesystem copy.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self.connection(readonly=True) as source:
                target = sqlite3.connect(backup)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except Exception:
            backup.unlink()
            raise
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._add_barrier_heights(db)
            if version in (7, 8, 9, 10, 11, 12, 13, 14):
                # Add authoring fields, declare legacy point mountings, and
                # classify buildings which already contain component sources.
                # Preserve geometry, rasters, settings, catalogs and result rows verbatim.
                if version < 13:
                    self._add_absorption_assignments(db)
                    self._add_face_sources(db)
                    self._create_directivity_schema(db)
                    self._remove_z_inference(db)
                    self._declare_point_mounting(db)
                if version < 14:
                    self._split_building_types(db)
                self._create_input_triggers(db)
                for config in db.execute("SELECT variant_id,definition_json FROM acalc_variants").fetchall():
                    value = json.loads(config["definition_json"])
                    value["schema_version"] = SCHEMA_VERSION
                    db.execute("UPDATE acalc_variants SET definition_json=? WHERE variant_id=?",
                               (json.dumps(value), config["variant_id"]))
                db.execute("UPDATE acalc_project SET schema_version=?,modified_at=?", (SCHEMA_VERSION, now()))
                db.execute("UPDATE gpkg_contents SET description=? WHERE table_name IN ('acalc_project','acalc_layers','acalc_variants','acalc_spectra')",
                           (f"Acalc model metadata; schema version {SCHEMA_VERSION}",))
                self.migration_backup = str(backup)
                return
            columns = {row["name"] for row in db.execute("PRAGMA table_info(acalc_project)")}
            if "horizontal_to_m" not in columns:
                db.execute("ALTER TABLE acalc_project ADD COLUMN horizontal_to_m REAL NOT NULL DEFAULT 1 CHECK(horizontal_to_m>0)")
            db.execute("UPDATE acalc_project SET horizontal_to_m=?,crs_wkt=?", (horizontal_to_m, canonical_crs_wkt))
            if version < 5:
                # Preserve variant identities and historical run links while renaming
                # the persisted vocabulary. Calculation conditions now belong to the model.
                db.execute("ALTER TABLE acalc_configurations RENAME TO acalc_variants")
                db.execute("ALTER TABLE acalc_variants RENAME COLUMN configuration_id TO variant_id")
                db.execute("UPDATE gpkg_contents SET table_name='acalc_variants',identifier='acalc_variants' WHERE table_name='acalc_configurations'")
                for event in ("insert", "update", "delete"):
                    db.execute(f"DROP TRIGGER IF EXISTS acalc_touch_acalc_configurations_{event}")
                run_fields = {row["name"] for row in db.execute("PRAGMA table_info(acalc_calculation_runs)")}
                if "configuration_id" in run_fields:
                    db.execute("ALTER TABLE acalc_calculation_runs RENAME COLUMN configuration_id TO variant_id")
                    db.execute("ALTER TABLE acalc_calculation_runs RENAME COLUMN configuration_name TO variant_name")
                db.execute("DROP INDEX IF EXISTS acalc_runs_configuration")
                self._create_calculation_schema(db)
                legacy = []
                for row in db.execute("SELECT * FROM acalc_variants ORDER BY created_at,variant_id"):
                    value = json.loads(row["definition_json"])
                    if value.get("engine") == ISO_ENGINE:
                        legacy.append({"variant_id": row["variant_id"], "name": row["name"], "settings": validate_settings(value["settings"])})
                distinct = []
                for row in legacy:
                    if row["settings"] not in distinct:
                        distinct.append(row["settings"])
                self._create_calculation_options(db, legacy[0]["settings"] if len(distinct) == 1 else None,
                                                 legacy if len(distinct) > 1 else [])
            for layer in db.execute("SELECT * FROM acalc_layers").fetchall():
                table = layer["table_name"]
                fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if layer["feature_type"] in BUILDING_TYPES:
                    if "height_mode" not in fields:
                        db.execute(f"ALTER TABLE \"{table}\" ADD COLUMN height_mode TEXT NOT NULL DEFAULT 'individual'")
                    if "height_value" not in fields:
                        db.execute(f'ALTER TABLE "{table}" ADD COLUMN height_value REAL NOT NULL DEFAULT 6')
            self._remove_legacy_terrain(db)
            self._add_absorption_assignments(db)
            self._add_face_sources(db)
            self._create_raster_schema(db)
            self._create_directivity_schema(db)
            self._remove_z_inference(db)
            self._declare_point_mounting(db)
            self._split_building_types(db)
            self._create_input_triggers(db)
            for config in db.execute("SELECT variant_id,definition_json FROM acalc_variants").fetchall():
                value = json.loads(config["definition_json"])
                value = definition(value["layer_ids"])
                db.execute("UPDATE acalc_variants SET definition_json=? WHERE variant_id=?",
                           (json.dumps(value), config["variant_id"]))
            for row in db.execute("SELECT spectrum_id,spectrum_json FROM acalc_spectra").fetchall():
                db.execute("UPDATE acalc_spectra SET spectrum_json=? WHERE spectrum_id=?",
                           (json.dumps(upgrade_spectrum(json.loads(row["spectrum_json"]))), row["spectrum_id"]))
            db.execute("UPDATE acalc_project SET schema_version=?,modified_at=?", (SCHEMA_VERSION, now()))
            db.execute("UPDATE gpkg_contents SET description=? WHERE table_name IN ('acalc_project','acalc_layers','acalc_variants','acalc_spectra')",
                       (f"Acalc model metadata; schema version {SCHEMA_VERSION}",))
        self.migration_backup = str(backup)

    @staticmethod
    def _add_barrier_heights(db):
        """Preserve legacy per-point Z/M while adding barrier authoring controls."""
        for layer in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type='barrier'").fetchall():
            table = layer["table_name"]
            fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if "height_mode" not in fields:
                db.execute(f"ALTER TABLE \"{table}\" ADD COLUMN height_mode TEXT NOT NULL DEFAULT 'individual'")
            if "height_value" not in fields:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN height_value REAL NOT NULL DEFAULT 3')

    @staticmethod
    def _split_building_types(db):
        """Keep legacy layer identities, promoting layers with attached sources.

        The whole layer retains its variant membership, including buildings in
        a mixed layer which do not emit sound. Archived layers must also migrate
        so restoring them cannot turn stored component sources into objects.
        """
        for layer in db.execute("SELECT layer_id,table_name FROM acalc_layers WHERE feature_type='building'").fetchall():
            for row in db.execute(f'SELECT face_sources FROM "{layer["table_name"]}" WHERE face_sources IS NOT NULL'):
                if row[0] == "":
                    continue
                try:
                    empty = json.loads(row[0]) == []
                except (TypeError, ValueError):
                    empty = False
                # Preserve malformed attachments for ordinary source validation;
                # never discard them or silently turn their building into an object.
                if not empty:
                    db.execute("UPDATE acalc_layers SET feature_type='source_building' WHERE layer_id=?", (layer["layer_id"],))
                    break

    @staticmethod
    def _add_face_sources(db):
        for layer in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type IN ('building','source_building')").fetchall():
            table = layer["table_name"]
            fields = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if "face_sources" not in fields:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN face_sources TEXT')

    @staticmethod
    def _declare_point_mounting(db):
        """Store each attached point's mounting in place of the distance rule.

        Before schema 13 a zero distance meant a surface emitter and a positive
        distance a displaced point; record that meaning explicitly. Displacements
        below the minimum standoff stay undeclared and block calculation until
        the user chooses surface or standoff.
        """
        from .building_faces import MIN_STANDOFF_M
        for layer in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type IN ('building','source_building')").fetchall():
            table = layer["table_name"]
            if "face_sources" not in {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}:
                continue
            updates = []
            for row in db.execute(f'SELECT fid,face_sources FROM "{table}" WHERE face_sources IS NOT NULL').fetchall():
                sources = json.loads(row["face_sources"])
                if not isinstance(sources, list):
                    continue  # Left for read_face_sources to report.
                changed = False
                for source in sources:
                    if not isinstance(source, dict) or source.get("type") != "point" or "mounting" in source:
                        continue
                    distance = source.get("distance_m")
                    if type(distance) not in (int, float):
                        continue
                    if distance == 0 or distance >= MIN_STANDOFF_M:
                        source["mounting"] = "surface" if distance == 0 else "standoff"
                        changed = True
                if changed:
                    updates.append((json.dumps(sources, allow_nan=False), row["fid"]))
            if not updates:
                continue
            # Attribute-only update; see _remove_z_inference for the RTree triggers.
            spatial_triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND name LIKE 'rtree_%'", (table,)).fetchall()
            for trigger in spatial_triggers:
                db.execute('DROP TRIGGER "' + trigger['name'].replace('"', '""') + '"')
            db.executemany(f'UPDATE "{table}" SET face_sources=? WHERE fid=?', updates)
            for trigger in spatial_triggers:
                db.execute(trigger['sql'])

    @staticmethod
    def _add_absorption_assignments(db):
        for layer in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type IN ('building','source_building','barrier')").fetchall():
            table = layer["table_name"]
            columns = {row["name"]: row for row in db.execute(f'PRAGMA table_info("{table}")')}
            column = columns.get("absorption_spectrum_id")
            if column is None:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN absorption_spectrum_id TEXT')
            elif column["type"].strip().upper() != "TEXT" or column["notnull"]:
                raise ModelError("Cannot upgrade an existing absorption_spectrum_id field: it must be nullable TEXT.")

    @staticmethod
    def _create_calculation_options(db, settings=None, legacy=()):
        db.execute("""CREATE TABLE acalc_calculation (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            engine TEXT, settings_json TEXT NOT NULL CHECK(json_valid(settings_json)),
            legacy_settings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(legacy_settings_json))
        )""")
        db.execute("INSERT INTO acalc_calculation VALUES (1,?,?,?)",
                   (None if legacy else ISO_ENGINE, json.dumps(settings or default_settings()), json.dumps(list(legacy))))
        db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description) VALUES ('acalc_calculation','attributes','acalc_calculation','Shared Acalc calculation settings')")

    @staticmethod
    def _read_calculation(db):
        rows = db.execute("SELECT * FROM acalc_calculation").fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise ModelError("The model must contain one set of calculation settings.")
        row = rows[0]
        value = {"engine": row["engine"], "settings": json.loads(row["settings_json"]),
                 "legacy_settings": json.loads(row["legacy_settings_json"])}
        if value["engine"] not in (None, ISO_ENGINE):
            raise ModelError("Unsupported calculation engine.")
        value["settings"] = validate_settings(value["settings"])
        if not isinstance(value["legacy_settings"], list):
            raise ModelError("Invalid previous calculation settings.")
        for preset in value["legacy_settings"]:
            require_name(preset["name"])
            preset["settings"] = validate_settings(preset["settings"])
        return value

    def calculation(self):
        with self.connection(readonly=True) as db:
            return self._read_calculation(db)

    def save_calculation(self, settings):
        settings = validate_settings(settings)
        with self.connection() as db:
            db.execute("UPDATE acalc_calculation SET engine=?,settings_json=? WHERE singleton=1",
                       (ISO_ENGINE, json.dumps(settings, allow_nan=False)))

    @staticmethod
    def _create_calculation_schema(db):
        for statement in CALCULATION_SCHEMA:
            db.execute(statement)
        for table in CALCULATION_TABLES:
            db.execute("INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description) VALUES (?,'attributes',?,?)",
                       (table, table, f"Acalc current calculation results; schema version {SCHEMA_VERSION}"))

    @staticmethod
    def _remove_legacy_terrain(db):
        # The migration backup retains old group membership and raster samples.
        # Preserve authored geometry, variants' object layers and raster references.
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            if row[0].startswith("acalc_terrain_dirty_") or row[0].startswith("acalc_touch_acalc_elevation_rasters_"):
                db.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rasters = []
        if "acalc_elevation_rasters" in tables:
            rasters = db.execute("SELECT raster_id,name,path,band FROM acalc_elevation_rasters").fetchall()
        for table in ("acalc_terrain_cache", "acalc_terrain_members", "acalc_elevation_rasters", "acalc_terrain_groups"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
            db.execute("DELETE FROM gpkg_contents WHERE table_name=?", (table,))
        ModelStore._create_raster_schema(db)
        db.executemany("INSERT INTO acalc_elevation_rasters VALUES (?,?,?,?)", rasters)

    @staticmethod
    def _create_raster_schema(db):
        db.execute("""CREATE TABLE IF NOT EXISTS acalc_elevation_rasters (
            raster_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
            band INTEGER NOT NULL DEFAULT 1 CHECK(band>0)
        )""")
        db.execute("""INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description)
            VALUES ('acalc_elevation_rasters','attributes','acalc_elevation_rasters',
                    'Elevation raster references for object authoring; excluded from calculations')""")

    @staticmethod
    def _validate_raster_schema(db):
        required = {"raster_id", "name", "path", "band"}
        if not required <= {row["name"] for row in db.execute("PRAGMA table_info(acalc_elevation_rasters)")}:
            raise ModelError("Invalid elevation raster reference schema.")
        for row in db.execute("SELECT * FROM acalc_elevation_rasters"):
            require_name(row["name"])
            if not row["path"] or type(row["band"]) is not int or row["band"] < 1:
                raise ModelError("Invalid elevation raster reference.")

    def elevation_rasters(self):
        with self.connection(readonly=True) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM acalc_elevation_rasters ORDER BY name COLLATE NOCASE,raster_id")]

    def add_elevation_raster(self, path, name=None, band=1):
        from .raster_elevation import RasterElevation
        path = str(Path(path).expanduser().resolve())
        name = require_name(name or Path(path).stem)
        # Validate metadata without importing or downsampling the raster.
        with RasterElevation(path, self.metadata()["crs_wkt"], band):
            pass
        raster_id = new_id()
        with self.connection() as db:
            db.execute("INSERT INTO acalc_elevation_rasters VALUES (?,?,?,?)", (raster_id, name, path, band))
        return raster_id

    def generated_terrain_ids(self):
        """Identify mesh layers independently of their breakline storage format."""
        with self.connection(readonly=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acalc_raster_terrain'").fetchone():
                return set()
            return {row[0] for row in db.execute('SELECT layer_id FROM acalc_raster_terrain')}

    def generated_terrains(self, *, archived=False):
        with self.connection(readonly=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acalc_raster_terrain'").fetchone():
                return []
            return [dict(layer_id=row['layer_id'], name=row['name'], archived=bool(row['archived']),
                         generation=json.loads(row['generation_json'])) for row in db.execute(
                """SELECT t.*,l.name,l.archived FROM acalc_raster_terrain t
                   JOIN acalc_layers l ON l.layer_id=t.layer_id WHERE l.archived=?
                   ORDER BY l.created_at,l.layer_id""", (int(archived),))]

    def remove_elevation_raster(self, raster_id):
        with self.connection() as db:
            if not db.execute("DELETE FROM acalc_elevation_rasters WHERE raster_id=?", (raster_id,)).rowcount:
                raise ModelError("This elevation raster no longer exists.")

    @staticmethod
    def _create_input_triggers(db, feature_table=None):
        """Keep result freshness visible even when edits bypass the plugin.

        The project guard advances by at least a millisecond when edits share a
        clock tick. Result-table writes deliberately have no touch trigger.
        """
        db.execute("""CREATE TRIGGER IF NOT EXISTS acalc_project_monotonic_modified
            AFTER UPDATE ON acalc_project
            WHEN NEW.modified_at <= OLD.modified_at
            BEGIN
                UPDATE acalc_project
                SET modified_at=strftime('%Y-%m-%dT%H:%M:%f+00:00',OLD.modified_at,'+0.001 seconds')
                WHERE singleton=1;
            END""")
        if feature_table is not None:
            tables = [feature_table]
        else:
            tables = ["acalc_spectra", "acalc_variants", "acalc_layers", "acalc_calculation"]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acalc_directivities'").fetchone():
                tables.append("acalc_directivities")
            tables.extend(row["table_name"] for row in db.execute("SELECT table_name FROM acalc_layers"))
        for table in tables:
            if table not in ("acalc_spectra", "acalc_variants", "acalc_layers", "acalc_calculation", "acalc_directivities") and not re.fullmatch(r"model_[a-f0-9]{32}", table):
                raise ModelError("Cannot track changes to an unsupported feature table.")
            for event in ("INSERT", "UPDATE", "DELETE"):
                name = f"acalc_touch_{table}_{event.lower()}"
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS "{name}" AFTER {event} ON "{table}"
                    BEGIN
                        UPDATE acalc_project SET modified_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
                        WHERE singleton=1;
                    END''')

    @staticmethod
    def _validate_calculation_schema(db, version=SCHEMA_VERSION):
        required = {
            "acalc_calculation_runs": {
                "run_id", "project_id", "variant_id", "variant_name", "engine",
                "engine_version", "standard", "started_at", "finished_at", "input_sha256",
                "project_modified_at", "status", "settings_json", "source_count", "receiver_count",
                "elapsed_seconds", "warnings_json", "metadata_json",
            },
            "acalc_receiver_results": {
                "run_id", "layer_id", "object_id", "receiver_name", "octave_levels_json",
                "broadband_z_db", "broadband_a_db", "metadata_json",
            },
        }
        if version < 5:
            fields = required["acalc_calculation_runs"]
            fields.difference_update(("variant_id", "variant_name"))
            fields.update(("configuration_id", "configuration_name"))
        for table, fields in required.items():
            actual = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not fields <= actual:
                raise ModelError(f"Invalid calculation results schema: {table}.")

    @staticmethod
    def validate_definition(value, active_ids, version=SCHEMA_VERSION, terrain_ids=None):
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != version):
            raise ModelError("Unsupported variant definition version.")
        ids = value.get("layer_ids")
        if (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= active_ids):
            raise ModelError("A variant references invalid or removed layers.")
        if version == 6:
            groups = value.get("terrain_group_ids")
            if (not isinstance(groups, list) or not all(isinstance(i, str) for i in groups)
                    or len(groups) != len(set(groups)) or "base" in groups
                    or (terrain_ids is not None and not set(groups) <= terrain_ids)):
                raise ModelError("A variant references invalid or removed local terrain groups.")
            if set(value) != {"schema_version", "layer_ids", "terrain_group_ids"}:
                raise ModelError("Variants contain layer and terrain group membership only. Save parameters in the calculation window.")
            return
        if version >= 5:
            if set(value) != {"schema_version", "layer_ids"}:
                raise ModelError("Variants contain layer membership only. Save parameters in the calculation window.")
            return
        if not isinstance(value.get("settings"), dict):
            raise ModelError("Calculation settings must be a JSON object.")
        engine = value.get("engine")
        if engine is None:
            return
        if version < 3 or engine != ISO_ENGINE:
            raise ModelError("Unsupported calculation engine. Choose ISO 9613-2:2024 or leave the variant unassigned.")
        validate_settings(value["settings"])

    def metadata(self):
        with self.connection(readonly=True) as db:
            return dict(db.execute("SELECT * FROM acalc_project").fetchone())

    def update_metadata(self, name, author, description, vertical_datum, metadata=None):
        with self.connection() as db:
            db.execute("UPDATE acalc_project SET name=?,author=?,description=?,vertical_datum=?,modified_at=? WHERE singleton=1",
                       (require_name(name), author, description, vertical_datum.strip() or "Unspecified", now()))
            if metadata is not None:
                if not isinstance(metadata, dict):
                    raise ModelError("Additional metadata must be a JSON object.")
                if metadata.get("elevation_unit", metadata.get("input_length_unit", "m")) not in ("m", "ft"):
                    raise ModelError("Choose meters or feet for project inputs.")
                db.execute("UPDATE acalc_project SET metadata_json=?", (json.dumps(metadata, allow_nan=False),))

    def touch(self):
        with self.connection() as db:
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    @staticmethod
    def _read_spectra(db, version=SCHEMA_VERSION):
        result, names = [], set()
        for row in db.execute("SELECT spectrum_id,name,spectrum_json FROM acalc_spectra ORDER BY name COLLATE NOCASE,spectrum_id"):
            try:
                if str(uuid.UUID(row["spectrum_id"])) != row["spectrum_id"]:
                    raise ValueError("Spectrum IDs must be canonical UUIDs.")
                name = require_name(row["name"])
                value = json.loads(row["spectrum_json"])
                validate_spectrum(value)
                if version >= 5 and value["kind"] == "sound_power" and value["schema_version"] != 2:
                    raise ModelError("This model requires dB-only spectrum definitions (version 2).")
                key = (value["kind"], name.casefold())
                if key in names:
                    raise ModelError("Spectrum names must be unique within each catalog.")
                names.add(key)
                result.append({"spectrum_id": row["spectrum_id"], "name": name, "spectrum": value})
            except (ValueError, TypeError, AttributeError) as error:
                raise ModelError(f"Invalid spectrum {row['name']!r}: {error}") from error
        return result

    def spectra(self, kind=None):
        if kind is not None and kind not in SPECTRUM_KINDS:
            raise ModelError("Unsupported spectrum type.")
        with self.connection(readonly=True) as db:
            return [row for row in self._read_spectra(db) if kind is None or row["spectrum"]["kind"] == kind]

    def save_spectrum(self, spectrum_id, name, value):
        return self._write_spectrum(spectrum_id, name, value, create=False)

    def apply_back_calculation(self, relationships, proposals):
        """Atomically replace fitted current values while keeping initial guesses."""
        originals = {item["id"]: item for item in relationships["spectra"]}
        if not proposals or any(identity not in originals or originals[identity]["mode"] != "adjustable"
                                for identity in proposals):
            raise ModelError("The fit contains an unavailable or locked sound power spectrum.")
        from .api_model import _revision
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            project = db.execute("SELECT modified_at,project_id FROM acalc_project WHERE singleton=1").fetchone()
            if project[0] != relationships["project_modified_at"]:
                raise ModelError("The model changed since the fit. Evaluate the Measurements again.")
            if (project["project_id"] != relationships["project_id"]
                    or _revision(db, relationships["revision_tables"]) != relationships["model_revision"]):
                raise ModelError("The model or Measurements changed since the fit. Evaluate the Measurements again.")
            records = {row["spectrum_id"]: row for row in self._read_spectra(db)}
            for identity, values in proposals.items():
                record = records.get(identity)
                original = originals[identity]
                if (record is None or record["spectrum"]["kind"] != "sound_power"
                        or record["spectrum"].get("back_calculation", "locked") != "adjustable"
                        or record["spectrum"]["values"] != original["current"]
                        or record["spectrum"]["initial_values"] != original["initial"]):
                    raise ModelError("A fitted spectrum changed since evaluation. Run the fit again.")
                value = dict(record["spectrum"], values=list(values))
                validate_spectrum(value)
                db.execute("UPDATE acalc_spectra SET spectrum_json=? WHERE spectrum_id=?",
                           (json.dumps(value, allow_nan=False), identity))
            timestamp = now()
            if timestamp <= project[0]:
                timestamp = (datetime.fromisoformat(project[0]) + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
            db.execute("UPDATE acalc_project SET modified_at=?", (timestamp,))
            db.execute("UPDATE gpkg_contents SET last_change=? WHERE table_name='acalc_spectra'", (timestamp,))

    def add_spectrum(self, name, value):
        spectrum_id = new_id()
        self._write_spectrum(spectrum_id, name, value, create=True)
        return spectrum_id

    def _write_spectrum(self, spectrum_id, name, value, create):
        name = require_name(name)
        value = upgrade_spectrum(value)
        with self.connection() as db:
            rows = self._read_spectra(db)
            current = next((row for row in rows if row["spectrum_id"] == spectrum_id), None)
            if not create and current is None:
                raise ModelError("This spectrum no longer exists.")
            if current and current["spectrum"]["kind"] != value["kind"]:
                raise ModelError("An existing spectrum cannot change type.")
            if any(row["spectrum_id"] != spectrum_id and row["spectrum"]["kind"] == value["kind"]
                   and row["name"].casefold() == name.casefold() for row in rows):
                raise ModelError("A spectrum with this name already exists in this catalog.")
            if value["kind"] == "sound_power":
                for layer in db.execute("SELECT * FROM acalc_layers").fetchall():
                    for source_type in ("point", "area"):
                        if (not compatible_power(value, source_type + "_source")
                                and self._face_spectrum_usage(db, layer, spectrum_id, source_type)):
                            raise ModelError(f'This power basis is incompatible with building {source_type} sources in “{layer["name"]}”.')
                    if LAYER_TYPES[layer["feature_type"]].spectrum and not compatible_power(value, layer["feature_type"]):
                        if db.execute(f'SELECT 1 FROM "{layer["table_name"]}" WHERE spectrum_id=? LIMIT 1', (spectrum_id,)).fetchone():
                            raise ModelError(f'This power basis is incompatible with sources in “{layer["name"]}”.')
            if create:
                db.execute("INSERT INTO acalc_spectra(spectrum_id,name,spectrum_json) VALUES (?,?,?)",
                           (spectrum_id, name, json.dumps(value, allow_nan=False)))
            else:
                db.execute("UPDATE acalc_spectra SET name=?,spectrum_json=? WHERE spectrum_id=?",
                           (name, json.dumps(value, allow_nan=False), spectrum_id))
            self._touch_spectra(db)

    @staticmethod
    def _touch_spectra(db):
        timestamp = now()
        db.execute("UPDATE acalc_project SET modified_at=?", (timestamp,))
        db.execute("UPDATE gpkg_contents SET last_change=? WHERE table_name='acalc_spectra'", (timestamp,))

    def delete_spectrum(self, spectrum_id):
        with self.connection() as db:
            for layer in db.execute("SELECT * FROM acalc_layers").fetchall():
                column = self._spectrum_reference_column(layer["feature_type"])
                if self._face_spectrum_usage(db, layer, spectrum_id) or (column and db.execute(
                        f'SELECT 1 FROM "{layer["table_name"]}" WHERE "{column}"=? LIMIT 1', (spectrum_id,)).fetchone()):
                    raise ModelError(f"This spectrum is used by “{layer['name']}”. Remove its object assignments first (including removed layers).")
            if not db.execute("DELETE FROM acalc_spectra WHERE spectrum_id=?", (spectrum_id,)).rowcount:
                raise ModelError("This spectrum no longer exists.")
            self._touch_spectra(db)

    @staticmethod
    def _face_spectrum_usage(db, layer, spectrum_id, source_type=None):
        if layer["feature_type"] not in BUILDING_TYPES:
            return 0
        from .building_faces import read_face_sources
        return sum(source["spectrum_id"] == spectrum_id
                   for row in db.execute(f'SELECT face_sources FROM "{layer["table_name"]}"')
                   for source in read_face_sources(row[0])
                   if source_type is None or source.get("type", "area") == source_type)

    @staticmethod
    def _spectrum_reference_column(feature_type):
        if LAYER_TYPES[feature_type].spectrum:
            return "spectrum_id"
        if feature_type in BUILDING_TYPES or feature_type == "barrier":
            return "absorption_spectrum_id"
        return None

    def spectrum_usage(self, spectrum_id):
        """Saved catalog references, including objects in archived layers."""
        usage = []
        with self.connection(readonly=True) as db:
            for layer in db.execute("SELECT * FROM acalc_layers").fetchall():
                column = self._spectrum_reference_column(layer["feature_type"])
                if column:
                    count = db.execute(f'SELECT COUNT(*) FROM "{layer["table_name"]}" WHERE "{column}"=?',
                                       (spectrum_id,)).fetchone()[0]
                    count += self._face_spectrum_usage(db, layer, spectrum_id)
                    if count:
                        usage.append({"layer_id": layer["layer_id"], "name": layer["name"],
                                      "count": count, "archived": bool(layer["archived"])})
        return usage

    def layers(self, archived=False):
        with self.connection(readonly=True) as db:
            return [dict(row) for row in db.execute("""SELECT * FROM acalc_layers
                WHERE archived=? ORDER BY sort_order,created_at""", (int(archived),))]

    def add_layer(self, kind_key, name, *, populate=None, register=None):
        """Create a model layer; optional population must finish before registration.

        ``populate`` receives the new OGR layer. A failed population removes the
        new table, leaving no partial layer or variant membership behind.
        ``register(db, layer_id)`` may add metadata within the registration
        transaction; its failure also removes the newly populated feature table.
        """
        name = require_name(name)
        kind = LAYER_TYPES[kind_key]
        layer_id = new_id()
        table = "model_" + layer_id.replace("-", "")
        srs = osr.SpatialReference()
        srs.ImportFromWkt(self.metadata()["crs_wkt"])
        geometry_type = {"Point": ogr.wkbPoint, "LineString": ogr.wkbLineString, "Polygon": ogr.wkbPolygon}[kind.geometry]
        if kind.elevation:
            geometry_type = ogr.GT_SetZ(geometry_type)
        if kind.height:
            geometry_type = ogr.GT_SetM(geometry_type)
        dataset = ogr.Open(self.path, update=1)
        if dataset is None:
            raise ModelError("Could not open the GeoPackage for writing.")
        try:
            layer = dataset.CreateLayer(table, srs, geometry_type,
                                        ["FID=fid", "GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"])
            if layer is None:
                raise ModelError("Could not create the model layer.")
            for field_name, field_type in (("object_id", ogr.OFTString), ("name", ogr.OFTString),
                                           ("notes", ogr.OFTString), ("enabled", ogr.OFTInteger)):
                field = ogr.FieldDefn(field_name, field_type)
                if field_name == "enabled":
                    field.SetDefault("1")
                if layer.CreateField(field) != 0:
                    raise ModelError(f"Could not create field {field_name}.")
            fields = [("ground_mode", ogr.OFTString, "'per_point'")] if kind.ground_points else []
            if kind.key == "ground_absorption":
                fields.append(("ground_factor", ogr.OFTReal, "0"))
            if kind.key == "trees":
                from .trees import DEFAULT_CANOPY_HEIGHT
                fields.append(("canopy_height", ogr.OFTReal, str(DEFAULT_CANOPY_HEIGHT)))
            if kind.height_controls:
                fields += [("height_mode", ogr.OFTString, "'individual'"),
                           ("height_value", ogr.OFTReal, "3" if kind.key == "barrier" else "6")]
            if kind.is_building:
                fields.append(("face_sources", ogr.OFTString, None))
            if kind.key == "point_source":
                fields += [("directivity_id", ogr.OFTString, None), ("directivity_orientation", ogr.OFTString, None)]
            if (kind.is_building or kind.key == "barrier"):
                fields.append(("reflection_coefficient", ogr.OFTReal, None))
                fields.append(("absorption_spectrum_id", ogr.OFTString, None))
            if kind.key == "area_receiver":
                fields += [("height_m", ogr.OFTReal, "1.5"), ("spacing_m", ogr.OFTReal, "10")]
            if kind.key == "measurement":
                fields += [(field, ogr.OFTReal, None) for field in ALL_MEASUREMENT_FIELDS]
            for field_name, field_type, default in fields:
                field = ogr.FieldDefn(field_name, field_type)
                if default is not None:
                    field.SetDefault(default)
                if layer.CreateField(field) != 0:
                    raise ModelError(f"Could not create field {field_name}.")
            if kind.spectrum:
                if layer.CreateField(ogr.FieldDefn("spectrum_id", ogr.OFTString)) != 0:
                    raise ModelError("Could not create field spectrum_id.")
            if populate is not None:
                # Materialize OGR's deferred table creation before a population
                # transaction. Rollback should remove rows, leaving a real table
                # for the failure cleanup below to delete.
                if layer.SyncToDisk() != 0:
                    raise ModelError("Could not create the model table.")
                populate(layer)
            if layer.SyncToDisk() != 0:
                raise ModelError("Could not save the model layer.")
            layer = None
        except Exception:
            layer = None
            dataset.DeleteLayer(table)
            raise
        finally:
            dataset = None
        try:
            with self.connection() as db:
                order = db.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM acalc_layers").fetchone()[0]
                db.execute("INSERT INTO acalc_layers VALUES (?,?,?,?,?,0,?)",
                           (layer_id, table, name, kind_key, order, now()))
                self._create_input_triggers(db, table)
                if register is not None:
                    register(db, layer_id)
                db.execute("UPDATE acalc_project SET modified_at=?", (now(),))
        except Exception:
            dataset = ogr.Open(self.path, update=1)
            if dataset:
                dataset.DeleteLayer(table)
            dataset = None
            raise
        return next(row for row in self.layers() if row["layer_id"] == layer_id)

    def rename_layer(self, layer_id, name):
        with self.connection() as db:
            db.execute("UPDATE acalc_layers SET name=? WHERE layer_id=?", (require_name(name), layer_id))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def archive_layer(self, layer_id):
        with self.connection() as db:
            db.execute("UPDATE acalc_layers SET archived=1 WHERE layer_id=?", (layer_id,))
            for row in db.execute("SELECT * FROM acalc_variants").fetchall():
                value = json.loads(row["definition_json"])
                value["layer_ids"] = [i for i in value["layer_ids"] if i != layer_id]
                db.execute("UPDATE acalc_variants SET definition_json=?,modified_at=? WHERE variant_id=?",
                           (json.dumps(value), now(), row["variant_id"]))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def restore_layer(self, layer_id):
        with self.connection() as db:
            db.execute("UPDATE acalc_layers SET archived=0 WHERE layer_id=?", (layer_id,))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def variants(self):
        with self.connection(readonly=True) as db:
            result = [dict(row) for row in db.execute("SELECT * FROM acalc_variants ORDER BY created_at,variant_id")]
        for row in result:
            row["definition"] = json.loads(row.pop("definition_json"))
        return result

    @staticmethod
    def _decode_calculation_run(row, project):
        value = dict(row)
        try:
            for key, expected in (("settings", dict), ("warnings", list), ("metadata", dict)):
                value[key] = json.loads(value.pop(key + "_json"))
                if not isinstance(value[key], expected):
                    raise ValueError(f"{key} has an invalid type")
            if value["status"] != "complete":
                raise ValueError("only complete runs may be stored")
        except (ValueError, TypeError, KeyError) as error:
            raise ModelError(f"Invalid calculation run {value.get('run_id')}: {error}") from error
        value["stale"] = (value["project_id"] != project["project_id"]
                          or value["project_modified_at"] != project["modified_at"])
        # This cutoff belongs to the old bundled engine only; independent APIs
        # have their own versions and an explicit captured capability profile.
        parts = value["engine_version"].split(".")
        if (value["metadata"].get("transport") != "http_json_v1"
                and len(parts) == 3 and all(part.isdecimal() for part in parts)):
            value["stale"] |= tuple(map(int, parts)) < (0, 24, 0)
        return value

    def calculation_runs(self, variant_id=None):
        """Return the latest completed calculation for each variant, newest first.

        Staleness is conservative: any saved model modification invalidates the
        display's current-result status. The engine's input_sha256 additionally
        identifies the exact calculation inputs, excluding stored results.
        """
        with self.connection(readonly=True) as db:
            project = db.execute("SELECT project_id,modified_at FROM acalc_project").fetchone()
            rows = self._latest_calculation_rows(db, variant_id)
            return [self._decode_calculation_run(row, project) for row in rows]

    @staticmethod
    def _latest_calculation_rows(db, variant_id=None, columns="current.*"):
        where = " AND current.variant_id=?" if variant_id is not None else ""
        return db.execute("SELECT " + columns + """ FROM acalc_calculation_runs AS current
            WHERE NOT EXISTS (SELECT 1 FROM acalc_calculation_runs AS newer
                WHERE newer.variant_id=current.variant_id AND
                (newer.finished_at>current.finished_at OR
                 (newer.finished_at=current.finished_at AND newer.run_id>current.run_id)))""" + where
                          + " ORDER BY current.finished_at DESC,current.run_id DESC",
                          (variant_id,) if variant_id is not None else ()).fetchall()

    def calculation_run_summaries(self):
        """Latest run/variant identities for navigation, without diagnostic JSON."""
        with self.connection(readonly=True) as db:
            return [dict(row) for row in self._latest_calculation_rows(
                db, columns="current.run_id,current.variant_id,current.variant_name")]

    def latest_calculation_run(self, variant_id):
        runs = self.calculation_runs(variant_id)
        return runs[0] if runs else None

    def receiver_results(self, variant_id=None, layer_id=None, object_id=None, run_id=None):
        """Read current results; an optional run ID pins the calculation being read.

        Receivers are identified by stable layer and normalized object UUIDs, not
        GeoPackage feature IDs. Octave nulls mean zero received energy; unknown
        bands cannot occur in a successfully completed run.
        """
        if run_id is None:
            if variant_id is None:
                raise ModelError("Choose a variant or calculation run.")
            run = self.latest_calculation_run(variant_id)
            if run is None:
                return []
            run_id = run["run_id"]
        with self.connection(readonly=True) as db:
            project = db.execute("SELECT project_id,modified_at FROM acalc_project").fetchone()
            run_row = db.execute("SELECT * FROM acalc_calculation_runs WHERE run_id=?", (run_id,)).fetchone()
            if run_row is None:
                return []
            run = self._decode_calculation_run(run_row, project)
            if variant_id is not None and variant_id != run["variant_id"]:
                raise ModelError("This calculation run belongs to a different variant.")
            clauses, parameters = ["run_id=?"], [run_id]
            if layer_id is not None:
                clauses.append("layer_id=?")
                parameters.append(layer_id)
            if object_id is not None:
                try:
                    object_id = str(uuid.UUID(str(object_id)))
                except (ValueError, TypeError, AttributeError) as error:
                    raise ModelError("Receiver object ID must be a UUID.") from error
                clauses.append("(object_id=? OR json_extract(metadata_json,'$.area_receiver_id')=?)")
                parameters.extend((object_id, object_id))
            rows = db.execute("SELECT * FROM acalc_receiver_results WHERE " + " AND ".join(clauses)
                              + " ORDER BY layer_id,receiver_name COLLATE NOCASE,object_id", parameters).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                levels = json.loads(value.pop("octave_levels_json"))
                metadata = json.loads(value.pop("metadata_json"))
                if not isinstance(levels, list) or len(levels) != 8 or not isinstance(metadata, dict):
                    raise ValueError("expected eight octave levels and metadata object")
                numbers = levels + [value["broadband_z_db"], value["broadband_a_db"]]
                if any(number is not None and (type(number) not in (int, float) or not math.isfinite(number))
                       for number in numbers):
                    raise ValueError("levels must be finite numbers or zero-energy nulls")
                silent = all(level is None for level in levels)
                if (value["broadband_a_db"] is None) != silent:
                    raise ValueError("broadband nulls must agree with zero received energy")
                value.update(octave_levels=levels, metadata=metadata, stale=run["stale"])
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                raise ModelError(f"Invalid receiver result {value.get('object_id')}: {error}") from error
            result.append(value)
        return result

    def measurements(self, variant_id, layer_id=None, object_id=None):
        """Return authored levels in a variant, independent of calculation runs."""
        if object_id is not None:
            try:
                object_id = str(uuid.UUID(str(object_id)))
            except (ValueError, TypeError, AttributeError) as error:
                raise ModelError("Measurement object ID must be a UUID.") from error
        with self.connection(readonly=True) as db:
            row = db.execute("SELECT definition_json FROM acalc_variants WHERE variant_id=?", (variant_id,)).fetchone()
            if row is None:
                return []
            included = set(json.loads(row[0])["layer_ids"])
            layers = db.execute("SELECT layer_id,table_name,name FROM acalc_layers WHERE feature_type='measurement' AND archived=0 ORDER BY sort_order,layer_id").fetchall()
            result = []
            for layer in layers:
                if layer["layer_id"] not in included or layer_id is not None and layer_id != layer["layer_id"]:
                    continue
                table = layer["table_name"]
                if not re.fullmatch(r"model_[a-f0-9]{32}", table):
                    raise ModelError("Invalid Measurement layer table.")
                for feature in db.execute(f'SELECT object_id,name,notes,{",".join(ALL_MEASUREMENT_FIELDS)} FROM "{table}" ORDER BY name COLLATE NOCASE,object_id'):
                    try:
                        identity = str(uuid.UUID(str(feature["object_id"])))
                    except (ValueError, TypeError, AttributeError) as error:
                        raise ModelError(f"Invalid Measurement object ID in {layer['name']}.") from error
                    if object_id is not None and identity != object_id:
                        continue
                    values = resolved_measurement({field: feature[field] for field in ALL_MEASUREMENT_FIELDS})
                    result.append({"layer_id": layer["layer_id"], "layer_name": layer["name"],
                                   "object_id": identity, "receiver_name": feature["name"],
                                   "notes": feature["notes"], "octave_levels": [values[field] for field in MEASUREMENT_FIELDS],
                                   "broadband_a_db": values["broadband_a_db"]})
        return result

    def save_variant(self, variant_id, name, value):
        with self.connection() as db:
            self.validate_definition(value, {row["layer_id"] for row in db.execute(
                "SELECT layer_id FROM acalc_layers WHERE archived=0")})
            db.execute("UPDATE acalc_variants SET name=?,definition_json=?,modified_at=? WHERE variant_id=?",
                       (require_name(name), json.dumps(value, allow_nan=False), now(), variant_id))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def add_variant(self, name, value=None):
        value = definition() if value is None else value
        config_id = new_id()
        with self.connection() as db:
            self.validate_definition(value, {row["layer_id"] for row in db.execute(
                "SELECT layer_id FROM acalc_layers WHERE archived=0")})
            db.execute("INSERT INTO acalc_variants VALUES (?,?,?,?,?)",
                       (config_id, require_name(name), json.dumps(value), now(), now()))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))
        return config_id

    def delete_variant(self, config_id):
        with self.connection() as db:
            if db.execute("SELECT COUNT(*) FROM acalc_variants").fetchone()[0] <= 1:
                raise ModelError("Keep at least one variant in the project.")
            db.execute("DELETE FROM acalc_variants WHERE variant_id=?", (config_id,))
            db.execute("UPDATE acalc_project SET modified_at=?", (now(),))

    def export_variant(self, config_id):
        config = next(c for c in self.variants() if c["variant_id"] == config_id)
        calculation = self.calculation()
        ids = config["definition"]["layer_ids"]
        spectra = self.spectra()
        directivities = self.directivities()
        directivity_ids = {row["directivity_id"] for row in directivities}
        powers = {row["spectrum_id"] for row in spectra if row["spectrum"]["kind"] == "sound_power"}
        spectra_by_id = {row["spectrum_id"]: row["spectrum"] for row in spectra}
        materials = {row["spectrum_id"] for row in spectra if row["spectrum"]["kind"] == "absorption"}
        layers = [r for r in self.layers() if r["layer_id"] in ids]
        with self.connection(readonly=True) as db:
            for layer in layers:
                if layer["feature_type"] == "point_source":
                    from .directivity import read_orientation
                    for row in db.execute(f'SELECT directivity_id,directivity_orientation FROM "{layer["table_name"]}"'):
                        if row[0] and row[0] not in directivity_ids:
                            raise ModelError(f"{layer['name']}: a source references a missing directionality.")
                        read_orientation(row[1])
                if LAYER_TYPES[layer["feature_type"]].spectrum:
                    for row in db.execute(f'SELECT DISTINCT spectrum_id FROM "{layer["table_name"]}"'):
                        if row[0] and row[0] not in powers:
                            raise ModelError(f"{layer['name']}: a source references a missing or non-sound-power spectrum.")
                if (layer["feature_type"] in BUILDING_TYPES or layer["feature_type"] == "barrier"):
                    for row in db.execute(f'SELECT DISTINCT absorption_spectrum_id FROM "{layer["table_name"]}"'):
                        if row[0] and row[0] not in materials:
                            raise ModelError(f"{layer['name']}: an object references a missing or non-absorption spectrum.")
                if layer["feature_type"] in BUILDING_TYPES:
                    from .building_faces import read_face_sources
                    for row in db.execute(f'SELECT face_sources FROM "{layer["table_name"]}"'):
                        sources = read_face_sources(row[0])
                        if sources and layer["feature_type"] == "building":
                            raise ModelError(f"{layer['name']}: Buildings cannot contain attached sources. Use Source Buildings.")
                        for source in sources:
                            if source["spectrum_id"] and source["spectrum_id"] not in powers:
                                raise ModelError(f"{layer['name']}: a face source references a missing sound power spectrum.")
                            if (source["spectrum_id"] and not compatible_power(
                                    spectra_by_id[source["spectrum_id"]], source.get("type", "area") + "_source")):
                                raise ModelError(f"{layer['name']}: a face source references an incompatible power basis.")
        return {"format": "acalc-model-variant", "schema_version": SCHEMA_VERSION,
                "project_id": self.metadata()["project_id"], "geopackage": Path(self.path).name,
                "variant_id": config_id, "name": config["name"],
                "definition": config["definition"],
                "calculation": {key: calculation[key] for key in ("engine", "settings")},
                "layers": layers, "spectra": spectra, "directivities": directivities}
