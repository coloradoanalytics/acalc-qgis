# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Horizontal tables with spreadsheet copy/paste, including during cell editing."""
import csv
import io

from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import QApplication, QStyledItemDelegate, QTableWidget, QTableWidgetItem


class ClipboardDelegate(QStyledItemDelegate):
    def __init__(self, table):
        super().__init__(table)
        self.table = table

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.matches(QKeySequence.Paste):
            text = QApplication.clipboard().text()
            if any(separator in text for separator in ('\t', '\n', '\r')):
                self.closeEditor.emit(editor, QStyledItemDelegate.NoHint)
                self.table.paste_cells()
                return True
        return super().eventFilter(editor, event)


class SpectrumTable(QTableWidget):
    """Copy selected cells as TSV; paste rows or a vertical list of band values."""
    def __init__(self, rows, columns, parent=None):
        super().__init__(rows, columns, parent)
        self.setItemDelegate(ClipboardDelegate(self))
        self.setTabKeyNavigation(True)
        self.paste_band = None
        self.copy_rows = None
        self.copy_value = None

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self.copy_cells()
        elif event.matches(QKeySequence.Paste):
            self.paste_cells()
        else:
            super().keyPressEvent(event)

    def copy_cells(self):
        indices = [i for i in self.selectedIndexes() if self.copy_rows is None or i.row() in self.copy_rows]
        if not indices:
            return
        rows = range(min(i.row() for i in indices), max(i.row() for i in indices) + 1)
        columns = range(min(i.column() for i in indices), max(i.column() for i in indices) + 1)
        selected = {(i.row(), i.column()) for i in indices}
        QApplication.clipboard().setText('\n'.join('\t'.join(
            (self.copy_value(r, c) if self.copy_value else self.item(r, c).text())
            if (r, c) in selected and self.item(r, c) else ''
            for c in columns) for r in rows))

    def paste_cells(self):
        if self.editTriggers() == QTableWidget.NoEditTriggers:
            return
        text = QApplication.clipboard().text().rstrip('\r\n')
        cells = list(csv.reader(io.StringIO(text), delimiter='\t'))
        if not cells:
            return
        if len(cells) > 1 and all(len(row) == 1 for row in cells):
            cells = [[row[0] for row in cells]]
        row, column = max(0, self.currentRow()), max(0, self.currentColumn())
        # Reject partial/out-of-bounds pastes rather than silently truncate data.
        if len(cells) != 1 or row >= self.rowCount() or column + len(cells[0]) > self.columnCount():
            return
        for offset, value in enumerate(cells[0]):
            if self.paste_band is not None:
                self.paste_band(row, column + offset, value.strip())
                continue
            item = self.item(row, column + offset)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, column + offset, item)
            if item.flags() & Qt.ItemIsEditable:
                item.setText(value.strip())
