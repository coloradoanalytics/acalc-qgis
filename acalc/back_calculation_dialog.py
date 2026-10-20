# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Fit adjustable sound power spectra to octave Measurements."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout)

from .back_calculation import fit_spectra
from .calculation_engines import available_engines
from .schema import ModelError
from .spectra import OCTAVE_BANDS


class BackCalculationDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller.iface.mainWindow())
        self.controller = controller
        self.store = controller.require_store()
        self._connections = []
        self._running = False
        self.relationships = None
        self.fit = None
        self.setWindowTitle("Back-calculate source power")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1000, 750)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.variant = QComboBox()
        selected = controller.selected_variant()
        for row in self.store.variants():
            self.variant.addItem(row["name"], row["variant_id"])
        if selected:
            self.variant.setCurrentIndex(self.variant.findData(selected["variant_id"]))
        form.addRow("Variant", self.variant)
        engine_id = controller.dock.calculation_page.engine.currentData()
        self.engine = QComboBox()
        self.panels = {}
        self.configuration = QStackedWidget()
        from .units import input_unit
        settings = dict(self.store.calculation()["settings"], pruning_strategy=None,
                        area_absolute_tolerance=0, ground_method="general")
        for engine in available_engines():
            if "source_receiver_transfers" not in engine.capabilities.get("features", []):
                continue
            self.engine.addItem(engine.name, engine.id)
            editor = engine.create_editor()
            if hasattr(editor, "set_input_unit"):
                editor.set_input_unit(input_unit(self.store))
            editor.set_settings(settings)
            # Fitting uses every octave path; a broadband pruning threshold
            # changes the transfer matrix as source powers change.
            editor.pruning_check.setEnabled(False)
            editor.pruning_basis.setEnabled(False)
            editor.pruning_note.setText("Back-calculation evaluates all source paths with pruning switched off.")
            editor.ground_method.setEnabled(False)
            editor.ground_method.setToolTip("Octave-band fitting requires the general ground method.")
            editor.numerical_fields["area_absolute_tolerance"].setEnabled(False)
            editor.numerical_fields["area_absolute_tolerance"].setToolTip(
                "Back-calculation uses zero absolute tolerance so source integration scales with sound power.")
            self.panels[engine.id] = editor
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(editor)
            scroll.setMinimumHeight(220)
            self.configuration.addWidget(scroll)
        self.engine.setCurrentIndex(self.engine.findData(engine_id))
        self.configuration.setCurrentIndex(self.engine.currentIndex())
        form.addRow("Engine", self.engine)
        layout.addLayout(form)
        layout.addWidget(self.configuration, 2)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["Spectrum", "Values"] + [f"{band} Hz" for band in OCTAVE_BANDS])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.status = QLabel("Choose a variant with Measurements and adjustable source spectra, then evaluate the fit. "
                             "Only Measurement receivers are required; applying the fit saves sound power spectra.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.status_scroll = QScrollArea()
        self.status_scroll.setWidgetResizable(True)
        self.status_scroll.setFrameShape(QScrollArea.NoFrame)
        self.status_scroll.setMinimumHeight(60)
        self.status_scroll.setMaximumHeight(150)
        self.status_scroll.setWidget(self.status)
        layout.addWidget(self.status_scroll)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.evaluate_button = QPushButton("Evaluate fit")
        self.evaluate_button.clicked.connect(self.evaluate)
        self.apply_button = QPushButton("Apply fitted spectra")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        self.cancel_button = QPushButton("Cancel evaluation")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(controller.cancel_calculation)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        for button in (self.evaluate_button, self.apply_button, self.cancel_button, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.variant.currentIndexChanged.connect(self.clear_fit)
        self.engine.currentIndexChanged.connect(self.engine_changed)
        for editor in self.panels.values():
            for field in editor.fields.values():
                field.valueChanged.connect(self.clear_fit)
            for field in {**editor.numerical_fields, **editor.optional_fields}.values():
                field.textChanged.connect(self.clear_fit)
            for field in (editor.meteorology, editor.ground_method, editor.pruning_basis):
                field.currentIndexChanged.connect(self.clear_fit)
            editor.pruning_check.toggled.connect(self.clear_fit)
        self.set_running(False)
        runner = controller.engine_runner
        for signal, slot in ((runner.progressChanged, self.progress_changed),
                             (runner.succeeded, self.succeeded),
                             (runner.failed, self.failed), (runner.stopped, self.stopped)):
            signal.connect(slot)
            self._connections.append((signal, slot))

    @property
    def settings_editor(self):
        return self.panels.get(self.engine.currentData())

    def engine_changed(self, *args):
        self.configuration.setCurrentIndex(self.engine.currentIndex())
        self.clear_fit()
        self.set_running(self._running)

    def clear_fit(self, *args):
        self.relationships = None
        self.fit = None
        self.apply_button.setEnabled(False)
        self.table.setRowCount(0)

    def set_running(self, running):
        self._running = running
        self.variant.setEnabled(not running)
        self.engine.setEnabled(not running)
        self.configuration.setEnabled(not running and self.settings_editor is not None)
        self.evaluate_button.setEnabled(not running and self.settings_editor is not None and self.variant.currentData() is not None)
        self.apply_button.setEnabled(not running and self.fit is not None)
        self.cancel_button.setEnabled(running)
        self.close_button.setEnabled(not running)

    def evaluate(self):
        self.clear_fit()
        try:
            if self.settings_editor is None:
                raise ModelError("Choose a calculation engine that supports back-calculation.")
            settings = self.settings_editor.settings()
            self.progress.setValue(0)
            self.set_running(True)
            self.status.setText("Evaluating source-to-Measurement relationships…")
            if self.controller.start_back_calculation(self.variant.currentData(), settings,
                                                       engine_id=self.engine.currentData()) is False:
                self.set_running(False)
                self.status.setText("Evaluation cancelled; layer edits were not saved.")
                return
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            self.set_running(False)
            self.status.setText("Cannot evaluate: " + str(error))

    def matches(self, context):
        return (self._running and context.get("relationships") and context["store"] is self.store
                and context.get("variant_id") == self.variant.currentData()
                and context.get("engine") == self.engine.currentData())

    def progress_changed(self, context, value):
        if self.matches(context):
            if "percent" in value:
                self.progress.setValue(round(value["percent"]))
                self.status.setText(f"Evaluating source-to-Measurement relationships: {value['percent']:.0f}%…")
            else:
                self.status.setText("Preparing the model…")

    def succeeded(self, context, relationships):
        if not self.matches(context):
            return
        try:
            fit = fit_spectra(relationships)
            names = {row["spectrum_id"]: row["name"] for row in self.store.spectra("sound_power")}
            original = {item["id"]: item for item in relationships["spectra"]}
            self.table.setRowCount(3 * len(fit["proposals"]))
            for group, (identity, values) in enumerate(sorted(fit["proposals"].items())):
                for offset, (label, levels) in enumerate((("Initial", original[identity]["initial"]),
                                                          ("Current", original[identity]["current"]),
                                                          ("Fitted", values))):
                    row = group * 3 + offset
                    self.table.setItem(row, 0, QTableWidgetItem(names.get(identity, identity)))
                    self.table.setItem(row, 1, QTableWidgetItem(label))
                    for column, level in enumerate(levels, 2):
                        self.table.setItem(row, column, QTableWidgetItem("" if level is None else f"{level:.1f}"))
            self.progress.setValue(100)
            qualifications = [relationships["standard"]] if relationships.get("standard") else []
            qualifications.extend(relationships.get("limitations", []))
            self.status.setText(f"{fit['observations']} octave observations · RMS level error: "
                                f"{fit['initial_rms_db']:.2f} dB at initial guesses → "
                                f"{fit['fitted_rms_db']:.2f} dB fitted. Review the fitted rows before applying." +
                                ("\n" + "\n".join(qualifications) if qualifications else ""))
            self.fit = fit
            self.relationships = relationships
        except (ModelError, ValueError, TypeError, KeyError, OverflowError) as error:
            self.clear_fit()
            self.status.setText("Fit failed: " + str(error))
        self.set_running(False)

    def failed(self, context, message):
        if self.matches(context):
            self.set_running(False)
            self.status.setText("Evaluation failed: " + message)

    def stopped(self, context):
        if self.matches(context):
            self.set_running(False)
            self.status.setText("Evaluation stopped; no spectra were changed.")

    def apply(self):
        if not self.fit or self._running:
            return
        try:
            self.controller.apply_back_calculation(self.relationships, self.fit["proposals"])
            self.controller.dock.spectra_page.refresh()
            self.controller.dock.results_page.refresh()
            self.controller.message(f"Updated {len(self.fit['proposals'])} adjustable sound power spectra. Initial guesses were retained.")
            self.status.setText("Fitted sound power saved. Initial guesses were retained; calculation results are now out of date.")
            self.clear_fit()
        except ModelError as error:
            self.status.setText("Cannot apply: " + str(error))

    def reject(self):
        if not self._running:
            super().reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
        else:
            super().closeEvent(event)

    def dispose(self):
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
        self._running = False
        self.close()
        self.deleteLater()
