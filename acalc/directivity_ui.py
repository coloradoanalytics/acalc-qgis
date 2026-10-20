# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Project directionality library, table editor, and point-source assignment."""

from copy import deepcopy
import csv
import io
import json
import math
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QFileDialog, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget)

from .numeric_controls import get_decimal
from . import directivity as model
from .directivity_preview import DirectivityPreview
from .directivity_templates import DirectivityTemplateDialog, number
from .schema import ModelError
from .spectra_ui import wrapped_label
from .spectrum_table import SpectrumTable
from .storage import require_name


class DirectionTable(SpectrumTable):
    paste_rejected = pyqtSignal(str)

    def paste_cells(self):
        cells = list(csv.reader(io.StringIO(QApplication.clipboard().text().rstrip('\r\n')), delimiter='\t'))
        if not cells:
            return
        row, col = max(0, self.currentRow()), max(0, self.currentColumn())
        if (any(col + len(r) > self.columnCount() for r in cells) or
                (row + len(cells)) * self.columnCount() > model.MAX_CELLS):
            self.paste_rejected.emit("Paste exceeds the table width or size limit. Add frequency columns first.")
            return
        self.blockSignals(True)
        self.setRowCount(max(self.rowCount(), row + len(cells)))
        for r, values in enumerate(cells):
            for c, value in enumerate(values):
                self.setItem(row+r, col+c, QTableWidgetItem(value.strip()))
        self.blockSignals(False)
        self.itemChanged.emit(self.item(row, col))


PRINCIPALS = [("Front (+X)", (0, 0)), ("Back (−X)", (180, 0)), ("Left (+Y)", (90, 0)),
              ("Right (−Y)", (270, 0)), ("Top (+Z)", (0, 90)), ("Bottom (−Z)", (0, -90)), ("Custom…", None)]


class DirectivityDialog(QDialog):
    def __init__(self, parent=None, record=None, input_unit="m"):
        super().__init__(parent)
        self.input_unit = input_unit
        self.setWindowTitle("Directionality · project local library")
        self.resize(1080, 800)
        self.value = None
        self._loading = False
        self._definition = deepcopy(record["definition"]) if record else model.definition()
        self.frequencies = []
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.template_button = QPushButton("Use a template…")
        self.template_button.clicked.connect(self.use_template)
        self.import_button = QPushButton("Import table…")
        self.import_button.clicked.connect(self.import_file)
        self.clf_button = QPushButton("Attach CLF…")
        self.clf_button.setToolTip("Retain a CF1/CF2 original with the definition. Binary decoding is not yet available.")
        self.clf_button.clicked.connect(self.attach_clf)
        self.export_button = QPushButton("Export definition…")
        self.export_button.clicked.connect(self.export_file)
        for button in (self.template_button, self.import_button, self.clf_button, self.export_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        split = QSplitter(Qt.Horizontal)
        fields = QWidget()
        form = QFormLayout(fields)
        self.name = QLineEdit(record["name"] if record else "")
        self.notes = QTextEdit()
        self.notes.setAcceptRichText(False)
        self.notes.setMaximumHeight(100)
        self.principal = QComboBox()
        for label, angles in PRINCIPALS:
            self.principal.addItem(label, angles)
        self.azimuth = number(0, 359.999999, 0, 6)
        self.elevation = number(-90, 90, 0, 6)
        self.custom = QWidget()
        custom_form = QFormLayout(self.custom)
        custom_form.setContentsMargins(0, 0, 0, 0)
        custom_form.addRow("Azimuth °", self.azimuth)
        custom_form.addRow("Elevation °", self.elevation)
        self.row_principal = QPushButton("Use selected row as principal")
        self.row_principal.clicked.connect(self.use_row_principal)
        form.addRow("Name", self.name)
        form.addRow("Notes", self.notes)
        form.addRow("Principal direction", self.principal)
        form.addRow(self.custom)
        form.addRow(self.row_principal)
        form.addRow(wrapped_label("Orange is the aiming reference. It need not be the loudest direction."))
        split.addWidget(fields)
        self.preview = DirectivityPreview()
        split.addWidget(self.preview)
        split.setSizes([400, 600])
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        actions.addWidget(QLabel("Directions × frequencies · dB"))
        actions.addStretch()
        for label, action in (("Add direction", self.add_direction), ("Remove direction", self.remove_direction),
                              ("Add frequency", self.add_frequency), ("Remove frequency", self.remove_frequency)):
            button = QPushButton(label)
            button.clicked.connect(action)
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addWidget(wrapped_label("Azimuth: 0° front, 90° left · Elevation: +90° top · Blank = unknown · 0 dB is a value, not silence. Copy/paste from a spreadsheet."))
        self.table = DirectionTable(0, 10)
        self.table.verticalHeader().setMinimumSectionSize(24)
        self.table.verticalHeader().setDefaultSectionSize(29)
        self.table.horizontalHeader().setMinimumSectionSize(65)
        self.table.horizontalHeader().setDefaultSectionSize(83)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(185)
        self.table.paste_rejected.connect(self.show_error)
        layout.addWidget(self.table, 2)
        reference = QHBoxLayout()
        reference.addWidget(QLabel("Level reference"))
        self.reference = QComboBox()
        self.reference.addItem("Relative levels · preserve total power", "relative_power")
        self.reference.addItem("Directivity corrections · use as entered", "correction")
        reference.addWidget(self.reference)
        reference.addStretch()
        layout.addLayout(reference)
        self.provenance = wrapped_label("")
        layout.addWidget(self.provenance)
        self.usage = wrapped_label("This definition is shared by assigned sources across all project variants.")
        layout.addWidget(self.usage)
        self.status = wrapped_label("")
        layout.addWidget(self.status)
        self.error = wrapped_label("")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Save to local library")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(120)
        self.refresh_timer.timeout.connect(self.update_preview)
        self.table.itemChanged.connect(self.data_changed)
        self.table.currentCellChanged.connect(self.select_cell)
        self.principal.currentIndexChanged.connect(self.principal_changed)
        self.azimuth.valueChanged.connect(self.data_changed)
        self.elevation.valueChanged.connect(self.data_changed)
        self.reference.currentIndexChanged.connect(self.data_changed)
        self.load_definition(self._definition)

    def load_definition(self, value):
        self._loading = True
        self._definition = deepcopy(value)
        self.frequencies = list(value["frequencies_hz"])
        self.notes.setPlainText(value["notes"])
        self.table.setColumnCount(2 + len(self.frequencies))
        self.table.setHorizontalHeaderLabels(["Azimuth °", "Elevation °"] + [f"{f:g} Hz" for f in self.frequencies])
        self.table.setRowCount(len(value["directions"]))
        for r, row in enumerate(value["directions"]):
            for c, item in enumerate([row["azimuth_deg"], row["elevation_deg"]] + row["levels_db"]):
                self.table.setItem(r, c, QTableWidgetItem("" if item is None else repr(item)))
        p = value["principal"]
        angles = p["azimuth_deg"], p["elevation_deg"]
        self.principal.setCurrentIndex(next((i for i, pair in enumerate(PRINCIPALS) if pair[1] == angles), 6))
        self.azimuth.setValue(angles[0])
        self.elevation.setValue(angles[1])
        self._loaded_angles = angles
        self._displayed_angles = (self.azimuth.value(), self.elevation.value())
        self.custom.setVisible(self.principal.currentData() is None)
        self.reference.setCurrentIndex(self.reference.findData(value["level_reference"]))
        self._loading = False
        self.update_preview()

    def read_value(self):
        value = deepcopy(self._definition)
        value["frequencies_hz"] = list(self.frequencies)
        value["notes"] = self.notes.toPlainText()
        value["level_reference"] = self.reference.currentData()
        angles = self.principal.currentData() or (self.azimuth.value(), self.elevation.value())
        if self.principal.currentData() is None and angles == self._displayed_angles:
            angles = self._loaded_angles
        value["principal"] = {"azimuth_deg": angles[0], "elevation_deg": angles[1]}
        value["directions"] = []
        try:
            for r in range(self.table.rowCount()):
                texts = [self.table.item(r, c).text().strip() if self.table.item(r, c) else "" for c in range(self.table.columnCount())]
                value["directions"].append({"azimuth_deg": float(texts[0]), "elevation_deg": float(texts[1]),
                                            "levels_db": [float(t) if t else None for t in texts[2:]]})
        except ValueError as error:
            raise ModelError(f"Direction {r+1}: enter numerical angles and dB values; only dB cells may be blank.") from error
        return model.validate_definition(value)

    def data_changed(self, *args):
        if not self._loading:
            if self._definition["generator"]:
                self._definition["generator"]["modified"] = True
            self.refresh_timer.start()

    def principal_changed(self):
        self.custom.setVisible(self.principal.currentData() is None)
        self.data_changed()

    def show_error(self, text):
        self.error.setText(text)
        self.error.show()

    def update_preview(self):
        try:
            value = self.read_value()
            self.preview.set_definition(value)
            self.error.hide()
            self.status.setText(f'{len(value["directions"])} directions × {len(self.frequencies)} frequencies · {model.status(value)}')
            generator = value["generator"]
            origin = "Manual definition" if not generator else ("Template: " + generator["kind"] +
                      (" · manually modified" if generator["modified"] else " · original inputs retained"))
            attachment = value["attachment"]
            if attachment:
                origin += f' · CLF: {attachment["filename"]} · decoding unavailable'
            self.provenance.setText(origin)
            self.provenance.setToolTip(json.dumps(generator, indent=2) if generator else "")
        except ModelError as error:
            self.show_error(str(error))
            self.preview.canvas.mesh = []
            self.preview.canvas.message = "Correct the table to update the preview"
            self.preview.canvas.update()

    def select_cell(self, row, column, *args):
        if self._loading:
            return
        if 2 <= column < 2 + len(self.frequencies):
            self.preview.band.setCurrentIndex(self.preview.band.findData(self.frequencies[column-2]))
        try:
            a, e = [float(self.table.item(row, c).text()) for c in (0, 1)]
            model.validate_angles(a, e)
            self.preview.canvas.selected = model.direction(a, e)
        except (ValueError, AttributeError, ModelError):
            self.preview.canvas.selected = None
        self.preview.canvas.update()

    def add_direction(self):
        if (self.table.rowCount()+1) * len(self.frequencies) > model.MAX_CELLS:
            self.show_error("Direction table size limit reached.")
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        for c in range(self.table.columnCount()):
            self.table.setItem(row, c, QTableWidgetItem(""))
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def remove_direction(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self.data_changed()

    def add_frequency(self):
        frequency, ok = get_decimal(self, "Add frequency", "Centre frequency (Hz)", 1000, .001, 1000000, 3)
        if not ok:
            return
        if frequency in self.frequencies or len(self.frequencies) >= 64:
            self.show_error("Choose a new frequency; at most 64 frequencies are supported.")
            return
        if self.table.rowCount() * (len(self.frequencies)+1) > model.MAX_CELLS:
            self.show_error("Direction table size limit reached.")
            return
        index = sum(f < frequency for f in self.frequencies)
        self.frequencies.insert(index, frequency)
        self.table.insertColumn(index+2)
        self.table.setHorizontalHeaderItem(index+2, QTableWidgetItem(f"{frequency:g} Hz"))
        self.data_changed()

    def remove_frequency(self):
        column = self.table.currentColumn()
        if column < 2 or len(self.frequencies) <= 1:
            self.show_error("Select a frequency column. Keep at least one frequency.")
            return
        if QMessageBox.question(self, "Remove frequency", f"Remove {self.frequencies[column-2]:g} Hz and its values?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            del self.frequencies[column-2]
            self.table.removeColumn(column)
            self.data_changed()

    def use_row_principal(self):
        try:
            row = self.table.currentRow()
            a, e = [float(self.table.item(row, c).text()) for c in (0, 1)]
            model.validate_angles(a, e)
            self.principal.setCurrentIndex(6)
            self.azimuth.setValue(a)
            self.elevation.setValue(e)
            self.data_changed()
        except (ValueError, AttributeError, ModelError):
            self.show_error("Select a row with valid angles first.")

    def use_template(self):
        dialog = DirectivityTemplateDialog(self, self._definition["generator"], input_unit=self.input_unit)
        if dialog.exec() == QDialog.Accepted and dialog.value:
            notes = self.notes.toPlainText().strip()
            if notes and notes != dialog.value["notes"]:
                dialog.value["notes"] = notes + "\n\n" + dialog.value["notes"]
            self.load_definition(dialog.value)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import directionality table", "", "Directionality (*.json *.csv *.tsv)")
        if not path:
            return
        try:
            if Path(path).stat().st_size > 20 * 1024 * 1024:
                raise ModelError("Directionality import is limited to 20 MiB.")
            text = Path(path).read_text(encoding="utf-8-sig")
            value = model.validate_definition(json.loads(text)) if Path(path).suffix.lower() == ".json" else model.import_table(text)
            self.load_definition(value)
            if not self.name.text().strip():
                self.name.setText(Path(path).stem)
        except (OSError, ValueError, TypeError) as error:
            self.show_error(str(error))

    def attach_clf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Retain original CLF · decoding unavailable", "", "CLF distribution (*.cf1 *.cf2 *.CF1 *.CF2)")
        if not path:
            return
        try:
            value = self.read_value()
            attached = model.attach_clf(path)
            value["attachment"] = attached["attachment"]
            value["notes"] += ("\n" if value["notes"] else "") + attached["notes"]
            self.load_definition(value)
        except (OSError, ModelError) as error:
            self.show_error(str(error))

    def export_file(self):
        try:
            value = self.read_value()
            path, _ = QFileDialog.getSaveFileName(self, "Export directionality definition", "directionality.json", "JSON (*.json)")
            if path:
                Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        except (OSError, ModelError) as error:
            self.show_error(str(error))

    def validate(self):
        self.refresh_timer.stop()
        try:
            require_name(self.name.text())
            self.value = self.read_value()
            self.accept()
        except ModelError as error:
            self.show_error(str(error))


class DirectivityPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.records = {}
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Project local directionality library · shared by all variants"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Values"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda *args: controller.run(controller.edit_directivity))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        self.notes = wrapped_label("")
        layout.addWidget(self.notes)
        self.buttons = {}
        actions = QHBoxLayout()
        for label, callback in (("New…", lambda: controller.edit_directivity(create=True)),
                                ("Edit…", controller.edit_directivity),
                                ("Duplicate…", lambda: controller.edit_directivity(create=True, duplicate=True)),
                                ("Delete", controller.delete_directivity)):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, action=callback: controller.run(action))
            actions.addWidget(button)
            self.buttons[label] = button
        layout.addLayout(actions)
        layout.addWidget(wrapped_label("Edit tables, generate templates, and assign patterns in point-source properties. Directionality calculation is not yet available."))

    def selected_id(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def refresh(self, preferred=None):
        selected = preferred or self.selected_id()
        records = self.controller.store.directivities() if self.controller.store else []
        self.records = {r["directivity_id"]: r for r in records}
        self.table.setRowCount(0)
        self.table.setRowCount(len(records))
        chosen = 0
        for i, record in enumerate(records):
            item = QTableWidgetItem(record["name"])
            item.setData(Qt.UserRole, record["directivity_id"])
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem(f'{len(record["definition"]["directions"])} × {len(record["definition"]["frequencies_hz"])}'))
            if record["directivity_id"] == selected:
                chosen = i
        if records:
            self.table.selectRow(chosen)
        self.setEnabled(self.controller.store is not None)
        self.selection_changed()

    def selection_changed(self):
        record = self.records.get(self.selected_id())
        for label in ("Edit…", "Duplicate…", "Delete"):
            self.buttons[label].setEnabled(record is not None)
        self.notes.setText((model.status(record["definition"]) + f' · revision {record["revision"]}\n' +
                            record["definition"]["notes"]) if record else "Choose New to create a directionality.")


class SourceDirectivityWidget(QWidget):
    def __init__(self, records=(), identity=None, orientation=None, store=None, edit_callback=None, parent=None):
        super().__init__(parent)
        self.records = {r["directivity_id"]: r for r in records}
        self.store, self.edit_callback = store, edit_callback
        self.pose = model.read_orientation(orientation)
        self.history = []
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.choice = QComboBox()
        bar.addWidget(self.choice, 1)
        self.edit_button = QPushButton("Edit definition…")
        self.edit_button.clicked.connect(self.edit_definition)
        bar.addWidget(self.edit_button)
        self.new_button = QPushButton("New…")
        self.new_button.clicked.connect(lambda: self.edit_definition(create=True))
        self.new_button.setEnabled(edit_callback is not None)
        bar.addWidget(self.new_button)
        layout.addLayout(bar)
        body = QHBoxLayout()
        controls = QVBoxLayout()
        self.definition_label = wrapped_label("")
        controls.addWidget(self.definition_label)
        controls.addWidget(QLabel("Aim principal direction"))
        compass = QGridLayout()
        self.aim_buttons = {}
        for i, name in enumerate(("Up", "North", "South", "Down", "East", "West")):
            button = QPushButton(name)
            button.clicked.connect(lambda checked=False, target=name: self.aim(target))
            compass.addWidget(button, i//3, i%3)
            self.aim_buttons[name] = button
        controls.addLayout(compass)
        controls.addWidget(QLabel("Rotate current orientation by"))
        rotate = QGridLayout()
        self.increments, self.rotation_buttons = {}, {}
        for r, (axis, label) in enumerate((("Up", "Up / Z axis"), ("East", "East–west axis"), ("North", "North–south axis"))):
            rotate.addWidget(QLabel(label), r, 0)
            spin = number(0, 180, 15, 2)
            spin.setSuffix("°")
            spin.setAccessibleName(label + " rotation increment")
            self.increments[axis] = spin
            for col, sign in ((1, -1), (3, 1)):
                button = QPushButton("−" if sign < 0 else "+")
                button.setAccessibleName(f"Rotate {label} {'negative' if sign < 0 else 'positive'}")
                button.setMaximumWidth(35)
                button.clicked.connect(lambda checked=False, a=axis, s=sign: self.rotate(a, s))
                self.rotation_buttons[axis, sign] = button
                rotate.addWidget(button, r, col)
            rotate.addWidget(spin, r, 2)
        controls.addLayout(rotate)
        controls.addWidget(wrapped_label("Degrees · fixed project axes"))
        self.undo_button = QPushButton("Undo last orientation change")
        self.undo_button.clicked.connect(self.undo)
        controls.addWidget(self.undo_button)
        controls.addStretch()
        body.addLayout(controls, 1)
        self.preview = DirectivityPreview(world=True)
        body.addWidget(self.preview, 1)
        layout.addLayout(body, 1)
        self.readout = wrapped_label("")
        self.preview.layout().insertWidget(1, self.readout)
        details_button = QPushButton("Definition notes & rotation convention")
        details_button.setCheckable(True)
        layout.addWidget(details_button)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.notes = wrapped_label("")
        details_layout.addWidget(self.notes)
        details_layout.addWidget(wrapped_label("North is grid north (+Y); East is +X. Positive rotation follows the right-hand rule. Aiming shortcuts reset roll; incremental rotations preserve the current orientation."))
        if edit_callback:
            details_layout.addWidget(wrapped_label("Definition edits save to the shared library immediately, even if you cancel this source dialog. Assignment and orientation save with Apply to layer."))
        self.details.hide()
        details_button.toggled.connect(self.details.setVisible)
        layout.addWidget(self.details)
        self.limit = wrapped_label("")
        self.preview.layout().insertWidget(2, self.limit)
        self.edit_button.setToolTip("Edits save immediately to the shared library, independently of source Apply/Cancel.")
        self.new_button.setToolTip("Create a saved project library definition, then assign it to this source.")
        self.fill_choices(identity)
        self.choice.currentIndexChanged.connect(self.changed_choice)
        self.refresh(pattern=True)

    def fill_choices(self, identity):
        self.choice.blockSignals(True)
        self.choice.clear()
        self.choice.addItem("None · omnidirectional", None)
        for record in self.records.values():
            self.choice.addItem(record["name"], record["directivity_id"])
        if identity and identity not in self.records:
            self.choice.addItem("Unavailable directionality", identity)
        self.choice.setCurrentIndex(max(0, self.choice.findData(identity)))
        self.choice.blockSignals(False)

    def selected_definition(self):
        record = self.records.get(self.choice.currentData())
        return record["definition"] if record else None

    def changed_choice(self):
        self.history.clear()
        value = self.selected_definition()
        self.pose = model.align(model.principal_vector(value), model.AXES["Up" if model.upright(value) else "North"]) if value else deepcopy(model.IDENTITY)
        self.refresh(pattern=True)

    def aim(self, target):
        value = self.selected_definition()
        if not value or (model.upright(value) and target != "Up"):
            return
        self.history.append(deepcopy(self.pose))
        self.pose = model.align(model.principal_vector(value), model.AXES[target])
        self.refresh()

    def rotate(self, axis, sign):
        value = self.selected_definition()
        if not value or (model.upright(value) and axis != "Up"):
            return
        self.history.append(deepcopy(self.pose))
        self.pose = model.compose(model.rotation(model.AXES[axis], sign*self.increments[axis].value()), self.pose)
        self.refresh()

    def undo(self):
        if self.history:
            self.pose = self.history.pop()
        self.refresh()

    def refresh(self, pattern=False):
        value = self.selected_definition()
        self.edit_button.setEnabled(value is not None and self.edit_callback is not None)
        self.undo_button.setEnabled(bool(self.history))
        for target, button in self.aim_buttons.items():
            button.setEnabled(value is not None and (not model.upright(value) or target == "Up"))
        for (axis, sign), button in self.rotation_buttons.items():
            button.setEnabled(value is not None and (not model.upright(value) or axis == "Up"))
        for axis, spin in self.increments.items():
            spin.setEnabled(value is not None and (not model.upright(value) or axis == "Up"))
        if pattern:
            self.preview.set_definition(value)
        self.preview.set_pose(self.pose)
        if value:
            vector = model.transform(self.pose, model.principal_vector(value))
            elevation = math.degrees(math.asin(max(-1, min(1, vector[2]))))
            bearing = math.degrees(math.atan2(vector[0], vector[1])) % 360
            self.readout.setText(("Principal: " + ("Up" if elevation > 0 else "Down") if abs(elevation) > 89.999 else f"Principal bearing: {bearing:.2f}° from grid north") + f" · elevation {elevation:.2f}°")
            p = value["principal"]
            self.definition_label.setText(f'Definition principal: azimuth {p["azimuth_deg"]:g}°, elevation {p["elevation_deg"]:g}°')
            self.notes.setText(value["notes"])
            self.limit.setText(("Template requires principal Up. " if model.upright(value) else "") + "Authoring only · Assigned directionality cannot be calculated yet.")
            self.limit.setToolTip(model.CALCULATION_LIMIT)
        else:
            self.definition_label.setText("No directional pattern assigned" if not self.choice.currentData() else "Unavailable directionality")
            self.readout.setText("Omnidirectional" if not self.choice.currentData() else "Repair the missing assignment before applying.")
            self.notes.clear()
            self.limit.clear()

    def edit_definition(self, create=False):
        if not self.edit_callback:
            return
        identity = self.edit_callback(identity=self.choice.currentData(), create=create)
        if self.store:
            self.records = {r["directivity_id"]: r for r in self.store.directivities()}
        old = self.choice.currentData()
        self.fill_choices(identity or old)
        if identity and identity != old:
            self.changed_choice()
        else:
            self.refresh(pattern=True)

    def attributes(self):
        identity = self.choice.currentData()
        if identity and identity not in self.records:
            raise ModelError("Choose an available directionality, or clear the unavailable assignment.")
        model.validate_orientation(self.pose)
        value = self.selected_definition()
        if value and model.upright(value) and model.dot(model.transform(self.pose, model.principal_vector(value)), model.AXES["Up"]) < 1 - 1e-8:
            raise ModelError("This stack template requires its principal to point Up. Use the Up shortcut.")
        return {"directivity_id": identity, "directivity_orientation": json.dumps(self.pose, allow_nan=False) if identity else None}
