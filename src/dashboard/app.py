"""The a7a-fedora dashboard (PySide6): the pipeline with each stage's status and log, the registry
of images and their tests, the live serial console, and a button per stage that shows the exact
commands (a dry run) before running it. Flash asks for the device and the typed confirmation
like the CLI does; nothing runs from here that the CLI would refuse.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from src.config import Config
from src.registry import StageRegistry

STAGES = ["sources", "kernel", "rootfs", "image", "emulate", "flash", "serial", "update"]


class StageRunner(QThread):
    """Runs one stage function (taking an on_line callback) off the GUI thread."""
    line = Signal(str)
    finished_with = Signal(dict)

    def __init__(self, fn: Callable[[Callable[[str], None]], Dict]):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn(self.line.emit)
        except Exception as e:  # noqa: BLE001
            result = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
        self.finished_with.emit(result or {})


class DashboardWindow(QMainWindow):
    def __init__(self, config: Optional[Config] = None):
        super().__init__()
        self.config = config or Config.load()
        self.setWindowTitle("a7a-fedora")
        self.resize(1200, 820)
        self.runner: Optional[StageRunner] = None
        self._build()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)

    # -- layout ------------------------------------------------------------------------
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        title = QLabel("a7a-fedora: Fedora 44 KDE on the Radxa Cubie A7A")
        title.setFont(QFont("Sans", 15, QFont.Weight.Bold))
        root.addWidget(title)
        self.summary = QLabel("")
        root.addWidget(self.summary)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # Pipeline
        pipe = QWidget(); pl = QVBoxLayout(pipe)
        self.stage_table = QTableWidget(len(STAGES), 4)
        self.stage_table.setHorizontalHeaderLabels(["stage", "status", "when", "notes"])
        self.stage_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.stage_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stage_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i, name in enumerate(STAGES):
            self.stage_table.setItem(i, 0, QTableWidgetItem(name))
        pl.addWidget(self.stage_table)
        buttons = QHBoxLayout()
        self.show_btn = QPushButton("Show commands (dry run)")
        self.show_btn.clicked.connect(self._show_commands)
        self.run_btn = QPushButton("Run selected stage")
        self.run_btn.clicked.connect(self._run_selected)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        for b in (self.show_btn, self.run_btn, self.refresh_btn):
            buttons.addWidget(b)
        pl.addLayout(buttons)
        self.stage_log = QPlainTextEdit(); self.stage_log.setReadOnly(True); self.stage_log.setFont(QFont("Monospace", 9))
        self.stage_log.setMaximumBlockCount(4000)
        pl.addWidget(self.stage_log)
        self.tabs.addTab(pipe, "Pipeline")

        # Images
        imgs = QWidget(); il = QVBoxLayout(imgs)
        self.images_table = QTableWidget(0, 6)
        self.images_table.setHorizontalHeaderLabels(["image", "kernel", "size", "tests", "flashed", "sha256"])
        self.images_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.images_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        il.addWidget(self.images_table)
        self.tabs.addTab(imgs, "Images")

        # Serial console
        ser = QWidget(); sl = QVBoxLayout(ser)
        top = QHBoxLayout()
        self.serial_label = QLabel(f"port: {self.config.serial_port or 'not configured (a7a-fedora.json: serial_port)'}")
        top.addWidget(self.serial_label)
        self.fallback_box = QCheckBox("type 2 at the menu (the image's other kernel)")
        top.addWidget(self.fallback_box)
        self.watch_btn = QPushButton("Watch the board's boot")
        self.watch_btn.clicked.connect(lambda: self._start("serial", fallback=self.fallback_box.isChecked()))
        top.addWidget(self.watch_btn)
        sl.addLayout(top)
        self.serial_view = QPlainTextEdit(); self.serial_view.setReadOnly(True); self.serial_view.setFont(QFont("Monospace", 9))
        self.serial_view.setMaximumBlockCount(6000)
        sl.addWidget(self.serial_view)
        self.tabs.addTab(ser, "Serial console")

        # Logs
        logs = QWidget(); ll = QVBoxLayout(logs)
        row = QHBoxLayout()
        self.log_pick = QComboBox(); self.log_pick.addItems(STAGES)
        self.log_pick.currentTextChanged.connect(self._load_log)
        row.addWidget(QLabel("newest log of")); row.addWidget(self.log_pick); row.addStretch()
        ll.addLayout(row)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); self.log_view.setFont(QFont("Monospace", 9))
        ll.addWidget(self.log_view)
        self.tabs.addTab(logs, "Logs")
        self._serial_log_pos: Dict[str, int] = {}

    # -- data ----------------------------------------------------------------------------
    def registry(self) -> StageRegistry:
        return StageRegistry(self.config.registry_path)

    def refresh(self):
        reg = self.registry()
        s = reg.summary()
        self.summary.setText(f"passed {s.get('passed', 0)}  failed {s.get('failed', 0)}  dry-run {s.get('dry_run', 0)}   registry {reg.path}")
        for i, name in enumerate(STAGES):
            e = reg.get_stage(name) or {}
            status = e.get("status", "not run")
            it = QTableWidgetItem(status)
            it.setForeground(Qt.GlobalColor.darkGreen if status == "passed" else Qt.GlobalColor.red if status in ("failed", "refused") else Qt.GlobalColor.darkYellow)
            self.stage_table.setItem(i, 1, it)
            self.stage_table.setItem(i, 2, QTableWidgetItem(str(e.get("timestamp", ""))[:19].replace("T", " ")))
            self.stage_table.setItem(i, 3, QTableWidgetItem(str(e.get("notes", ""))[:200]))
        images = reg.images()
        self.images_table.setRowCount(len(images))
        for r, (name, img) in enumerate(images.items()):
            tests = ", ".join(f"{t['mode']}:{t['verdict']}" for t in img.get("tests", [])) or "not tested"
            cells = [name, str(img.get("kernel_release", "")), str(img.get("size", "")), tests, str(len(img.get("flashes", []))), str(img.get("sha256", ""))[:16]]
            for c, text in enumerate(cells):
                self.images_table.setItem(r, c, QTableWidgetItem(text))
        self._load_log(self.log_pick.currentText())

    def _tick(self):
        self._tail_serial_log()

    def _tail_serial_log(self):
        logs = sorted(self.config.logs_dir.glob("serial-*.log")) if self.config.logs_dir.exists() else []
        if not logs:
            return
        p = logs[-1]
        pos = self._serial_log_pos.get(str(p), 0)
        try:
            with open(p, errors="replace") as f:
                f.seek(pos)
                new = f.read()
                self._serial_log_pos[str(p)] = f.tell()
        except OSError:
            return
        if new:
            if pos == 0:
                self.serial_view.appendPlainText(f"--- {p.name} ---")
            self.serial_view.appendPlainText(new.rstrip("\n"))

    def _load_log(self, stage: str):
        logs = sorted(self.config.logs_dir.glob(f"{stage}-*.log")) if self.config.logs_dir.exists() else []
        if not logs:
            self.log_view.setPlainText(f"(no {stage} log under {self.config.logs_dir})")
            return
        with open(logs[-1], errors="replace") as f:
            tail = deque(f, maxlen=400)
        self.log_view.setPlainText(f"{logs[-1]}\n\n" + "".join(tail))

    # -- actions ---------------------------------------------------------------------------
    def selected_stage(self) -> str:
        row = self.stage_table.currentRow()
        return STAGES[row if row >= 0 else 0]

    def commands_for(self, stage: str, **options) -> str:
        """The dry run of a stage, rendered: what the button would run."""
        import importlib
        mod = importlib.import_module(f"src.stages.{stage}")
        r = mod.run(self.config, self.registry(), dry_run=True, **options)
        lines = [f"{stage}: {r.get('status')}" + (f" - {r.get('reason')}" if r.get("reason") else "")]
        for w in r.get("warnings", []) or []:
            lines.append("warning: " + str(w))
        for c in r.get("commands", []) or []:
            lines.append("  " + str(c))
        if r.get("plan"):
            lines.append(json.dumps(r["plan"], indent=1, default=str))
        return "\n".join(lines)

    def _show_commands(self):
        stage = self.selected_stage()
        opts = self._options_for(stage, ask=False)
        self.stage_log.appendPlainText(self.commands_for(stage, **opts))

    def _options_for(self, stage: str, ask: bool) -> Dict:
        if stage == "flash":
            from src.stages.flash import candidates
            cands = candidates(self.config)
            if not ask:
                return {"device": cands[0]["path"] if cands else None}
            if not cands:
                QMessageBox.warning(self, "flash", "no removable disk found")
                return {"device": None}
            labels = [f"{c['path']}  {c['model']}  {c['size'] / 1e9:.1f} GB" + ("  MOUNTED" if c["mounted"] else "") for c in cands]
            pick, ok = QInputDialog.getItem(self, "flash", "card to write (it will be backed up first):", labels, 0, False)
            if not ok:
                return {"device": None}
            dev = cands[labels.index(pick)]
            typed, ok = QInputDialog.getText(self, "flash", f"Type {dev['name']} to confirm writing {dev['path']} ({dev['model']}):")
            return {"device": dev["path"], "confirm": typed if ok else None}
        if stage == "emulate":
            return {"mode": "kernel"}
        if stage == "serial":
            return {"fallback": self.fallback_box.isChecked()}
        return {}

    def _run_selected(self):
        stage = self.selected_stage()
        opts = self._options_for(stage, ask=True)
        preview = self.commands_for(stage, **opts)
        if QMessageBox.question(self, f"run {stage}?", preview[:3000] + "\n\nRun it?") != QMessageBox.StandardButton.Yes:
            return
        self._start(stage, **opts)

    def _start(self, stage: str, **opts):
        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "busy", "a stage is already running")
            return
        import importlib
        mod = importlib.import_module(f"src.stages.{stage}")
        cfg, reg = self.config, self.registry()
        self.stage_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {stage} starting")
        self.runner = StageRunner(lambda on_line: mod.run(cfg, reg, on_line=on_line, **opts))
        self.runner.line.connect(lambda l: (self.serial_view if stage == "serial" else self.stage_log).appendPlainText(l))
        self.runner.finished_with.connect(self._done)
        self.runner.start()

    def _done(self, result: Dict):
        self.stage_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {result.get('stage', '?')}: {result.get('status')}"
                                       + (f" - {result.get('reason')}" if result.get("reason") else "")
                                       + (f"\nnext: {result['next_step']}" if result.get("next_step") else ""))
        self.refresh()


def main(config: Optional[Config] = None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    w = DashboardWindow(config)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
