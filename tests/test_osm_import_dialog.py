# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""OSM import workflow with real QGIS widgets/storage and offline reply fixtures."""

import json
import unittest
from unittest.mock import PropertyMock, patch

from qgis.PyQt.QtCore import QByteArray, QEvent, QObject, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import QDialog, QDoubleSpinBox
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRectangle

from acalc.layer_import import import_layer
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.osm_download import OsmDownload
from acalc.osm_import_dialog import OsmImportDialog
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture


def building_payload():
    """Invented footprints test authoring only; they are not OSM or ISO references."""
    elements = []
    for index, longitude in enumerate((-105.0, -104.9997), 1):
        tags = {"building": "yes", "name": f"Building {index}"}
        if index == 1:
            tags["height"] = "12"
        ring = [(longitude, 40), (longitude + .0001, 40),
                (longitude + .0001, 40.0001), (longitude, 40.0001), (longitude, 40)]
        elements.append({"type": "way", "id": index, "nodes": [1, 2, 3, 4, 1],
                         "tags": tags,
                         "geometry": [{"lon": x, "lat": y} for x, y in ring]})
    return {"elements": elements}


class OsmImportDialogTests(ModelFixture):
    def iface(self):
        iface = FakeIface()
        iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:26913"))
        iface.canvas.setExtent(QgsRectangle(500000, 4427700, 500300, 4428000))
        self.addCleanup(APP.processEvents)
        self.addCleanup(iface.window.close)
        return iface

    def dialog(self, iface=None):
        iface = iface or self.iface()
        dialog = OsmImportDialog(None, self.store, iface.canvas)
        self.addCleanup(dialog.close)
        return dialog

    def plugin(self):
        iface = self.iface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        return plugin, iface

    def test_project_import_buttons_are_adjacent_and_disabled_without_model_or_when_busy(self):
        plugin, _ = self.plugin()
        ordinary = plugin.dock.import_button
        osm = plugin.dock.import_osm_button
        self.assertFalse(osm.isEnabled())
        parent_layout = ordinary.parentWidget().layout()
        button_row = next(parent_layout.itemAt(i).layout() for i in range(parent_layout.count())
                          if parent_layout.itemAt(i).layout() is not None
                          and parent_layout.itemAt(i).layout().indexOf(ordinary) >= 0)
        self.assertEqual(button_row.indexOf(osm), button_row.indexOf(ordinary) + 1)
        plugin.open_project(self.path)
        self.assertTrue(osm.isEnabled())
        plugin.calculation_busy_changed(True)
        self.assertFalse(osm.isEnabled())
        plugin.calculation_busy_changed(False)
        self.assertTrue(osm.isEnabled())

    def test_projected_current_canvas_extent_becomes_wgs84_and_user_bounds_are_used(self):
        iface = self.iface()
        dialog = self.dialog(iface)
        transform = QgsCoordinateTransform(iface.canvas.mapSettings().destinationCrs(),
                                           QgsCoordinateReferenceSystem("EPSG:4326"),
                                           QgsProject.instance())
        expected = transform.transformBoundingBox(iface.canvas.extent())
        self.assertEqual(dialog.extent.outputCrs().authid(), "EPSG:4326")
        for actual, wanted in zip(dialog.bbox(), (expected.yMinimum(), expected.xMinimum(),
                                                 expected.yMaximum(), expected.xMaximum())):
            self.assertAlmostEqual(actual, wanted, places=7)
        dialog.extent.setOutputExtentFromUser(QgsRectangle(-105.01, 40, -105, 40.01),
                                             QgsCoordinateReferenceSystem("EPSG:4326"))
        self.assertEqual(dialog.bbox(), (40, -105.01, 40.01, -105))
        with patch.object(dialog.download, "start") as start:
            dialog.start_download()
        start.assert_called_once_with((40, -105.01, 40.01, -105))
        self.assertFalse(dialog.inputs.isEnabled())
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertEqual(self.store.layers(), [])

    def test_oversized_area_is_rejected_before_network_or_model_write(self):
        dialog = self.dialog()
        dialog.extent.setOutputExtentFromUser(QgsRectangle(-105, 40, -104, 41),
                                             QgsCoordinateReferenceSystem("EPSG:4326"))
        with patch.object(dialog.download, "start") as start:
            dialog.start_download()
        start.assert_not_called()
        self.assertIn("100", dialog.error.text())
        self.assertTrue(dialog.inputs.isEnabled())
        self.assertEqual(self.store.layers(), [])

    def test_download_is_staged_and_height_options_rebuild_preview(self):
        dialog = self.dialog()
        dialog.downloaded(building_payload())
        self.assertEqual(self.store.layers(), [])
        self.assertEqual(dialog.source_layer().featureCount(), 2)
        self.assertEqual((dialog.report.tagged_height_count, dialog.report.default_height_count), (1, 1))
        self.assertEqual([f["height_m"] for f in dialog.source_layer().getFeatures()], [12, 6])
        self.assertTrue(dialog.import_button.isEnabled())
        dialog.default_height.setValue(8)
        self.assertEqual([f["height_m"] for f in dialog.source_layer().getFeatures()], [12, 8])
        dialog.use_osm_height.setChecked(False)
        self.assertEqual([f["height_m"] for f in dialog.source_layer().getFeatures()], [8, 8])
        self.assertEqual((dialog.report.tagged_height_count, dialog.report.default_height_count), (0, 2))
        self.assertEqual(self.store.layers(), [])

    def test_feet_display_and_ground_options_keep_metre_values_for_import(self):
        metadata = self.store.metadata()
        self.store.update_metadata(metadata["name"], metadata["author"], metadata["description"],
                                   metadata["vertical_datum"], {"elevation_unit": "ft"})
        dialog = self.dialog()
        self.assertEqual(dialog.default_height.suffix(), " ft")
        self.assertEqual(dialog.default_height.value(), 6)
        self.assertAlmostEqual(QDoubleSpinBox.value(dialog.default_height), 6 / .3048, places=5)
        self.assertEqual(dialog.options().ground_mode, "constant")
        self.assertEqual(dialog.options().building_height_mode, "average")
        self.assertTrue(dialog.ground_z.isEnabled())
        dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData("constant"))
        self.assertTrue(dialog.ground_z.isEnabled())
        # Simulate typed display values instead of using the metre-based setter.
        QDoubleSpinBox.setValue(dialog.default_height, 20)
        QDoubleSpinBox.setValue(dialog.ground_z, 5000)
        dialog.downloaded(building_payload())
        self.assertAlmostEqual(list(dialog.source_layer().getFeatures())[1]["height_m"], 6.096)
        self.assertAlmostEqual(dialog.options().ground_z, 1524)
        record, _, _ = import_layer(self.store, dialog.source_layer(), dialog.options())
        saved = list(load_layer(self.store, record).getFeatures())
        self.assertTrue(all("infer_z" not in f.fields().names() for f in saved))
        for feature, height in zip(saved, (12, 6.096)):
            self.assertEqual(feature["height_mode"], "average")
            self.assertAlmostEqual(feature["height_value"], height)
            for row in vertex_rows(feature.geometry(), LAYER_TYPES["building"]):
                self.assertAlmostEqual(row[3], 1524)
                self.assertAlmostEqual(row[4], height)

    def test_area_change_invalidates_staged_buildings_and_cancel_stops_download(self):
        dialog = self.dialog()
        dialog.downloaded(building_payload())
        dialog.extent.setOutputExtentFromUser(QgsRectangle(-104.9, 40, -104.89, 40.01),
                                             QgsCoordinateReferenceSystem("EPSG:4326"))
        self.assertIsNone(dialog.source_layer())
        self.assertIsNone(dialog.report)
        self.assertFalse(dialog.import_button.isEnabled())
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        with patch.object(dialog.download, "start"), patch.object(dialog.download, "cancel") as cancel:
            dialog.start_download()
            cancel.reset_mock()
            dialog.reject()
        cancel.assert_called_once()
        self.assertEqual(self.store.layers(), [])

    def test_empty_download_and_malformed_payload_do_not_enable_import(self):
        dialog = self.dialog()
        dialog.downloaded({"elements": []})
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertIn("No supported", dialog.error.text())
        dialog.downloaded({"remark": "runtime error: Query timed out"})
        self.assertIsNone(dialog.source_layer())
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertTrue(dialog.error.text())
        self.assertEqual(self.store.layers(), [])

    def test_controller_creates_active_building_layer_without_variant_membership(self):
        plugin, iface = self.plugin()
        plugin.open_project(self.path)
        variants_before = self.store.variants()
        destroyed = []
        def accept(dialog):
            dialog.destroyed.connect(lambda: destroyed.append(True))
            dialog.downloaded(building_payload())
            dialog.name.setText("OSM neighborhood")
            dialog.validate()
            return dialog.result()
        with patch.object(OsmImportDialog, "exec", accept):
            plugin.dock.import_osm_button.click()
        record, = self.store.layers()
        self.assertEqual((record["feature_type"], record["name"]), ("building", "OSM neighborhood"))
        layer = plugin.selected_layer()
        self.assertEqual(iface.activeLayer(), layer)
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual(validate_layer(layer, LAYER_TYPES["building"]), [])
        self.assertTrue(all("infer_z" not in f.fields().names() for f in layer.getFeatures()))
        self.assertTrue(all(f["height_mode"] == "average" for f in layer.getFeatures()))
        self.assertEqual(self.store.variants(), variants_before)
        self.assertIn("Copy saved", plugin.dock.status.text())
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(destroyed, [True])

    def test_controller_reopens_preview_after_write_failure_and_can_retry(self):
        plugin, _ = self.plugin()
        plugin.open_project(self.path)
        attempts = []
        def accept(dialog):
            if attempts:
                self.assertIn("Simulated write failure", dialog.error.text())
                self.assertEqual(self.store.layers(), [])
                self.assertEqual(dialog.source_layer().featureCount(), 2)
            else:
                dialog.downloaded(building_payload())
            attempts.append(dialog)
            dialog.validate()
            return dialog.result()
        writes = []
        def fail_first(*args, **kwargs):
            writes.append(1)
            if len(writes) == 1:
                raise ModelError("Simulated write failure")
            return import_layer(*args, **kwargs)
        with patch.object(OsmImportDialog, "exec", accept), \
                patch("acalc.layer_import.import_layer", side_effect=fail_first):
            plugin.import_osm_buildings()
        self.assertEqual(len(attempts), 2)
        self.assertIs(attempts[0], attempts[1])
        self.assertEqual(len(self.store.layers()), 1)

    def test_controller_cancel_leaves_model_unchanged(self):
        plugin, _ = self.plugin()
        plugin.open_project(self.path)
        destroyed = []
        def cancel(dialog):
            dialog.destroyed.connect(lambda: destroyed.append(True))
            dialog.downloaded(building_payload())
            dialog.reject()
            return dialog.result()
        with patch.object(OsmImportDialog, "exec", cancel):
            plugin.import_osm_buildings()
        self.assertEqual(self.store.layers(), [])
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(destroyed, [True])

    def test_controller_rejects_closed_or_switched_model_after_drawing_extent(self):
        plugin, _ = self.plugin()
        metadata = self.store.metadata()
        other = ModelStore.create(self.path.with_name("other.gpkg"), "Other model",
                                  metadata["crs_wkt"], metadata["crs_authid"])
        for change in ("close", "switch"):
            with self.subTest(change=change):
                plugin.open_project(self.path)
                destroyed = []
                def accept(dialog):
                    dialog.destroyed.connect(lambda: destroyed.append(True))
                    dialog.downloaded(building_payload())
                    if change == "close":
                        self.assertTrue(plugin.close_project())
                    else:
                        plugin.open_project(other.path)
                    dialog.validate()
                    return dialog.result()
                with patch.object(OsmImportDialog, "exec", accept), \
                        self.assertRaisesRegex(ModelError, "open Acalc model changed"):
                    plugin.import_osm_buildings()
                self.assertEqual(self.store.layers(), [])
                self.assertEqual(other.layers(), [])
                APP.sendPostedEvents(None, QEvent.DeferredDelete)
                self.assertEqual(destroyed, [True])

    def test_controller_rejects_calculation_started_while_drawing_extent(self):
        plugin, _ = self.plugin()
        plugin.open_project(self.path)
        with patch.object(type(plugin.engine_runner), "active", new_callable=PropertyMock,
                          return_value=False) as active:
            def accept(dialog):
                self.assertFalse(plugin.engine_runner.active)
                dialog.downloaded(building_payload())
                active.return_value = True
                dialog.validate()
                return dialog.result()
            with patch.object(OsmImportDialog, "exec", accept), \
                    self.assertRaisesRegex(ModelError, "Wait for the calculation"):
                plugin.import_osm_buildings()
        self.assertEqual(self.store.layers(), [])


class FakeReply(QObject):
    """Only the transport is substituted; Qt signals exercise reply lifetimes."""
    readyRead = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, status=200, error=QNetworkReply.NoError):
        super().__init__()
        self.status, self.code = status, error
        self.pending = b""
        self.aborted = False
        self.deleted = False

    def setReadBufferSize(self, size): self.buffer_size = size
    def attribute(self, key):
        assert key == QNetworkRequest.HttpStatusCodeAttribute
        return self.status
    def error(self): return self.code
    def errorString(self): return "Connection failed"
    def readAll(self):
        data, self.pending = self.pending, b""
        return QByteArray(data)
    def abort(self):
        self.aborted = True
        self.finished.emit()  # Real Qt replies may finish during abort.
    def deleteLater(self): self.deleted = True
    def deliver(self, data, finish=True):
        self.pending += data
        self.readyRead.emit()
        if finish:
            self.finished.emit()


class OsmDownloadTests(unittest.TestCase):
    bbox = (40, -105, 40.01, -104.99)

    def downloader(self, replies):
        network_patch = patch("acalc.osm_download.QgsNetworkAccessManager")
        network = network_patch.start()
        self.addCleanup(network_patch.stop)
        network.instance.return_value.post.side_effect = replies
        downloader = OsmDownload()
        self.addCleanup(downloader.cancel)
        succeeded, failed = [], []
        downloader.succeeded.connect(succeeded.append)
        downloader.failed.connect(failed.append)
        return downloader, succeeded, failed, network.instance.return_value.post

    def test_post_request_is_encoded_and_chunked_response_succeeds_once(self):
        reply = FakeReply()
        downloader, succeeded, failed, post = self.downloader([reply])
        downloader.start(self.bbox)
        request, body = post.call_args.args
        self.assertEqual(request.url().scheme(), "https")
        self.assertEqual(request.header(QNetworkRequest.ContentTypeHeader), "application/x-www-form-urlencoded")
        self.assertTrue(bytes(body).startswith(b"data="))
        self.assertIn(b"40%2C-105%2C40.01%2C-104.99", bytes(body))
        self.assertEqual(downloader._timer.interval(), 90000)
        reply.deliver(b'{"elements":', finish=False)
        self.assertEqual(succeeded, [])
        reply.deliver(b'[]}')
        self.assertEqual(succeeded, [{"elements": []}])
        self.assertEqual(failed, [])
        self.assertIsNone(downloader._reply)
        self.assertFalse(downloader._timer.isActive())
        self.assertTrue(reply.deleted)

    def test_http_errors_malformed_json_and_connection_failure_are_reported(self):
        cases = [(429, QNetworkReply.NoError, b'{}', "HTTP 429"),
                 (504, QNetworkReply.NoError, b'{}', "HTTP 504"),
                 (200, QNetworkReply.NoError, b'<html>busy</html>', "invalid response"),
                 (None, QNetworkReply.ConnectionRefusedError, b'', "Connection failed")]
        for status, error, data, expected in cases:
            with self.subTest(expected=expected):
                reply = FakeReply(status, error)
                downloader, succeeded, failed, _ = self.downloader([reply])
                downloader.start(self.bbox)
                reply.deliver(data)
                self.assertEqual(succeeded, [])
                self.assertEqual(len(failed), 1)
                self.assertIn(expected, failed[0])
                self.assertTrue(reply.aborted)

    def test_timeout_and_cancel_disconnect_late_reply_signals(self):
        for timeout in (True, False):
            with self.subTest(timeout=timeout):
                reply = FakeReply()
                downloader, succeeded, failed, _ = self.downloader([reply])
                downloader.start(self.bbox)
                if timeout:
                    downloader._timer.timeout.emit()
                else:
                    downloader.cancel()
                reply.deliver(b'{"elements":[]}')
                self.assertEqual(succeeded, [])
                self.assertEqual(len(failed), int(timeout))
                if timeout:
                    self.assertIn("timed out", failed[0])
                self.assertTrue(reply.aborted)
                self.assertFalse(downloader._timer.isActive())

    def test_response_limit_aborts_and_next_request_can_succeed(self):
        oversized, retry = FakeReply(), FakeReply()
        downloader, succeeded, failed, post = self.downloader([oversized, retry])
        downloader.start(self.bbox)
        with patch("acalc.osm_download.MAX_RESPONSE_BYTES", 10):
            oversized.deliver(b'{"elements":', finish=False)
        self.assertEqual(succeeded, [])
        self.assertEqual(len(failed), 1)
        self.assertIn("too large", failed[0])
        self.assertTrue(oversized.aborted)
        downloader.start(self.bbox)
        oversized.deliver(b'[]}')
        retry.deliver(json.dumps({"elements": []}).encode())
        self.assertEqual(succeeded, [{"elements": []}])
        self.assertEqual(len(failed), 1)
        self.assertEqual(post.call_count, 2)

    def test_replacing_active_request_discards_its_partial_response_and_late_signals(self):
        first, second = FakeReply(), FakeReply()
        downloader, succeeded, failed, _ = self.downloader([first, second])
        downloader.start(self.bbox)
        first.deliver(b'{"elements":', finish=False)
        downloader.start((40.01, -105, 40.02, -104.99))
        first.deliver(b'[{"stale": true}]}')
        second.deliver(b'{"elements":[]}')
        self.assertEqual(succeeded, [{"elements": []}])
        self.assertEqual(failed, [])
        self.assertTrue(first.aborted)
