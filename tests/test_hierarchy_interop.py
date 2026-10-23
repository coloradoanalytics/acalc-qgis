# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Optional sibling-Core acceptance, using its actual generated fixture.

ACALC_CORE_HIERARCHY_FIXTURE can select a frozen Core fixture. The regular
portable-project tests remain self-contained when Core is not checked out.
"""
import copy
import os
from pathlib import Path
import unittest

from tests import test_portable_project as native_tests
from acalc.portable_project import encode, export_model, import_model, models, read


FIXTURE = Path(os.environ.get("ACALC_CORE_HIERARCHY_FIXTURE",
    str(Path(__file__).resolve().parents[2] / "core/hierarchy/testdata/heterogeneous.acalc-project.json")))


class HierarchyInteropTests(unittest.TestCase):
    @unittest.skipUnless(FIXTURE.is_file(), "Core's generated hierarchy fixture is not present")
    def test_native_model_edit_preserves_heterogeneous_core_project(self):
        fixture = native_tests.PortableProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        native = fixture.exported()
        document = read(FIXTURE)
        for record in native["files"]:
            record["owner"]["id"] = document["project"]["info"]["project_id"]
        document["files"].extend(native["files"])
        document["project"]["models"].extend(native["project"]["models"])
        project = fixture.root / "heterogeneous.acalc-project.json"
        project.write_bytes(encode(document))
        model = native["project"]["models"][0]
        imported = import_model(project, fixture.root / "heterogeneous-working", model["id"])
        imported.update_metadata("Edited native model", "QGIS", "Updated", "Unspecified", {})
        output = fixture.root / "edited.acalc-project.json"
        result = export_model(imported, output)
        before, after = copy.deepcopy(document), copy.deepcopy(result)
        # Only the selected model and its new immutable native byte record change.
        for value in (before, after):
            value["project"]["models"] = [m for m in value["project"]["models"] if m["id"] != model["id"]]
        old_ids = {f["id"] for f in before["files"]}
        after["files"] = [f for f in after["files"] if f["id"] in old_ids]
        self.assertEqual(encode(before), encode(after))
        self.assertEqual(models(result)[-1]["name"], "Edited native model")
        self.assertNotEqual(imported.metadata()["project_id"], result["project"]["info"]["project_id"])
        evidence = os.environ.get("ACALC_QGIS_INTEROP_OUTPUT")
        if evidence:
            Path(evidence).write_bytes(output.read_bytes())
