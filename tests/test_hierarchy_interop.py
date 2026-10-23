# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Optional sibling-Core acceptance, using its actual generated fixture.

ACALC_CORE_HIERARCHY_FIXTURE can select a frozen Core fixture. The regular
portable-project tests remain self-contained when Core is not checked out.
"""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from tests.test_model import APP
from acalc.portable_project import encode, export_model, file_bytes, import_model, models, read


FIXTURE = Path(os.environ.get("ACALC_CORE_HIERARCHY_FIXTURE",
    str(Path(__file__).resolve().parents[2] / "core/hierarchy/testdata/heterogeneous.acalc-project.json")))


class HierarchyInteropTests(unittest.TestCase):
    @unittest.skipUnless(FIXTURE.is_file(), "Core's generated hierarchy fixture is not present")
    def test_native_model_edit_preserves_heterogeneous_core_project(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        document = read(FIXTURE)
        self.assertTrue(models(document), "Core fixture must include an actual native QGIS model")
        model = models(document)[0]
        native_id = model["payload"]["extensions"]["acalc-qgis"]["geopackage_file_id"]
        native_record = next(f for f in document["files"] if f["id"] == native_id)
        expected_native = file_bytes(native_record, FIXTURE.parent)
        imported = import_model(FIXTURE, root / "heterogeneous-working", model["id"])
        self.assertEqual(Path(imported.path).read_bytes(), expected_native)
        self.assertTrue(imported.receiver_results(imported.variants()[0]["variant_id"]))
        imported.update_metadata("Edited native model", "QGIS", "Updated", "Unspecified", {})
        output = root / "edited.acalc-project.json"
        result = export_model(imported, output)
        before, after = copy.deepcopy(document), copy.deepcopy(result)
        # Only the selected model and its new immutable native byte record change.
        for value in (before, after):
            value["project"]["models"] = [m for m in value["project"]["models"] if m["id"] != model["id"]]
        old_ids = {f["id"] for f in before["files"]}
        after["files"] = [f for f in after["files"] if f["id"] in old_ids]
        self.assertEqual(encode(before), encode(after))
        self.assertEqual(next(m for m in models(result) if m["id"] == model["id"])["name"], "Edited native model")
        self.assertNotEqual(imported.metadata()["project_id"], result["project"]["info"]["project_id"])
        evidence = os.environ.get("ACALC_QGIS_INTEROP_OUTPUT")
        if evidence:
            Path(evidence).write_bytes(output.read_bytes())
