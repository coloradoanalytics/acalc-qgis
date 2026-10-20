# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""GeoPackage project-local directionality catalog and assignment migration."""

import json
import uuid

from .directivity import validate_definition
from .schema import ModelError, new_id


class DirectivityStore:
    @staticmethod
    def _create_directivity_schema(db):
        db.execute("""CREATE TABLE IF NOT EXISTS acalc_directivities (
            directivity_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            definition_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0)
        )""")
        db.execute("""INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description)
            VALUES ('acalc_directivities','attributes','acalc_directivities','Acalc project directionality library')""")
        for row in db.execute("SELECT table_name FROM acalc_layers WHERE feature_type='point_source'").fetchall():
            table = row[0]
            columns = {r["name"]: r for r in db.execute(f'PRAGMA table_info("{table}")')}
            for field in ("directivity_id", "directivity_orientation"):
                if field not in columns:
                    db.execute(f'ALTER TABLE "{table}" ADD COLUMN {field} TEXT')
                elif columns[field]["type"].upper() != "TEXT" or columns[field]["notnull"]:
                    raise ModelError(f"Cannot upgrade {table}: {field} must be nullable TEXT.")

    @staticmethod
    def _read_directivities(db):
        from .storage import require_name
        result, names = [], set()
        for row in db.execute("SELECT * FROM acalc_directivities ORDER BY name COLLATE NOCASE,directivity_id"):
            try:
                if str(uuid.UUID(row["directivity_id"])) != row["directivity_id"]:
                    raise ValueError("Directionality IDs must be canonical UUIDs.")
                name = require_name(row["name"])
                if name.casefold() in names or type(row["revision"]) is not int or row["revision"] < 1:
                    raise ValueError("Invalid directionality name or revision.")
                names.add(name.casefold())
                value = validate_definition(json.loads(row["definition_json"]))
                result.append({"directivity_id": row["directivity_id"], "name": name,
                               "definition": value, "revision": row["revision"]})
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ModelError(f"Invalid directionality {row['name']!r}: {error}") from error
        return result

    def directivities(self):
        with self.connection(readonly=True) as db:
            return self._read_directivities(db)

    def add_directivity(self, name, value):
        identity = new_id()
        self._write_directivity(identity, name, value, create=True)
        return identity

    def save_directivity(self, identity, name, value, expected_revision=None):
        self._write_directivity(identity, name, value, expected_revision=expected_revision)

    def _write_directivity(self, identity, name, value, create=False, expected_revision=None):
        from .storage import now, require_name
        name = require_name(name)
        validate_definition(value)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            records = self._read_directivities(db)
            current = next((r for r in records if r["directivity_id"] == identity), None)
            if not create and not current:
                raise ModelError("This directionality no longer exists.")
            if current and expected_revision is not None and current["revision"] != expected_revision:
                raise ModelError("This directionality changed in another editor. Reopen it before saving.")
            if any(r["directivity_id"] != identity and r["name"].casefold() == name.casefold() for r in records):
                raise ModelError("A directionality with this name already exists.")
            data = json.dumps(value, allow_nan=False)
            if create:
                db.execute("INSERT INTO acalc_directivities VALUES (?,?,?,1)", (identity, name, data))
            else:
                db.execute("UPDATE acalc_directivities SET name=?,definition_json=?,revision=revision+1 WHERE directivity_id=?",
                           (name, data, identity))
            db.execute("UPDATE gpkg_contents SET last_change=? WHERE table_name='acalc_directivities'", (now(),))

    def directivity_usage(self, identity):
        with self.connection(readonly=True) as db:
            return self._directivity_usage(db, identity)

    @staticmethod
    def _directivity_usage(db, identity):
        result = []
        for layer in db.execute("SELECT * FROM acalc_layers WHERE feature_type='point_source'").fetchall():
            count = db.execute(f'SELECT COUNT(*) FROM "{layer["table_name"]}" WHERE directivity_id=?', (identity,)).fetchone()[0]
            if count:
                result.append({"layer_id": layer["layer_id"], "name": layer["name"], "count": count,
                               "archived": bool(layer["archived"])})
        return result

    def delete_directivity(self, identity):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            usage = self._directivity_usage(db, identity)
            if usage:
                raise ModelError("Directionality is assigned in: " + ", ".join(r["name"] for r in usage) +
                                 ". Clear assignments first, including removed layers.")
            if not db.execute("DELETE FROM acalc_directivities WHERE directivity_id=?", (identity,)).rowcount:
                raise ModelError("This directionality no longer exists.")
