# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Calculation setup, persistent variant selection, and live batch status."""

import json
from html import escape
from time import monotonic

from qgis.PyQt.QtCore import QSettings, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QProgressBar, QPushButton, QScrollArea,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .calculation_engines import available_engines
from .calculation_ui import explanation
from .schema import ModelError
from .run_info import (
    WORK_PROGRESS_HELP, calculation_scene_counts, path_counts_help, path_counts_text, work_progress_text,
)


def _elapsed_text(seconds):
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _plain_tooltip(text):
    # Qt detects rich text in tooltips; model names must remain literal text.
    return "<qt>" + escape(text).replace("\n", "<br>") + "</qt>"


class CalculationDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller.iface.mainWindow())
        self.controller = controller
        self.store = controller.require_store()
        self._running = False
        self._connections = []
        self._selected = []
        self._completed = 0
        self._warned = []
        self._validate = False
        self._pruning = True
        self._batch_started = None
        self._batch_elapsed = 0
        self._variant_started_at = {}
        self._variant_elapsed = {}
        self._active_variant = None
        self._stopping = False
        self._clock = QTimer(self)
        self._clock.setInterval(250)
        self._clock.timeout.connect(self.update_run_time)
        self.setWindowTitle("Calculate variants")
        self.setWindowModality(Qt.WindowModal)
        self.resize(980, 780)
        self.preference_key = "acalc/calculation/" + self.store.metadata()["project_id"]
        try:
            preferences = json.loads(QSettings().value(self.preference_key, "{}"))
            if not isinstance(preferences, dict):
                preferences = {}
        except (ValueError, TypeError):
            preferences = {}

        layout = QVBoxLayout(self)
        self.setup = QWidget()
        setup = QHBoxLayout(self.setup)
        setup.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.setup, 1)
        variants_box = QGroupBox("Variants to calculate")
        variants_layout = QVBoxLayout(variants_box)
        self.variants = QTableWidget(0, 2)
        self.variants.setHorizontalHeaderLabels(["Variant", "Status"])
        self.variants.verticalHeader().hide()
        self.variants.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.variants.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.variants.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.variants.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.variants.setMinimumWidth(250)
        self.variants.setAccessibleName("Variants to calculate")
        remembered = preferences.get("variants")
        current = controller.selected_variant()
        selected = set(remembered) if isinstance(remembered, list) and all(isinstance(v, str) for v in remembered) else (
            {current["variant_id"]} if current else set())
        for row, variant in enumerate(self.store.variants()):
            self.variants.insertRow(row)
            item = QTableWidgetItem(variant["name"])
            item.setToolTip(variant["name"])
            item.setData(Qt.UserRole, variant["variant_id"])
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if variant["variant_id"] in selected else Qt.Unchecked)
            self.variants.setItem(row, 0, item)
            self.variants.setItem(row, 1, QTableWidgetItem(""))
        variants_layout.addWidget(self.variants, 1)
        selection = QHBoxLayout()
        self.all_button = QPushButton("Select all")
        self.none_button = QPushButton("Clear selection")
        self.all_button.clicked.connect(lambda: self.select_all(True))
        self.none_button.clicked.connect(lambda: self.select_all(False))
        selection.addWidget(self.all_button)
        selection.addWidget(self.none_button)
        variants_layout.addLayout(selection)
        variants_layout.addWidget(explanation("Your selection is remembered for this model. Calculating a variant replaces its saved results."))
        setup.addWidget(variants_box, 2)

        options = QGroupBox("Calculation parameters")
        self.options = options
        options_layout = QVBoxLayout(options)
        engine_form = QFormLayout()
        self.engine = QComboBox()
        self.engines = {engine.id: engine for engine in available_engines()}
        self.panels = {}
        self.configuration = QStackedWidget()
        stored = self.store.calculation()
        for engine in self.engines.values():
            self.engine.addItem(engine.name, engine.id)
            from .units import input_unit
            panel = engine.create_editor()
            if hasattr(panel, "set_input_unit"):
                panel.set_input_unit(input_unit(self.store))
            panel.set_settings(controller.dock.calculation_page.settings())
            self.panels[engine.id] = panel
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            scroll.setWidget(panel)
            self.configuration.addWidget(scroll)
        self.engine.setCurrentIndex(self.engine.findData(controller.dock.calculation_page.engine.currentData()))
        self.configuration.setCurrentIndex(self.engine.currentIndex())
        self.engine.currentIndexChanged.connect(self.configuration.setCurrentIndex)
        engine_form.addRow("Engine", self.engine)
        options_layout.addLayout(engine_form)
        self.legacy = QComboBox()
        self.legacy.addItem("Previous calculation settings…", None)
        for item in stored["legacy_settings"]:
            self.legacy.addItem(item["name"], item["settings"])
        self.legacy.setVisible(bool(stored["legacy_settings"]))
        self.legacy.currentIndexChanged.connect(self.load_legacy)
        options_layout.addWidget(self.legacy)
        options_layout.addWidget(self.configuration, 1)
        options_layout.addWidget(explanation("All receiver layers included in each variant are calculated."))
        setup.addWidget(options, 3)

        self.status = explanation("Choose variants and parameters, then start the calculation.")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.model_summary = explanation("")
        self.model_summary.setTextFormat(Qt.PlainText)
        self.model_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.model_summary.hide()
        layout.addWidget(self.model_summary)
        self.path_counts = explanation("")
        self.path_counts.setToolTip(path_counts_help())
        self.path_counts.setAccessibleName("Calculations checked and pruned")
        self.path_counts.hide()
        layout.addWidget(self.path_counts)
        self.work_progress = explanation("")
        self.work_progress.setAccessibleName("Live calculation worker activity")
        self.work_progress.setToolTip(WORK_PROGRESS_HELP)
        self.work_progress.hide()
        layout.addWidget(self.work_progress)
        self.run_time = explanation("")
        self.run_time.setAccessibleName("Calculation elapsed time")
        self.run_time.setToolTip("Time includes preparation, engine processing and saving results.")
        self.run_time.hide()
        layout.addWidget(self.run_time)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Overall calculation progress")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.receiver_activity = QGroupBox("Receiver activity — hover for step timings")
        activity_layout = QVBoxLayout(self.receiver_activity)
        self.receiver_table = QTableWidget(0, 5)
        self.receiver_table.setHorizontalHeaderLabels([
            "Receiver", "Source", "Current step", "Step time", "Receiver time"])
        self.receiver_table.setAccessibleName("Live receiver calculation details")
        self.receiver_table.verticalHeader().hide()
        self.receiver_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.receiver_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.receiver_table.setWordWrap(False)
        self.receiver_table.verticalHeader().setDefaultSectionSize(
            2 * self.receiver_table.fontMetrics().lineSpacing() + 12)
        self.receiver_table.setTextElideMode(Qt.ElideRight)
        self.receiver_table.setMinimumHeight(215)
        self.receiver_table.setMaximumHeight(215)
        header = self.receiver_table.horizontalHeader()
        header.setMinimumSectionSize(70)
        for column, width in ((0, 200), (1, 180)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.receiver_table.setColumnWidth(column, width)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for column in (3, 4):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            self.receiver_table.setColumnWidth(column, 115)
        activity_layout.addWidget(self.receiver_table)
        self.receiver_table.setToolTip(
            "Shows up to 50 active receivers in input order and the five most recent completions. "
            "All receivers continue calculating. Hover over a row for full details and time spent in each step.")
        self.receiver_activity.hide()
        layout.addWidget(self.receiver_activity)
        actions = QHBoxLayout()
        self.check_button = QPushButton("Check inputs")
        self.check_button.setToolTip("Check local conversion against the engine profile. The service checks propagation geometry and resource limits when calculating.")
        self.check_button.clicked.connect(lambda: self.start(True))
        self.start_button = QPushButton("Start calculation")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(lambda: self.start(False))
        self.cancel_button = QPushButton("Cancel calculation")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        for button in (self.check_button, self.start_button, self.cancel_button, self.close_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.variants.itemChanged.connect(self.selection_changed)
        self.engine.currentIndexChanged.connect(self.selection_changed)
        self.selection_changed()
        runner = controller.engine_runner
        for signal, slot in ((runner.variantStarted, self.variant_started),
                             (runner.progressChanged, self.update_progress),
                             (runner.succeeded, self.succeeded),
                             (runner.failed, self.failed), (runner.stopped, self.stopped)):
            signal.connect(slot)
            self._connections.append((signal, slot))

    def selected_variants(self):
        return [self.variants.item(row, 0).data(Qt.UserRole)
                for row in range(self.variants.rowCount())
                if self.variants.item(row, 0).checkState() == Qt.Checked]

    def load_legacy(self, *args):
        value = self.legacy.currentData()
        if value is not None and self.engine.currentData() in self.panels:
            self.panels[self.engine.currentData()].set_settings(value)

    def select_all(self, selected):
        for row in range(self.variants.rowCount()):
            self.variants.item(row, 0).setCheckState(Qt.Checked if selected else Qt.Unchecked)

    def selection_changed(self, *args):
        enabled = bool(self.selected_variants()) and self.engine.currentData() in self.panels and not self._running
        self.start_button.setEnabled(enabled)
        self.check_button.setEnabled(enabled)

    def remember(self):
        QSettings().setValue(self.preference_key, json.dumps({
            "variants": self.selected_variants(), "engine": self.engine.currentData()}))

    def set_variant_status(self, variant_id, text):
        for row in range(self.variants.rowCount()):
            if self.variants.item(row, 0).data(Qt.UserRole) == variant_id:
                self.variants.item(row, 1).setText(text)

    def set_running(self, running):
        if running and not self._running:
            self._stopping = False
            self._batch_started = monotonic()
            self._batch_elapsed = 0
            self._variant_started_at.clear()
            self._variant_elapsed.clear()
            self._active_variant = None
            self.model_summary.clear()
            self.model_summary.hide()
            self._clock.start()
            self.work_progress.clear()
            self.work_progress.hide()
            self.path_counts.clear()
            self.path_counts.hide()
        elif not running and self._running:
            self._batch_elapsed = max(0, monotonic() - self._batch_started)
            self._clock.stop()
        if not running:
            self.work_progress.hide()
        self._running = running
        self.update_run_time()
        self.options.setEnabled(not running)
        self.all_button.setEnabled(not running)
        self.none_button.setEnabled(not running)
        # Keep names and live statuses readable and scrollable during the run.
        for row in range(self.variants.rowCount()):
            item = self.variants.item(row, 0)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable |
                          (Qt.NoItemFlags if running else Qt.ItemIsUserCheckable))
        self.close_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.selection_changed()

    def start(self, validate=False):
        if self._running:
            return
        self._selected = self.selected_variants()
        if not self._selected:
            self.status.setText("Choose at least one variant.")
            return
        try:
            if self.engine.currentData() not in self.panels:
                raise ModelError("Choose an available engine in Settings.")
            settings = self.panels[self.engine.currentData()].settings()
            self._completed = 0
            self._warned = []
            self._validate = validate
            self._pruning = settings.get("pruning_strategy") is not None
            self.path_counts.setToolTip(path_counts_help(self._pruning))
            self.remember()
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)
            self.clear_receiver_activity()
            for row in range(self.variants.rowCount()):
                self.variants.item(row, 1).setText("Queued" if self.variants.item(row, 0).data(Qt.UserRole) in self._selected else "")
            self.set_running(True)
            started = self.controller.calculate(validate=validate, variant_ids=self._selected,
                                                settings=settings, engine_id=self.engine.currentData())
            if started is False:
                self.finish("Calculation cancelled; layer edits were not saved.")
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            self.finish("Cannot start: " + str(error))

    def matches(self, context):
        return self._running and context["store"] is self.store

    def variant_started(self, context):
        if self.matches(context):
            self.work_progress.clear()
            self.work_progress.hide()
            self.path_counts.setText(path_counts_text(None, self._pruning))
            self.path_counts.setVisible(not self._validate)
            self._active_variant = context["variant_id"]
            self._variant_started_at[self._active_variant] = monotonic()
            self.model_summary.clear()
            self.model_summary.hide()
            self.update_run_time()
            self.clear_receiver_activity()
            self.set_variant_status(context["variant_id"], "Checking" if self._validate else "Running")
            self.update_progress(context, {"phase": "loading", "completed": 0, "total": 0})

    def update_progress(self, context, value):
        if not self.matches(context):
            return
        self.update_model_summary(context)
        if value.get("path_counts") is not None:
            self.path_counts.setText(path_counts_text(value["path_counts"], self._pruning))
            self.path_counts.show()
        self.work_progress.setText(work_progress_text(value.get("work")))
        self.work_progress.setVisible(value.get("work") is not None and
                                      value.get("state") not in ("completed", "cancelled", "failed"))
        if "percent" in value:
            percent = min(100.0, max(0.0, value["percent"]))
            if value.get("state") != "completed":
                percent = min(99.0, percent)
            phase = {"preparation": "Preparing calculation", "propagation": "Calculating sound levels",
                     "queued": "Waiting for the engine", "completed": "Saving results",
                     "cancelling": "Stopping calculation — waiting for workers to finish"}.get(
                         value.get("phase"), value.get("phase", "running").replace("_", " ").title())
            if self._stopping and value.get("state") != "completed":
                phase = "Stopping calculation — waiting for workers to finish"
            self.status.setText(
                f"Variant {context['batch_index'] + 1} of {context['batch_total']} · {context['variant_name']}\n"
                f"{phase} · {percent:.0f}%")
            self.progress.setRange(0, 1000)
            self.progress.setValue(min(999, int(1000 * (context["batch_index"] + percent / 100) / context["batch_total"])))
            return
        description = {
            "loading": "Connecting to the engine…",
            "checking": "Checking local model compatibility…",
            "preparing": "Preparing engine input…",
            "saving": "Saving completed results…",
            "downloading": (f"Downloading area result rasters ({value.get('downloaded', 0) + 1} of "
                            f"{value.get('total_rasters', 1)})…"),
        }.get(value.get("phase"), "Waiting for engine progress…")
        if self._stopping:
            description = "Stopping calculation — waiting for the engine to confirm"
        self.status.setText(f"Variant {context['batch_index'] + 1} of {context['batch_total']} · {context['variant_name']}\n{description}")
        self.progress.setRange(0, 1000)
        # Local conversion has no server task credit. Only API snapshots advance
        # the current variant; do not manufacture progress before submission.
        # Downloads follow completed calculation work; do not move the bar back.
        done = 0.99 if value.get("phase") == "downloading" else 0
        self.progress.setValue(int(1000 * (context["batch_index"] + done) / context["batch_total"]))

    def update_run_time(self):
        if self._batch_started is None:
            return
        seconds = max(0, monotonic() - self._batch_started) if self._running else self._batch_elapsed
        text = f"{'Elapsed' if self._running else 'Total time'}: {_elapsed_text(seconds)}"
        if self._running and len(self._selected) > 1 and self._active_variant in self._variant_started_at:
            seconds = self._variant_elapsed.get(self._active_variant)
            if seconds is None:
                seconds = max(0, monotonic() - self._variant_started_at[self._active_variant])
            text += f" · Current variant: {_elapsed_text(seconds)}"
        self.run_time.setText(text)
        self.run_time.show()

    def update_model_summary(self, context):
        summary = context.get("model_summary")
        if not summary:
            return
        sources, receivers = summary["source_count"], summary["receiver_count"]
        pairs = sources * receivers
        if summary.get("receivers_resolved", True):
            counts = f"{sources:,} sources · {receivers:,} receivers · {pairs:,} source–receiver pairs"
        else:
            areas = summary["area_receiver_count"]
            counts = (f"{sources:,} sources · {receivers:,} point receivers + {areas:,} receiver areas\n"
                      f"{pairs:,} point-receiver pairs; additional grid pairs determined by the engine")
        counts += f" · {summary.get('building_count', 0):,} buildings · {summary.get('barrier_count', 0):,} barriers"
        self.model_summary.setText(f"{context['variant_name']}: {counts}")
        self.model_summary.show()

    def finish_variant(self, context, status):
        started = self._variant_started_at.get(context["variant_id"], self._batch_started)
        if started is not None:
            seconds = max(0, monotonic() - started)
            self._variant_elapsed[context["variant_id"]] = seconds
            status += f" · {_elapsed_text(seconds)}"
        self.set_variant_status(context["variant_id"], status)

    def clear_receiver_activity(self):
        self.receiver_table.setRowCount(0)
        self.receiver_activity.setTitle("Receiver activity — hover for step timings")
        self.receiver_activity.hide()

    def update_receiver_activity(self, receivers, active_count=None):
        shown_active = sum(not receiver.get("finished", False) for receiver in receivers)
        title = "Receiver activity — "
        if type(active_count) is int and active_count > shown_active:
            title += f"showing {shown_active:,} of {active_count:,} active; "
        self.receiver_activity.setTitle(title + "hover for step timings")
        self.receiver_table.setUpdatesEnabled(False)
        try:
            self.receiver_table.setRowCount(len(receivers))
            for row, receiver in enumerate(receivers):
                name = receiver.get("name") or receiver.get("id") or "Unnamed receiver"
                receiver_text = f"{receiver['index']}. {name}"
                source_text = receiver.get("source_name") or receiver.get("source_id") or "—"
                if receiver.get("source_index", 0) > 0:
                    source_text = f"{receiver.get('source_index', 0)} of {receiver['source_total']} · {source_text}"
                stage = receiver["stage"]
                detail = receiver.get("detail", "")
                if "work_completed" in receiver:
                    work = f"{receiver['work_completed']:,} reflection samples processed"
                    detail = f"{detail} · {work}" if detail else work
                stage = stage.replace("\n", " ").replace("\r", " ")
                detail = detail.replace("\n", " ").replace("\r", " ")
                step_text = f"{stage}\n{detail}" if detail else stage
                stage_seconds = receiver.get("stage_elapsed_seconds", 0)
                elapsed_seconds = receiver.get("elapsed_seconds", 0)
                tooltip = (f"Receiver: {receiver_text}\nSource: {source_text}\nStep: {step_text}"
                           f"\nCurrent step: {_elapsed_text(stage_seconds)}\nReceiver total: {_elapsed_text(elapsed_seconds)}")
                timings = receiver.get("timings", {})
                if timings:
                    tooltip += "\n\nTime spent in each step (all sources):"
                    for name, seconds in sorted(timings.items(), key=lambda item: (-item[1], item[0])):
                        tooltip += f"\n{name}: {_elapsed_text(seconds)}"
                stage_time = "—" if receiver.get("finished") and stage == "Complete" else _elapsed_text(stage_seconds)
                for column, text in enumerate((receiver_text, source_text, step_text,
                                               stage_time, _elapsed_text(elapsed_seconds))):
                    item = self.receiver_table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem()
                        self.receiver_table.setItem(row, column, item)
                    item.setText(text if column == 2 else text.replace("\n", " ").replace("\r", " "))
                    item.setToolTip(_plain_tooltip(tooltip))
                    if column >= 3:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.receiver_activity.setVisible(bool(receivers))
        finally:
            self.receiver_table.setUpdatesEnabled(True)

    def succeeded(self, context, result):
        if not self.matches(context):
            return
        self._completed += 1
        if "source_count" in result and "receiver_count" in result:
            summary = dict(context.get("model_summary", {}))
            summary.update({key: result[key] for key in ("receiver_count", "barrier_count") if key in result})
            scene_counts = calculation_scene_counts(result)
            summary["source_count"] = scene_counts["source_count"]
            if "building_count" in result or "solid_building_count" in result:
                summary["building_count"] = scene_counts["building_count"]
            if not self._validate:
                summary["receivers_resolved"] = True
            context["model_summary"] = summary
            self.update_model_summary(context)
        warned = bool(result.get("pathway_warnings"))
        if warned:
            self._warned.append((context["variant_name"], result["pathway_warnings"]))
        self.finish_variant(context, "Valid" if self._validate else "Complete with warnings" if warned else "Complete")
        self.progress.setValue(int(1000 * self._completed / len(self._selected)))
        if self._completed == len(self._selected):
            self.finish(f"{'Input checks complete' if self._validate else 'Calculation complete'} · {self._completed} of {len(self._selected)} variants. "
                        + ("Local engine compatibility checked. The service checks propagation paths and resource limits when calculating." if self._validate else "Results saved to the model.")
                        + "".join(f"\n{name}: " + "\n".join(lines) for name, lines in self._warned))

    def failed(self, context, message):
        if self.matches(context):
            self.finish_variant(context, "Failed")
            self.finish(f"{context['variant_name']} failed: {message}\n{self._completed} of {len(self._selected)} variants completed.")

    def stopped(self, context):
        if self.matches(context):
            self.finish_variant(context, "Stopped")
            self.finish(f"Calculation stopped · {self._completed} of {len(self._selected)} variants completed. "
                        "Completed results are kept. A run finishing just before cancellation may also have saved results.")

    def finish(self, message):
        for row in range(self.variants.rowCount()):
            if self.variants.item(row, 1).text() == "Queued":
                self.variants.item(row, 1).setText("Not run")
        self.set_running(False)
        self.status.setText(message)
        if self.receiver_table.rowCount():
            title = self.receiver_activity.title()
            if "last update" not in title:
                self.receiver_activity.setTitle(title.replace("hover for step timings", "last update; hover for step timings"))

    def cancel(self):
        if self._running:
            self._stopping = True
            self.cancel_button.setEnabled(False)
            self.status.setText("Stopping the calculation… Remaining variants will not run.")
            self.controller.cancel_calculation()

    def reject(self):
        if not self._running:
            self.remember()
            super().reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
        else:
            self.remember()
            super().closeEvent(event)

    def dispose(self):
        self._clock.stop()
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
        self._running = False
        self.close()
        self.deleteLater()
