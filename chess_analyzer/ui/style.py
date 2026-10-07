"""Dark Fusion look: palette, fonts and the few stylesheet rules Fusion does not cover."""

from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from .. import theme

ACCENT = theme.SELECTED
ACCENT_HOVER = theme.SELECTED_ACTIVE
BORDER = "#1F1F1F"
SEPARATOR = "#4A4A4A"

STYLESHEET = f"""
QMainWindow, QDialog {{ background: {theme.BG}; }}
QToolBar {{ background: {theme.PANEL}; border: none; spacing: 4px; padding: 3px 6px; }}
QToolBar::separator {{ width: 1px; background: {SEPARATOR}; margin: 4px 6px; }}
QToolButton {{ padding: 4px 8px; border-radius: 4px; }}
QToolButton:hover {{ background: {theme.BUTTON}; }}
QToolButton:checked {{ background: {ACCENT}; color: {theme.WHITE}; }}
QStatusBar {{ background: {theme.STATUS_BG}; color: {theme.TEXT}; }}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background: {theme.PANEL}; }}
QMenuBar::item:selected {{ background: {ACCENT}; }}
QMenu {{ background: {theme.PANEL}; border: 1px solid {SEPARATOR}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {theme.BG}; }}
QTabBar::tab {{ background: {theme.PANEL}; padding: 6px 14px;
    border: 1px solid {BORDER}; border-bottom: none; margin-right: 1px; }}
QTabBar::tab:selected {{ background: {theme.BG}; color: {theme.TEXT_BRIGHT}; }}
QTabBar::tab:hover:!selected {{ background: {theme.BUTTON}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 4px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px;
    color: {theme.TEXT_MUTED}; }}
QSplitter::handle {{ background: {theme.BG}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:hover {{ background: {theme.BUTTON}; }}
QTableView, QTreeView, QListView, QTextBrowser, QPlainTextEdit {{
    background: {theme.BG}; alternate-background-color: #303030;
    border: 1px solid {BORDER}; gridline-color: {BORDER};
    selection-background-color: {ACCENT}; }}
QHeaderView::section {{ background: {theme.PANEL}; color: {theme.TEXT}; padding: 4px 6px;
    border: none; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; }}
QTableView::item {{ padding: 2px 6px; }}
QScrollBar:vertical {{ background: {theme.BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {theme.BUTTON}; min-height: 24px; border-radius: 5px; }}
QScrollBar::handle:vertical:hover {{ background: {theme.BUTTON_ACTIVE}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {theme.BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {theme.BUTTON}; min-width: 24px; border-radius: 5px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 3px 6px;
    border: 1px solid {SEPARATOR}; border-radius: 3px; background: {theme.FIELD};
    selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT_HOVER}; }}
QPushButton {{ padding: 5px 12px; border-radius: 4px; background: {theme.BUTTON}; }}
QPushButton:hover {{ background: {theme.BUTTON_ACTIVE}; }}
QPushButton:pressed {{ background: {ACCENT}; }}
QPushButton:disabled {{ color: {theme.TEXT_FAINT}; background: {theme.PANEL}; }}
QPushButton[accent="true"] {{ background: {ACCENT}; color: {theme.WHITE}; font-weight: bold; }}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; }}
QToolTip {{ background: {theme.PANEL}; color: {theme.TEXT_BRIGHT}; border: 1px solid {SEPARATOR}; }}
QLabel[role="muted"] {{ color: {theme.TEXT_MUTED}; }}
QLabel[role="faint"] {{ color: {theme.TEXT_FAINT}; }}
QLabel[role="good"] {{ color: {theme.GOOD}; }}
QLabel[role="warn"] {{ color: {theme.WARN}; }}
QLabel[role="bad"] {{ color: {theme.BAD}; }}
QLabel[role="title"] {{ color: {theme.TEXT_BRIGHT}; font-weight: bold; }}
"""


def apply(app: QApplication) -> None:
    """Fusion style with a dark palette and the stylesheet above."""
    app.setStyle("Fusion")
    palette = QPalette()
    colour_roles = {
        QPalette.ColorRole.Window: theme.BG,
        QPalette.ColorRole.WindowText: theme.TEXT,
        QPalette.ColorRole.Base: theme.FIELD,
        QPalette.ColorRole.AlternateBase: theme.PANEL,
        QPalette.ColorRole.ToolTipBase: theme.PANEL,
        QPalette.ColorRole.ToolTipText: theme.TEXT_BRIGHT,
        QPalette.ColorRole.Text: theme.TEXT_BRIGHT,
        QPalette.ColorRole.PlaceholderText: theme.TEXT_FAINT,
        QPalette.ColorRole.Button: theme.BUTTON,
        QPalette.ColorRole.ButtonText: theme.TEXT_BRIGHT,
        QPalette.ColorRole.BrightText: theme.WHITE,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: theme.WHITE,
        QPalette.ColorRole.Link: theme.HIGHLIGHT_OTHER,
        QPalette.ColorRole.Light: theme.BUTTON_ACTIVE,
        QPalette.ColorRole.Midlight: theme.BUTTON,
        QPalette.ColorRole.Mid: theme.PANEL,
        QPalette.ColorRole.Dark: BORDER,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, colour in colour_roles.items():
        palette.setColor(role, QColor(colour))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.TEXT_FAINT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 10))
    elif sys.platform == "darwin":
        app.setFont(QFont("Helvetica Neue", 12))


def set_role(widget, role: str) -> None:
    """Tag a widget for the ``[role=...]`` stylesheet rules and restyle it."""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
