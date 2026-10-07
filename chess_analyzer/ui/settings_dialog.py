"""Settings: the Lichess token and engine options."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config
from .style import set_role


class SettingsDialog(QDialog):
    def __init__(self, cfg: config.AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._cfg = cfg

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        token_row = QHBoxLayout()
        self._token = QLineEdit(cfg.lichess_token)
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._token.setPlaceholderText("lip_…")
        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self._token.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        token_row.addWidget(self._token, 1)
        token_row.addWidget(show)
        form.addRow("Lichess API token", token_row)
        hint = QLabel(
            "Needed for the Opening Explorer and Opening Insights. Create one without any "
            "scopes at <a href='https://lichess.org/account/oauth/token'>lichess.org/account/"
            "oauth/token</a>. It is stored in plain text in your data directory."
        )
        hint.setOpenExternalLinks(True)
        hint.setWordWrap(True)
        set_role(hint, "muted")
        form.addRow("", hint)

        engine_row = QHBoxLayout()
        self._engine_path = QLineEdit(cfg.engine_path)
        self._engine_path.setPlaceholderText("leave empty to use the downloaded Stockfish")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        engine_row.addWidget(self._engine_path, 1)
        engine_row.addWidget(browse)
        form.addRow("Engine binary", engine_row)

        self._threads = QSpinBox()
        self._threads.setRange(0, 128)
        self._threads.setSpecialValueText("auto (half the CPUs)")
        self._threads.setValue(cfg.engine_threads)
        form.addRow("Engine threads", self._threads)

        self._hash = QSpinBox()
        self._hash.setRange(16, 16384)
        self._hash.setSingleStep(64)
        self._hash.setSuffix(" MB")
        self._hash.setValue(cfg.engine_hash_mb)
        form.addRow("Engine hash", self._hash)

        note = QLabel("Engine settings take effect the next time the app starts.")
        set_role(note, "faint")
        form.addRow("", note)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a UCI engine", "", "Executables (*.exe);;All files (*)"
        )
        if path:
            self._engine_path.setText(path)

    def apply(self) -> dict[str, bool]:
        """Write the fields into the config. Returns which groups changed."""
        cfg = self._cfg
        token = self._token.text().strip()
        engine = (
            self._engine_path.text().strip(),
            self._threads.value(),
            self._hash.value(),
        )
        changed = {
            "token": token != cfg.lichess_token,
            "engine": engine != (cfg.engine_path, cfg.engine_threads, cfg.engine_hash_mb),
        }
        cfg.lichess_token = token
        cfg.engine_path, cfg.engine_threads, cfg.engine_hash_mb = engine
        return changed
