# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Engine connection management and explicit numerical configuration."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .api_connections import (
    ApiConnectionStore, LOCAL_BASE_URL, capabilities_problem, connection_mode,
    normalize_base_url, validate_client_key,
)
from .api_connection_check import ConnectionCheck, check_message, engine_summary
from .calculation_settings import default_settings, validate_settings
from .numeric_controls import DecimalInput, IntegerInput
from .schema import ModelError
from .spectra_ui import AbsorptionSpinBox


def _label(text=""):
    value = QLabel(text)
    value.setTextFormat(Qt.PlainText)
    value.setWordWrap(True)
    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return value


def _engine_list():
    widget = QListWidget()
    widget.setWordWrap(False)
    widget.setTextElideMode(Qt.ElideNone)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    widget.setAccessibleName("Installed calculation engines")
    widget.setMinimumHeight(80)
    return widget


def _show_engines(widget, profiles):
    widget.clear()
    for profile in profiles:
        item = QListWidgetItem(engine_summary(profile), widget)
        item.setToolTip(profile.get("error") or profile.get("message") or
                        capabilities_problem(profile.get("capabilities")))


class EngineConnectionDialog(QDialog):
    """Edit and test a draft; only Save changes the profile or encrypted key."""

    def __init__(self, parent=None, *, connection_store=None, connection=None, client=None):
        super().__init__(parent)
        self.setWindowTitle("Edit engine" if connection else "New engine")
        self.setModal(True)
        self.resize(570, 400)
        self.connection_store = connection_store or ApiConnectionStore()
        self.original = connection or {}
        self.saved_connection = None
        self._closed = False
        self._tested = None
        self.checker = ConnectionCheck(self, client)
        self.checker.progress.connect(self._progress)
        self.checker.finished.connect(self._checked)
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self.original.get("name", "Local engine"))
        self.mode = QComboBox()
        self.mode.addItem("Local", "local")
        self.mode.addItem("Remote", "remote")
        self.mode.setCurrentIndex(self.mode.findData(connection_mode(self.original) if self.original else "local"))
        form.addRow("Engine name", self.name)
        form.addRow("Connection", self.mode)
        outer.addLayout(form)
        self.local_note = _label("Local engine: " + LOCAL_BASE_URL)
        outer.addWidget(self.local_note)
        self.remote_fields = QWidget()
        remote_form = QFormLayout(self.remote_fields)
        remote_form.setContentsMargins(0, 0, 0, 0)
        self.base_url = QLineEdit(self.original.get("base_url", LOCAL_BASE_URL))
        self.client_key = QLineEdit()
        self.client_key.setEchoMode(QLineEdit.Password)
        self.client_key.setPlaceholderText("Saved securely · leave blank to keep" if self.original.get("authcfg") else "Client key")
        remote_form.addRow("Engine URL", self.base_url)
        remote_form.addRow("Client key", self.client_key)
        remote_form.addRow(_label("Keys are saved in QGIS’s encrypted credential store."))
        outer.addWidget(self.remote_fields)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        outer.addWidget(self.test_button)
        self.status = _label()
        outer.addWidget(self.status)
        self.engines = _engine_list()
        outer.addWidget(self.engines, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        for field in (self.name, self.base_url, self.client_key):
            field.textChanged.connect(self._edited)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()

    def _mode_changed(self, *args):
        remote = self.mode.currentData() == "remote"
        self.remote_fields.setVisible(remote)
        self.local_note.setVisible(not remote)
        self._edited()

    def _edited(self, *args):
        if self._closed:
            return
        self.checker.cancel()
        self._tested = None
        self.test_button.setEnabled(True)
        self.status.clear()
        self.engines.clear()

    def _draft(self):
        remote = self.mode.currentData() == "remote"
        base_url = normalize_base_url(self.base_url.text()) if remote else LOCAL_BASE_URL
        draft = {"name": self.name.text().strip(), "mode": self.mode.currentData(), "base_url": base_url}
        if not draft["name"]:
            raise ModelError("Give this engine connection a name.")
        key = (self.client_key.text() or None) if remote else None
        if remote:
            if key is not None:
                validate_client_key(key)
            elif (connection_mode(self.original) == "remote" and self.original.get("base_url") == base_url
                  and self.original.get("authcfg")):
                draft["authcfg"] = self.original["authcfg"]
            else:
                raise ModelError("Enter the client key for this engine URL.")
        return draft, key

    def _test(self):
        try:
            draft, key = self._draft()
            self.test_button.setEnabled(False)
            self.engines.clear()
            self.checker.start(draft, client_key=key)
        except (ModelError, ValueError) as error:
            self.status.setText(str(error))
            self.test_button.setEnabled(True)

    def _progress(self, message):
        if not self._closed:
            self.status.setText(message)

    def _checked(self, result):
        if self._closed:
            return
        self._tested = result
        self.test_button.setEnabled(True)
        self.status.setText(check_message(result))
        _show_engines(self.engines, result["engines"])

    def _save(self):
        self.checker.cancel()
        self.test_button.setEnabled(True)
        try:
            draft, key = self._draft()
            # Recheck names before calling the credential store.
            if any(c["id"] != self.original.get("id") and c["name"].casefold() == draft["name"].casefold()
                   for c in self.connection_store.connections()):
                raise ModelError("An engine connection already has this name.")
            value = self.connection_store.save(draft["name"], draft["base_url"], self.original.get("id"),
                                               mode=draft["mode"], client_key=key)
            if self._tested is not None:
                self.connection_store.record_discovery(value, self._tested)
            self.saved_connection = self.connection_store.get(value["id"])
        except (ModelError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.accept()

    def done(self, result):
        self._closed = True
        self.checker.cancel()
        self.client_key.clear()
        super().done(result)


class ApiSettingsPage(QWidget):
    connectionsChanged = pyqtSignal()

    def __init__(self, parent=None, connection_store=None, client=None):
        super().__init__(parent)
        self.connection_store = connection_store or ApiConnectionStore()
        self.checker = ConnectionCheck(self, client)
        self.client = self.checker.client
        self.checker.progress.connect(self.status_progress)
        self.checker.finished.connect(self._checked)
        self._closed = False
        self.editor = None
        self._checking_connection = None
        outer = QVBoxLayout(self)
        selector = QHBoxLayout()
        self.connections = QComboBox()
        self.connections.setAccessibleName("Engine connection")
        self.connections.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.connections.setMinimumContentsLength(16)
        self.connections.currentIndexChanged.connect(self._selection_changed)
        selector.addWidget(self.connections, 1)
        self.menu_button = QToolButton()
        self.menu_button.setText("☰")
        self.menu_button.setToolTip("Engine connections")
        self.menu_button.setAccessibleName("Engine connections menu")
        self.menu_button.setPopupMode(QToolButton.InstantPopup)
        self.menu_button.setMinimumWidth(32)
        menu = QMenu(self.menu_button)
        self.new_action = menu.addAction("New engine…", lambda: self._open_editor())
        self.edit_action = menu.addAction("Edit engine…", self._edit)
        self.delete_action = menu.addAction("Delete engine…", self._remove)
        self.menu_button.setMenu(menu)
        selector.addWidget(self.menu_button)
        outer.addLayout(selector)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        outer.addWidget(self.test_button)
        self.status = _label()
        outer.addWidget(self.status)
        self.use_remote_button = QPushButton("Use Remote with this local URL…")
        self.use_remote_button.clicked.connect(self._use_remote)
        self.use_remote_button.hide()
        outer.addWidget(self.use_remote_button)
        self.engines = _engine_list()
        outer.addWidget(self.engines, 1)
        if not self.connection_store.connections():
            self.connection_store.save("Local engine", LOCAL_BASE_URL, mode="local")
        self.refresh()

    def refresh(self, selected=None):
        selected = selected or self.connections.currentData()
        self.connections.blockSignals(True)
        self.connections.clear()
        for value in self.connection_store.connections():
            self.connections.addItem(value["name"], value["id"])
        index = self.connections.findData(selected)
        self.connections.setCurrentIndex(max(0, index))
        self.connections.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self, *args):
        self.checker.cancel()
        self._checking_connection = None
        value = self.connection_store.get(self.connections.currentData()) or {}
        self.status.setText(value.get("discovery_error") or ("Last checked: " + value["discovery_checked_at"]
                            if value.get("discovery_checked_at") else "Test connection to discover installed engines."))
        profiles = value.get("engine_profiles", [])
        if "engine_profiles" not in value and value.get("capabilities"):
            profiles = [{"state": "ready", "capabilities": value["capabilities"]}]
        if value.get("discovery_error") or value.get("health_status") == "error":
            profiles = [{**p, "error": "The latest connection check failed. Test connection again."} for p in profiles]
        _show_engines(self.engines, profiles)
        self.use_remote_button.hide()
        self._update_buttons()

    def _update_buttons(self):
        saved = bool(self.connections.currentData())
        self.test_button.setEnabled(saved and not self.checker.busy)
        self.edit_action.setEnabled(saved)
        self.delete_action.setEnabled(saved)

    def _open_editor(self, connection=None):
        self.checker.cancel()
        self._update_buttons()
        self.editor = EngineConnectionDialog(self, connection_store=self.connection_store, connection=connection)
        selected = self.connections.currentData()
        try:
            if self.editor.exec_() == QDialog.Accepted:
                selected = self.editor.saved_connection["id"]
                self.connectionsChanged.emit()
        finally:
            self.editor.deleteLater()
            self.editor = None
            if not self._closed:
                self.refresh(selected)

    def _edit(self):
        value = self.connection_store.get(self.connections.currentData())
        if value:
            self._open_editor(value)

    def _use_remote(self):
        value = self.connection_store.get(self.connections.currentData())
        if value:
            self._open_editor({**value, "mode": "remote"})

    def _remove(self):
        value = self.connection_store.get(self.connections.currentData())
        if not value:
            return
        if QMessageBox.question(self, "Delete engine", f'Delete “{value["name"]}” and its saved client key?',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.checker.cancel()
        try:
            self.connection_store.remove(value["id"])
        except ModelError as error:
            self.status.setText(str(error))
            self._update_buttons()
            return
        self.refresh()
        self.connectionsChanged.emit()
        self.status.setText("Engine connection deleted.")

    def status_progress(self, message):
        if not self._closed:
            self.status.setText(message)

    def _test(self):
        value = self.connection_store.get(self.connections.currentData())
        if not value or self.checker.busy:
            return
        self._checking_connection = value
        self.checker.start(value)
        self._update_buttons()

    def _checked(self, result):
        if self._closed or not self._checking_connection:
            return
        value = self._checking_connection
        if self.connection_store.record_discovery(value, result):
            self.status.setText(check_message(result))
            _show_engines(self.engines, result["engines"])
            self.use_remote_button.setVisible(result.get("local_key_required", False))
            self.connectionsChanged.emit()
        else:
            self.status.setText("Connection changed during the test. Test the saved connection again.")
        self._update_buttons()

    def shutdown(self):
        self._closed = True
        self.checker.cancel()
        if self.editor:
            self.editor.reject()


class ApiISOSettingsEditor(QWidget):
    """Model parameters shared by the independent engine implementations."""

    def __init__(self, connection=None, input_unit="m"):
        super().__init__()
        connection = connection or dict(name="ISO calculation", base_url="", capabilities={
            "method": "ISO 9613-2:2024", "atmospheric_absorption": "ISO 9613-1:1993", "max_reflect_order": 5})
        self.connection = connection
        self._settings = default_settings()
        caps = connection["capabilities"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 6, 0)
        self.method_explanation = _label(f"{caps['method']} · {caps['atmospheric_absorption']}\n{connection['name']} · {connection['base_url']}")
        outer.addWidget(self.method_explanation)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.fields = {}
        for key, title, minimum, maximum, decimals, suffix in (
                ("temperature_c", "Air temperature", -20, 50, 1, " °C"),
                ("relative_humidity_percent", "Relative humidity", 10, 100, 1, " %"),
                ("pressure_kpa", "Atmospheric pressure", 50, 120, 3, " kPa"),
                ("ground_factor", "Default ground factor G", 0, 1, 3, ""),
                ("c0_db", "Meteorological coefficient C₀", 0, 5, 2, " dB"),
                ("building_reflection_coefficient", "Default building absorption α", 0, 1, 4, ""),
                ("barrier_reflection_coefficient", "Default wall absorption α", 0, 1, 4, "")):
            field = AbsorptionSpinBox() if key.endswith("reflection_coefficient") else DecimalInput()
            field.setRange(minimum, maximum)
            field.setDecimals(decimals)
            field.setSuffix(suffix)
            self.fields[key] = field
            form.addRow(title, field)
        self.fields["reflection_order"] = IntegerInput()
        # Do not clamp a saved order when switching between API implementations.
        self.fields["reflection_order"].setRange(0, 5)
        self.fields["reflection_order"].setToolTip(f"This engine advertises reflection orders through {caps['max_reflect_order']}.")
        self.fields["reflection_order"].valueChanged.connect(self._compatibility_changed)
        form.addRow("Maximum reflection order", self.fields["reflection_order"])
        self.meteorology = QComboBox()
        self.meteorology.addItem("Favourable / downwind", "downwind")
        self.meteorology.addItem("Long-term average (uses C₀)", "long_term")
        self.meteorology.currentIndexChanged.connect(self._meteorology_changed)
        form.addRow("Meteorology", self.meteorology)
        self.ground_method = QComboBox()
        self.ground_method.addItem("General (octave bands)", "general")
        self.ground_method.addItem("Mean path height (A-weighted)", "mean_path_height")
        self.ground_method.setToolTip("Select the ground method for this assessment. Mean path height assumes non-pure-tone sound and predominantly porous ground. Terrain is retained with either method.")
        form.addRow("Ground method", self.ground_method)
        outer.addLayout(form)
        self.optional_fields = {}
        pruning = QGroupBox("Pruning")
        pruning_layout = QVBoxLayout(pruning)
        pruning_form = QFormLayout()
        self.pruning_check = QCheckBox("Prune paths that will not raise the overall level")
        self.pruning_basis = QComboBox()
        self.pruning_basis.addItem("dBA", "dba")
        self.pruning_basis.addItem("dBC", "dbc")
        self.pruning_basis.setAccessibleName("Pruning basis")
        self.pruning_check.toggled.connect(self.pruning_basis.setEnabled)
        pruning_form.addRow(self.pruning_check)
        pruning_form.addRow("Based on", self.pruning_basis)
        pruning_layout.addLayout(pruning_form)
        self.pruning_note = _label(
            "Pruning speeds up the calculation. As it progresses, sound paths that are not expected to "
            "raise the chosen overall level (dBA or dBC) at any receiver are abandoned. Thresholds are set "
            "automatically from the quietest receiver and the number of sources. Overall levels stay very "
            "close to an unpruned calculation, but individual octave bands, especially quiet ones, may lose "
            "accuracy. Switch pruning off when octave-band results matter.")
        pruning_layout.addWidget(self.pruning_note)
        outer.addWidget(pruning)
        self.integration_toggle = QToolButton()
        self.integration_toggle.setText("Source integration")
        self.integration_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.integration_toggle.setArrowType(Qt.RightArrow)
        self.integration_toggle.setCheckable(True)
        outer.addWidget(self.integration_toggle)
        integration = QWidget()
        integration.hide()
        self.integration_toggle.toggled.connect(integration.setVisible)
        self.integration_toggle.toggled.connect(lambda expanded: self.integration_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow))
        integration_form = QFormLayout(integration)
        self.numerical_fields = {}
        for key, title in (("area_raster_factor", "Extent / distance factor"),
                           ("area_relative_tolerance", "Relative energy tolerance"),
                           ("area_absolute_tolerance", "Absolute energy tolerance"),
                           ("area_max_extent_m", "Maximum cell extent (m)")):
            field = QLineEdit()
            if key == "area_max_extent_m":
                field.setPlaceholderText("Unlimited")
                self.optional_fields[key] = field
            else:
                self.numerical_fields[key] = field
            integration_form.addRow(title, field)
        for key, title, maximum in (("area_max_depth", "Maximum refinement depth", 24),):
            field = IntegerInput()
            field.setRange(1, maximum)
            self.fields[key] = field
            integration_form.addRow(title, field)
        outer.addWidget(integration)
        self.compatibility = _label()
        outer.addWidget(self.compatibility)
        outer.addStretch()
        self.set_settings(self._settings)

    def _compatibility_changed(self, *args):
        problems = []
        maximum = self.connection["capabilities"]["max_reflect_order"]
        if self.fields["reflection_order"].value() > maximum:
            problems.append(f"Saved reflection order exceeds this engine's maximum of {maximum}.")
        self.compatibility.setText("\n".join(problems))
        self.compatibility.setVisible(bool(problems))

    def _meteorology_changed(self, *args):
        self.fields["c0_db"].setEnabled(self.meteorology.currentData() == "long_term")

    def set_settings(self, value):
        self._settings = validate_settings(value)
        value = self._settings
        for key, field in self.fields.items():
            if isinstance(field, AbsorptionSpinBox):
                field.set_reflection(value[key])
            else:
                field.setValue(value[key])
        self.meteorology.setCurrentIndex(self.meteorology.findData(value["meteorology"]))
        self.ground_method.setCurrentIndex(self.ground_method.findData(value["ground_method"]))
        for key, field in {**self.numerical_fields, **self.optional_fields}.items():
            field.setText("" if value[key] is None else str(value[key]))
        strategy = value["pruning_strategy"]
        self.pruning_check.setChecked(strategy is not None)
        self.pruning_basis.setCurrentIndex(self.pruning_basis.findData(
            strategy or value["pruning_values"]["pruning_strategy"]))
        self.pruning_basis.setEnabled(strategy is not None)
        self._meteorology_changed()
        self._compatibility_changed()

    def settings(self):
        value = dict(self._settings)
        value["pruning_values"] = dict(value["pruning_values"])
        for key, field in self.fields.items():
            value[key] = field.reflection() if isinstance(field, AbsorptionSpinBox) else field.value()
        for key, field in {**self.numerical_fields, **self.optional_fields}.items():
            raw = field.text().strip()
            try:
                value[key] = None if not raw and key in self.optional_fields else float(raw)
            except ValueError as error:
                raise ModelError(f"{key}: enter a finite number.") from error
        basis = self.pruning_basis.currentData()
        value["pruning_values"]["pruning_strategy"] = basis
        value["pruning_strategy"] = basis if self.pruning_check.isChecked() else None
        value.update(meteorology=self.meteorology.currentData(), ground_method=self.ground_method.currentData())
        return validate_settings(value)

    def set_input_unit(self, unit):
        pass  # All source-integration lengths are explicitly labelled metres.
