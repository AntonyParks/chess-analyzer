"""Game Review: the flagged moves of the analysed game, with a better alternative each."""

from __future__ import annotations

import html

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from .. import theme
from ..game_analysis import ReviewEntry
from .style import set_role

HINT = (
    "Run <b>Analyze game</b> to evaluate every position of the loaded game. Flagged moves "
    "appear here with a better alternative; the move list and the graph fill in as it runs."
)


class ReviewPanel(QWidget):
    plyClicked = Signal(int)
    analyzeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self._analyze_btn = QPushButton("Analyze game")
        self._analyze_btn.setProperty("accent", True)
        self._analyze_btn.clicked.connect(self.analyzeRequested.emit)
        self._summary = QLabel("")
        set_role(self._summary, "muted")
        top.addWidget(self._analyze_btn)
        top.addWidget(self._summary, 1)
        layout.addLayout(top)

        self._browser = QTextBrowser()
        self._browser.setOpenLinks(False)
        self._browser.setOpenExternalLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor)
        layout.addWidget(self._browser, 1)
        self.set_entries(None)

    def set_analyzing(self, running: bool) -> None:
        self._analyze_btn.setEnabled(not running)

    def set_summary(self, text: str) -> None:
        self._summary.setText(text)

    def set_entries(self, entries: list[ReviewEntry] | None) -> None:
        if entries is None:
            self._browser.setHtml(f"<p style='color:{theme.TEXT_MUTED}'>{HINT}</p>")
            self._summary.setText("")
            return
        if not entries:
            self._browser.setHtml(
                f"<p style='color:{theme.GOOD}'>No inaccuracies, mistakes or blunders found.</p>"
            )
            self._summary.setText("Game analysed")
            return
        self._summary.setText(f"{len(entries)} move{'' if len(entries) == 1 else 's'} flagged")
        cards = "".join(self._card(e) for e in entries)
        self._browser.setHtml(
            f"<style>a {{ color: {theme.HIGHLIGHT_OTHER}; text-decoration: none; }}</style>{cards}"
        )

    @staticmethod
    def _card(entry: ReviewEntry) -> str:
        colour = theme.MARKER_COLORS.get(entry.marker, theme.TEXT)
        better = (
            f"<div style='color:{theme.GOOD}; font-weight:bold'>"
            f"Better: {html.escape(entry.better_san)}</div>"
            if entry.better_san
            else ""
        )
        explanation = (
            f"<div style='color:{theme.TEXT}'>{html.escape(entry.explanation)}</div>"
            if entry.explanation
            else ""
        )
        return (
            f"<table width='100%' cellpadding='6' "
            f"style='margin-bottom:6px; background:{theme.CARD}'>"
            f"<tr><td style='border-left: 4px solid {colour}'>"
            f"<span style='color:{colour}; font-weight:bold; font-size:11pt'>"
            f"{html.escape(entry.label)}</span>"
            f"<span style='color:{theme.TEXT_DIM}'>&nbsp;&nbsp;−{entry.cp_drop} cp, "
            f"−{entry.win_drop * 50:.0f}% win chance</span>"
            f"&nbsp;&nbsp;<a href='ply:{entry.ply}'>Go to move →</a>"
            f"{better}{explanation}</td></tr></table>"
        )

    def _on_anchor(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("ply:") and text[4:].isdigit():
            self.plyClicked.emit(int(text[4:]))
