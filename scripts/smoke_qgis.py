# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Run inside QGIS with --code, using an isolated profile (see README).

Writes a screenshot and result to /tmp/acalc-qgis-smoke, then exits that QGIS
process. Never run this in a QGIS session containing unsaved user work.
"""
import json
import os
import shutil
from pathlib import Path
import sys
import traceback

from qgis.PyQt.QtCore import QCoreApplication, QEvent, QPoint, QTimer, Qt
from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QMenu
from qgis.PyQt.QtTest import QTest
from qgis.core import QgsProject, QgsRectangle, Qgis
from qgis.gui import QgsMapMouseEvent
from qgis.utils import iface

# QGIS executes --code in its console namespace without defining __file__.
ROOT = Path(os.environ.get("ACALC_QGIS_REPO", Path.cwd())).resolve()
OUTPUT = Path("/tmp/acalc-qgis-smoke")
OUTPUT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))

from acalc import classFactory
from acalc.dialogs import FeatureDialog

plugin = None


def finish():
    try:
        assert len(plugin.group.findLayers()) == 8
        assert plugin.dock.current_kind == "barrier"
        assert plugin.dock.features.rowCount() == 1
        assert plugin.dock.current_layer.selectedFeatureCount() == 1
        assert not hasattr(plugin.dock, "vertex_preview")
        assert not plugin.validation_errors()
        assert iface.mainWindow().grab().save(str(OUTPUT / "qgis.png"))
        assert plugin.dock.grab().save(str(OUTPUT / "dock.png"))
        canvas = iface.mapCanvas()
        layer = plugin.dock.current_layer
        vertex = next(layer.getFeatures()).geometry().vertexAt(1)
        pixel = canvas.getCoordinateTransform().transform(vertex.x(), vertex.y())
        event = QgsMapMouseEvent(canvas, QEvent.MouseButtonPress,
                                QPoint(round(pixel.x()), round(pixel.y())), Qt.RightButton)
        menu = QMenu()
        canvas.contextMenuAboutToShow.emit(menu, event)
        properties = next(action for action in menu.actions() if action.text() == "Acalc properties")
        assert properties.menu() is None
        captured = []

        def capture_properties():
            dialog = QApplication.activeModalWidget()
            try:
                assert isinstance(dialog, FeatureDialog)
                assert dialog.table.rowCount() == 3
                assert [float(dialog.table.item(row, 3).text()) for row in range(3)] == [3, 3.5, 4]
                assert dialog.grab().save(str(OUTPUT / "properties.png"))
                captured.append("passed")
            except Exception:
                captured.append(traceback.format_exc())
            finally:
                if isinstance(dialog, FeatureDialog):
                    dialog.reject()

        QTimer.singleShot(150, capture_properties)
        properties.trigger()
        assert captured == ["passed"], captured
        assert not layer.isEditable()
        plugin.dock.show_view("point_source")
        draw_layer = plugin.selected_layer()
        original_count = draw_layer.featureCount()
        drawn_dialogs = []

        def handle_drawn_properties(accept):
            dialog = QApplication.activeModalWidget()
            try:
                # No generic QGIS attribute form should have opened first.
                assert isinstance(dialog, FeatureDialog)
                assert draw_layer.featureCount() == original_count
                assert dialog.table.rowCount() == 1
                assert dialog.tabs.tabText(1) == "Spectrum"
                if accept:
                    dialog.name.setText("Smoke drawn point")
                    dialog.table.item(0, 2).setText("1500")
                    dialog.table.item(0, 3).setText("2.5")
                    assert dialog.grab().save(str(OUTPUT / "draw-properties.png"))
                    dialog.validate()
                else:
                    dialog.reject()
                drawn_dialogs.append("accepted" if accept else "canceled")
            except Exception:
                drawn_dialogs.append(traceback.format_exc())
                if isinstance(dialog, FeatureDialog):
                    dialog.reject()

        for accept in (True, False):
            plugin.draw_feature()
            point = canvas.getCoordinateTransform().transform(500070, 4400030)
            QTimer.singleShot(150, lambda choice=accept: handle_drawn_properties(choice))
            QTest.mouseClick(canvas.viewport(), Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            QApplication.processEvents()
            if accept:
                assert drawn_dialogs == ["accepted"], drawn_dialogs
                assert draw_layer.featureCount() == original_count + 1
                draw_layer.undoStack().undo()
                assert draw_layer.featureCount() == original_count
                draw_layer.undoStack().redo()
                QApplication.processEvents()
                assert draw_layer.featureCount() == original_count + 1
                assert drawn_dialogs == ["accepted"]
            else:
                assert drawn_dialogs == ["accepted", "canceled"], drawn_dialogs
                assert draw_layer.featureCount() == original_count
                assert not draw_layer.isModified()
            assert draw_layer.rollBack()  # Keep the example GeoPackage unchanged.
            assert plugin.digitizer.tool is None
        result = {"status": "passed", "qgis": Qgis.QGIS_VERSION,
                  "layers": len(plugin.layers), "variants": len(plugin.store.variants()),
                  "checks": ["real iface initialization", "sample open", "styled group", "selection synchronization", "map context action", "modal properties", "per-vertex heights", "cancel without edits", "draw directly into Acalc properties", "apply drawn feature", "cancel drawn feature", "draw undo/redo", "model validation", "unload"]}
        plugin.close_project()
        plugin.unload()
        (OUTPUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        QCoreApplication.exit(0)
    except Exception:
        (OUTPUT / "result.json").write_text(json.dumps({"status": "failed", "traceback": traceback.format_exc()}, indent=2))
        QCoreApplication.exit(1)


def start():
    global plugin
    try:
        plugin = classFactory(iface)
        plugin.initGui()
        # Work on a copy: opening migrates the schema-1 fixture in place.
        model = OUTPUT / "example-site.gpkg"
        shutil.copy2(ROOT / "tests/fixtures/example-site.gpkg", model)
        plugin.open_project(model)
        iface.mainWindow().resize(1440, 980)
        plugin.show_dock(True)
        QgsProject.instance().setCrs(next(iter(plugin.layers.values())).crs())
        plugin.dock.show_view("barrier")
        layer = plugin.selected_layer()
        iface.setActiveLayer(layer)
        layer.selectByIds([next(layer.getFeatures()).id()])
        tree_dock = iface.layerTreeView().parentWidget()
        while tree_dock is not None and not isinstance(tree_dock, QDockWidget):
            tree_dock = tree_dock.parentWidget()
        if tree_dock is not None:
            iface.mainWindow().resizeDocks([tree_dock, plugin.dock], [260, 465], Qt.Horizontal)
        else:
            iface.mainWindow().resizeDocks([plugin.dock], [465], Qt.Horizontal)
        iface.mapCanvas().setExtent(QgsRectangle(499945, 4399950, 500135, 4400120))
        iface.mapCanvas().refresh()
        QTimer.singleShot(1500, finish)
    except Exception:
        (OUTPUT / "result.json").write_text(json.dumps({"status": "failed", "traceback": traceback.format_exc()}, indent=2))
        QCoreApplication.exit(1)


QTimer.singleShot(500, start)
