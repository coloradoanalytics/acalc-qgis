# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Draw an import box and generate terrain without blocking the QGIS event loop."""

from qgis.PyQt.QtCore import QEventLoop, QThread, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)
from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsExtentWidget

from .raster_terrain import GenerationCancelled, generate, validate_options
from .schema import ModelError
from .storage import require_name
from .units import LengthSpinBox, elevation_unit


class TerrainWorker(QThread):
    progress = pyqtSignal(str)

    def __init__(self, parent, record, wkt, bounds, tolerance):
        super().__init__(parent)
        self.arguments = (dict(record), wkt, bounds, tolerance)
        self.mesh = None
        self.error = None

    def report(self, message):
        self.progress.emit(message)
        return not self.isInterruptionRequested()

    def run(self):
        try:
            self.mesh = generate(*self.arguments, progress=self.report)
        except GenerationCancelled:
            pass
        except Exception as error:
            self.error = str(error)


class RasterTerrainDialog(QDialog):
    def __init__(self, parent, store, canvas, record):
        super().__init__(parent)
        self.setWindowTitle("Generate terrain from raster")
        self.resize(650, 570)
        self.store, self.record = store, record
        self.wkt = store.metadata()['crs_wkt']
        self.worker = None
        self.mesh = None
        self.cancelling = False
        self.canvas = canvas
        self._last_previous_tool = canvas.mapTool()
        self._draw_tool = None
        self._drawing_previous_tool = None
        self._closing = False
        canvas.mapToolSet.connect(self.map_tool_changed)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        source = QLabel(record['path'])
        source.setTextFormat(Qt.PlainText)
        source.setWordWrap(True)
        body.addWidget(source)
        self.inputs = QWidget()
        form = QFormLayout(self.inputs)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        base = record['name'] + ' terrain'
        names = {layer['name'] for layer in store.layers() + store.layers(archived=True)}
        name, number = base, 2
        while name in names:
            name = f'{base} {number}'
            number += 1
        self.name = QLineEdit(name)
        form.addRow("New terrain layer", self.name)
        self.extent = QgsExtentWidget(self, QgsExtentWidget.CondensedStyle)
        self.extent.setOutputCrs(QgsCoordinateReferenceSystem(self.wkt))
        self.extent.setCurrentExtent(canvas.extent(), canvas.mapSettings().destinationCrs())
        self.extent.setMapCanvas(canvas)
        self.extent.clear()
        self.extent.toggleDialogVisibility.connect(self.set_drawing_visibility)
        form.addRow("Terrain area", self.extent)
        self.draw_button = QPushButton("Draw box on map")
        self.draw_button.clicked.connect(self.extent.setOutputExtentFromDrawOnCanvas)
        form.addRow("", self.draw_button)
        self.tolerance = LengthSpinBox(elevation_unit(store))
        self.tolerance.setRange(0.001, 10000)
        self.tolerance.setValue(0.5)
        self.tolerance.setToolTip("Maximum vertical difference at checked raster pixel centres and box-boundary samples. "
                                 "This controls terrain simplification, not raster survey or sound-level accuracy.")
        form.addRow("Maximum elevation deviation", self.tolerance)
        body.addWidget(self.inputs)
        hint = QLabel("Draw a box, or choose the current map extent from the area menu. "
                      "Use bare-earth elevations in the model's vertical datum. "
                      "The new layer opens under Generated terrain; select it in Variants to use it in calculations.")
        hint.setWordWrap(True)
        body.addWidget(hint)
        body.addStretch()
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Generate terrain")
        self.buttons.accepted.connect(self.start_generation)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.extent.isValid())
        self.extent.validationChanged.connect(self.update_generate_enabled)

    def update_generate_enabled(self, *args):
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.extent.isValid() and self.worker is None)

    def exec(self):
        """Wait for accept/reject, not for the temporary hide used by drawing.

        QDialog.exec() returns as soon as QgsExtentWidget hides the dialog. The
        controller would then delete it and its still-active map tool. A local
        event loop keeps the existing synchronous controller contract, while
        only finished/destroyed ends the wait. A hidden window is not modal.
        """
        loop = QEventLoop()
        result = QDialog.Rejected

        def finished(value):
            nonlocal result
            result = value
            loop.quit()

        self.finished.connect(finished)
        self.destroyed.connect(loop.quit)
        self.setWindowModality(Qt.ApplicationModal)
        self.show()
        try:
            loop.exec()
        finally:
            try:
                self.finished.disconnect(finished)
                self.destroyed.disconnect(loop.quit)
            except (RuntimeError, TypeError):
                pass  # The parent QGIS window may have destroyed the dialog.
        return result

    exec_ = exec

    def map_tool_changed(self, new, previous):
        self._last_previous_tool = previous

    def set_drawing_visibility(self, visible):
        if self._closing:
            return
        if not visible:
            # mapToolSet precedes toggleDialogVisibility, including when drawing
            # starts from the extent widget's own menu rather than our button.
            self._draw_tool = self.canvas.mapTool()
            self._drawing_previous_tool = self._last_previous_tool
            self.hide()
            self.canvas.window().raise_()
            self.canvas.window().activateWindow()
            self.canvas.setFocus(Qt.OtherFocusReason)
        else:
            self._draw_tool = self._drawing_previous_tool = None
            self.show()
            self.raise_()
            self.activateWindow()

    def done(self, result):
        self._closing = True
        # Closing while the box tool is active must restore the previous tool
        # before detaching/deleting QgsExtentWidget's native canvas pointer.
        try:
            if self._draw_tool is not None and self.canvas.mapTool() is self._draw_tool:
                if self._drawing_previous_tool is not None:
                    try:
                        self.canvas.setMapTool(self._drawing_previous_tool)
                    except RuntimeError:  # A previous tool may have been deleted.
                        self.canvas.unsetMapTool(self._draw_tool)
                else:
                    self.canvas.unsetMapTool(self._draw_tool)
            self.canvas.mapToolSet.disconnect(self.map_tool_changed)
        except (RuntimeError, TypeError):
            pass
        self._draw_tool = self._drawing_previous_tool = None
        self.extent.setMapCanvas(None)
        super().done(result)

    def bounds(self):
        if not self.extent.isValid():
            raise ModelError("Draw a terrain box or choose an area from the area menu.")
        rect = self.extent.outputExtent()
        bounds = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        validate_options(bounds, self.tolerance.value())
        return bounds

    def start_generation(self):
        if self.worker is not None:
            return
        try:
            require_name(self.name.text())
            bounds = self.bounds()
        except ModelError as error:
            self.status.setText(str(error))
            return
        self.inputs.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self.status.setText("Reading elevation raster…")
        self.progress.show()
        self.cancelling = False
        self.worker = TerrainWorker(self, self.record, self.wkt, bounds, self.tolerance.value())
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self.generation_finished)
        self.worker.start()

    def generation_finished(self):
        worker, self.worker = self.worker, None
        self.progress.hide()
        self.inputs.setEnabled(True)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)
        worker.deleteLater()
        if self.cancelling:
            super().reject()
        elif worker.error:
            self.status.setText(worker.error)
        elif worker.mesh is not None:
            self.mesh = worker.mesh
            self.accept()

    def reject(self):
        if self.worker is not None:
            self.cancelling = True
            self.worker.requestInterruption()
            self.status.setText("Cancelling terrain generation…")
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
