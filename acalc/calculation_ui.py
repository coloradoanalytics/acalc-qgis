# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Shared calculation parameters and stored receiver results by variant."""

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QPlainTextEdit,
    QFormLayout, QHeaderView, QLabel, QPushButton, QTableWidgetItem,
    QVBoxLayout, QWidget, QScrollArea, QHBoxLayout, QSplitter,
)

from .calculation_settings import ENGINE_ID, default_settings, validate_settings
from .schema import ModelError
from .spectrum_table import SpectrumTable
from .api_settings_ui import ApiISOSettingsEditor as ISOSettingsEditor
from .run_info import calculation_scene_counts, path_counts_help, path_counts_text


def explanation(text):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


class CalculationPage(QWidget):
    def __init__(self, controller, variant_controls, variant_combo):
        super().__init__()
        self.controller = controller
        self._refreshing_engines = False
        self._busy = False
        self._engine_project = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        self.variant = variant_combo
        # Keep remembered selection for the calculation dialog; selection controls
        # belong in that dialog, not on this overview page.
        self.engine = QComboBox(self)
        self.engine.hide()
        self.engine.setMinimumContentsLength(20)
        self.engine.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.engine.currentIndexChanged.connect(self._engine_changed)
        actions = QHBoxLayout()
        self.check_button = QPushButton("Validate model")
        self.check_button.clicked.connect(lambda: controller.run(controller.show_validation))
        self.calculate_button = QPushButton("Calculate")
        self.calculate_button.clicked.connect(lambda: controller.run(controller.open_calculation_dialog))
        self.back_calculate_button = QPushButton("Back-calculate source power…")
        self.back_calculate_button.clicked.connect(lambda: controller.run(controller.open_back_calculation_dialog))
        self.back_calculate_button.setToolTip("Fit adjustable sound power spectra to octave Measurements using a compatible engine.")
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(lambda: controller.run(self.open_export))
        for widget in (self.check_button, self.calculate_button, self.back_calculate_button, self.export_button):
            actions.addWidget(widget)
        outer.addLayout(actions)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.splitter.setStyleSheet(
            "QSplitter::handle:vertical { background: palette(midlight); "
            "border-top: 1px solid palette(mid); border-bottom: 1px solid palette(mid); }"
            "QSplitter::handle:vertical:hover { background: palette(highlight); }")
        self.splitter.setAccessibleName("Resize variant tree and calculation messages")
        self.splitter.addWidget(variant_controls)
        messages_panel = QWidget()
        messages_layout = QVBoxLayout(messages_panel)
        messages_layout.setContentsMargins(0, 0, 0, 0)
        messages_header = QHBoxLayout()
        messages_header.addWidget(QLabel("Calculation messages"), 1)
        self.copy_messages_button = QPushButton("Copy messages")
        self.copy_messages_button.setToolTip("Copy all calculation messages to the clipboard.")
        self.copy_messages_button.setEnabled(False)
        self.copy_messages_button.clicked.connect(self.copy_messages)
        messages_header.addWidget(self.copy_messages_button)
        messages_layout.addLayout(messages_header)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.messages.setMinimumHeight(80)
        self.messages.setAccessibleName("Calculation messages")
        messages_layout.addWidget(self.messages, 1)
        self.splitter.addWidget(messages_panel)
        self.splitter.handle(1).setCursor(Qt.SplitVCursor)
        self.splitter.handle(1).setToolTip("Drag to resize the variant tree and calculation messages.")
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 160])
        state = QSettings().value("acalc/calculation_page/splitter")
        if state is not None:
            self.splitter.restoreState(state)
        self.splitter.splitterMoved.connect(
            lambda *args: QSettings().setValue("acalc/calculation_page/splitter", self.splitter.saveState()))
        outer.addWidget(self.splitter, 1)

    def append_message(self, text):
        self.messages.appendPlainText(str(text))
        self.copy_messages_button.setEnabled(bool(self.messages.toPlainText()))

    def copy_messages(self):
        QApplication.clipboard().setText(self.messages.toPlainText())

    def settings(self):
        return self.controller.store.calculation()["settings"] if self.controller.store else default_settings()

    def open_export(self):
        from .export_dialog import ExportDialog
        self.controller.require_store()
        ExportDialog(self.controller, self).exec_()

    def dirty(self):
        return False

    def refresh(self, force=False):
        from .calculation_engines import available_engines
        store = self.controller.store
        project = store.metadata()["project_id"] if store else None
        selected = (self.engine.currentData() if project == self._engine_project else None)
        selected = selected or QSettings().value("acalc/api_engine/" + (project or "default"), "")
        self._refreshing_engines = True
        try:
            self.engine.clear()
            for engine in available_engines():
                self.engine.addItem(engine.name, engine.id)
                self.engine.setItemData(self.engine.count() - 1, engine.name, Qt.ToolTipRole)
            self.engine.setCurrentIndex(self.engine.findData(selected) if selected else (0 if self.engine.count() else -1))
            self._engine_project = project
        finally:
            self._refreshing_engines = False
        self._engine_changed()
        self.set_calculating(self._busy)

    def select_engine(self, engine_id):
        self.engine.setCurrentIndex(self.engine.findData(engine_id))

    def _engine_changed(self, *args):
        if self._refreshing_engines:
            return
        selected = self.engine.currentData()
        if selected:
            QSettings().setValue("acalc/api_engine/" + (self._engine_project or "default"), selected)
        self.engine.setToolTip(self.engine.currentText())
        self.set_calculating(self._busy)

    def set_calculating(self, busy):
        self._busy = busy
        active = self.controller.store is not None and not busy
        for widget in (self.variant, self.engine, self.check_button):
            widget.setEnabled(active)
        # Opening setup must remain possible when a remembered API disappeared:
        # the user can choose another available API inside the dialog.
        self.calculate_button.setEnabled(active and self.engine.count() > 0)
        from .calculation_engines import available_engines
        self.back_calculate_button.setEnabled(active and any(
            "source_receiver_transfers" in engine.capabilities.get("features", [])
            for engine in available_engines()))
        self.export_button.setEnabled(active)


class ResultsPage(QWidget):
    def __init__(self, controller, receiver=None, area=False, measurement_values=None):
        super().__init__()
        self.controller = controller
        self.receiver = receiver
        self.measurement_values = measurement_values
        self._measurement_result = None
        self._has_run = False
        self._updating = False
        self.layer_error = ""
        self._run_details = ""
        self._details_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.variant = QComboBox()
        self.variant.currentIndexChanged.connect(self.show_results)
        layout.addWidget(self.variant)
        self.summary = explanation("No stored calculation results.")
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.details_button = QPushButton("Run details…")
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(self.show_run_details)
        layout.addWidget(self.details_button)
        self.area_view = None
        if area:
            from .area_results_ui import AreaResultView
            self.area_view = AreaResultView(self)
            layout.addWidget(self.area_view, 1)
        self.table = SpectrumTable(0, 10)
        self.table.setHorizontalHeaderLabels(["Receiver / type", "dBA"] +
                                             [f"{band} Hz" for band in (63, 125, 250, 500, 1000, 2000, 4000, 8000)])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 0 if area else 1)
        if measurement_values is not None:
            layout.addWidget(explanation(
                "Difference = calculated − measured (dB). Positive values mean the calculation is higher. "
                "Blank values are unavailable; −∞ means zero calculated energy. "
                "Calculated values belong to the selected variant; entered measurements are kept separately."))
        if area:
            self.table.setFixedHeight(82)
            self.table.setHorizontalHeaderLabels(["Closest point", "dBA"] + [f"{band} Hz" for band in (63,125,250,500,1000,2000,4000,8000)])
            self.table.horizontalHeader().setMinimumSectionSize(65)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.area_view.sample_changed.connect(self.show_area_sample)
        if receiver is None:
            self.table.cellDoubleClicked.connect(self.open_receiver)
        refresh = QPushButton("Refresh saved results")
        refresh.clicked.connect(self.publish_saved_results)
        layout.addWidget(refresh)
        if receiver is None:
            self.clear_button = QPushButton("Clear results…")
            self.clear_button.clicked.connect(lambda: self.controller.run(self.controller.clear_results))
            self.clear_button.setEnabled(False)
            layout.addWidget(self.clear_button)
        self._level_help = ("Hover values are saved samples: dBA and unweighted octaves. −∞ means zero energy. Contour lines interpolate between samples." if area else
                            "Sound pressure levels in dB re 20 µPa. Calculated octaves are unweighted; "
                            "dBA combines the eight bands with A weighting. −∞ means zero calculated energy. "
                            "Blank measured values are unknown. Calculating a variant replaces only calculated results.")

    def show_run_details(self):
        if not self._run_details:
            return
        if self._details_dialog is None:
            self._details_dialog = QDialog(self)
            self._details_dialog.resize(760, 560)
            layout = QVBoxLayout(self._details_dialog)
            self._details_text = QPlainTextEdit()
            self._details_text.setReadOnly(True)
            layout.addWidget(self._details_text)
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(self._details_dialog.reject)
            layout.addWidget(buttons)
        self._details_dialog.setWindowTitle("Run details — " + self.variant.currentText())
        self._details_text.setPlainText(self._run_details)
        self._details_dialog.show()
        self._details_dialog.raise_()
        self._details_dialog.activateWindow()

    def publish_saved_results(self, variant_id=None):
        self.layer_error = ""
        if self.receiver is None and self.controller.store and not self.controller.engine_runner.active:
            try:
                self.controller.refresh_result_layers()
            except (ModelError, OSError, ValueError, RuntimeError) as error:
                self.layer_error = "Saved values are available, but result map layers could not be refreshed: " + str(error)
                self.controller.message(self.layer_error, error=True)
        self.refresh(variant_id)

    def refresh(self, variant_id=None):
        # Navigation refreshes the results panel, never map layers or their data.
        selected = variant_id if isinstance(variant_id, str) else self.variant.currentData()
        self._updating = True
        self.variant.clear()
        store = self.controller.store
        if store:
            existing = set()
            for variant in store.variants():
                self.variant.addItem(variant["name"], variant["variant_id"])
                existing.add(variant["variant_id"])
            for run in store.calculation_run_summaries():
                if run["variant_id"] not in existing:
                    self.variant.addItem(run["variant_name"] + " (deleted variant)",
                                               run["variant_id"])
                    existing.add(run["variant_id"])
            self.variant.setCurrentIndex(max(0, self.variant.findData(selected)))
        self._updating = False
        self.show_results()

    def show_results(self, *args):
        if self._updating:
            return
        store = self.controller.store
        variant_id = self.variant.currentData()
        run = store.latest_calculation_run(variant_id) if store and variant_id else None
        self._run_details = ""
        self.details_button.setEnabled(False)
        if self._details_dialog is not None:
            self._details_dialog.close()
        self._has_run = run is not None
        if self.receiver is None:
            self.set_calculating(self.controller.engine_runner.active)
        self.table.setRowCount(0)
        self._measurement_result = None
        if self.measurement_values is not None:
            self.update_measurement_comparison()
        if store is None or not variant_id:
            self.summary.setText("No model or variant selected.")
            if self.area_view is not None:
                self.area_view.set_area(store, None)
            return
        try:
            selected = ({"layer_id": self.receiver[0], "object_id": self.receiver[1]} if self.receiver else {})
            results = store.receiver_results(run_id=run["run_id"], **selected) if run else []
            if run:
                from .area_results import grouped_results
                results = grouped_results(store, run, results)
                if self.receiver:
                    results = [r for r in results if (r["layer_id"], r["object_id"]) == self.receiver]
            measurements = store.measurements(variant_id, **selected)
        except ModelError as error:
            self.summary.setText("Cannot display receiver levels: " + str(error))
            return
        if run:
            status = "OUT OF DATE — model or calculation engine changed; calculate again to update results." if run["stale"] else "Results match the saved model revision."
            settings = run["settings"]
            conditions = ("Long-term average" if settings["meteorology"] == "long_term" else "Favourable / downwind")
            conditions += (f" · {settings['temperature_c']:g} °C · {settings['relative_humidity_percent']:g} % RH"
                           f" · G {settings['ground_factor']:g}")
            scene_counts = calculation_scene_counts(run)
            sources, buildings = scene_counts["source_count"], scene_counts["building_count"]
            barriers = run.get("metadata", {}).get("barrier_count", 0)
            self.summary.setText(f"{status}\n{len(results)} receiver results · {len(measurements)} Measurements · "
                                 f"{sources} sources · {buildings} buildings · "
                                 f"{barriers} barriers\n{run['standard']}\n{conditions}\nCalculated: {run['finished_at']}" +
                                 ("\n" + "\n".join(run["warnings"]) if run.get("warnings") else ""))
            if self.area_view is not None:
                self.summary.setText(f"{status}\n{run['standard']}\n{conditions} · Calculated: {run['finished_at']}" +
                                     ("\n" + "\n".join(run["warnings"]) if run.get("warnings") else ""))
            backend = run.get("metadata", {}).get("backend", {})
            if backend.get("api_name"):
                implementation = backend.get("implementation", {})
                self.summary.setText(
                    f"{backend['api_name']} · {implementation.get('name', run['engine'])} "
                    f"{implementation.get('version', run['engine_version'])}\n" + self.summary.text())
            self._run_details = self.summary.text() + "\n\n" + self._level_help
            if run["metadata"].get("source_building_count"):
                self._run_details += (f"\n\n{sources} component sources were submitted "
                                      "to the engine. Source Building components retain their individual spectra and paths.")
            paths = run["metadata"].get("api_job", {}).get("progress", {}).get("path_counts")
            if paths is not None:
                pruning = settings.get("pruning_strategy") is not None
                self._run_details += ("\n\n" + path_counts_text(paths, pruning) + "\n" +
                                      path_counts_help(pruning))
            self.details_button.setEnabled(True)
            counts = (f"{len(results)} receiver results · {len(measurements)} Measurements · "
                      f"{sources} sources · {buildings} buildings · {barriers} barriers")
            self.summary.setText(f"{status}\n{counts}" +
                                 (f"\n{backend['api_name']}" if backend.get("api_name") else ""))
        else:
            self.summary.setText(f"No stored calculation results for this variant. {len(measurements)} Measurements are available.")
        if self.layer_error:
            self._run_details = self.layer_error + "\n\n" + self._run_details
            self.details_button.setEnabled(True)
            self.summary.setText("Map layers could not be refreshed. See Run details.\n" + self.summary.text())
        if not results and not measurements and self.receiver:
            self.summary.setText("No levels for this receiver in the selected variant.\n" + self.summary.text())
        if self.area_view is not None:
            self.show_area_sample(None)
            self.area_view.set_area(store, next((r for r in results if r.get("is_area")), None))
            return
        if self.measurement_values is not None:
            self._measurement_result = next(iter(results), None)
            self.update_measurement_comparison()
            return
        measurement_ids = {(value["layer_id"], value["object_id"]) for value in measurements}
        entries = [(result, False) for result in results] + [(value, True) for value in measurements]
        self.table.setRowCount(len(entries))
        for row, (result, measured) in enumerate(entries):
            is_area = result.get("is_area", False)
            is_measurement = (result["layer_id"], result["object_id"]) in measurement_ids
            suffix = " · Measurement · Measured" if measured else " · Measurement · Calculated" if is_measurement else " · Area" if is_area else ""
            receiver = QTableWidgetItem(result["receiver_name"] + suffix)
            receiver.setData(Qt.UserRole, (result["layer_id"], result["object_id"]))
            receiver.setToolTip("Double-click to open " + ("Measurement properties" if measured else "receiver properties and results"))
            self.table.setItem(row, 0, receiver)
            numbers = [result["broadband_a_db"]] + result["octave_levels"]
            for column, value in enumerate(numbers, start=1):
                item = QTableWidgetItem("" if is_area else (("" if measured else "−∞") if value is None else f"{value:.1f}"))
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)

    def update_measurement_comparison(self, *args):
        if self.measurement_values is None:
            return
        from .measurement import MEASUREMENT_FIELDS
        try:
            values = self.measurement_values()
            measured = [values["broadband_a_db"]] + [values[field] for field in MEASUREMENT_FIELDS]
        except ModelError:
            measured = [None] * 9
        result = self._measurement_result
        calculated = [result["broadband_a_db"]] + result["octave_levels"] if result else [None] * 9
        self.table.setHorizontalHeaderLabels(["Value set", "dBA"] + [f"{band} Hz" for band in (63,125,250,500,1000,2000,4000,8000)])
        self.table.setRowCount(3)
        for row, label in enumerate(("Measured", "Calculated", "Calculated − measured")):
            self.table.setItem(row, 0, QTableWidgetItem(label))
        for column, (observed, predicted) in enumerate(zip(measured, calculated), 1):
            difference = "" if result is None or observed is None else "−∞" if predicted is None else f"{predicted - observed:+.1f}"
            texts = ("" if observed is None else f"{observed:.1f}",
                     "" if result is None else "−∞" if predicted is None else f"{predicted:.1f}", difference)
            for row, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip("Difference = calculated − measured (dB). Positive means the calculation is higher. Blank means unavailable; −∞ means zero calculated energy.")
                self.table.setItem(row, column, item)

    def set_calculating(self, busy):
        if self.receiver is None:
            self.clear_button.setEnabled(self._has_run and not busy)

    def show_area_sample(self, result):
        self.table.setRowCount(1)
        point = (f"[{result['metadata']['grid_column']+1}, {result['metadata']['grid_row']+1}]" if result else "—")
        self.table.setItem(0, 0, QTableWidgetItem(point))
        numbers = [result["broadband_a_db"], *result["octave_levels"]] if result else [None]*9
        for column, value in enumerate(numbers, 1):
            item = QTableWidgetItem("" if result is None else "−∞" if value is None else f"{value:.1f}")
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(0, column, item)

    def open_receiver(self, row, column):
        item = self.table.item(row, 0)
        if item is not None:
            layer_id, object_id = item.data(Qt.UserRole)
            self.controller.run(lambda: self.controller.open_receiver_results(
                layer_id, object_id, self.variant.currentData()))


class ReceiverResultsPage(ResultsPage):
    def __init__(self, context, area=False, measurement_values=None):
        from types import SimpleNamespace
        store, layer_id, object_id, selection = context or (None, None, None, None)
        super().__init__(SimpleNamespace(store=store), (layer_id, object_id), area=area, measurement_values=measurement_values)
        if store and selection is None:
            for run in store.calculation_runs():
                if store.receiver_results(run_id=run["run_id"], layer_id=layer_id, object_id=object_id) or any(
                        r["layer_id"] == layer_id and r["object_id"] == object_id for r in run["metadata"].get("area_receivers") or []):
                    selection = run["variant_id"]
                    break
        self.refresh(selection)
        if store is None:
            self.summary.setText("Save this receiver and calculate a variant to see its results here.")
