import html
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QDoubleSpinBox, QFileDialog,
    QTextEdit, QStatusBar, QProgressBar, QSplitter, QSizePolicy,
    QMessageBox,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QUrl
from PyQt6.QtGui import QFont, QPixmap, QDesktopServices

import apitest as bsky


# ── Background worker ──────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    log           = pyqtSignal(str)
    error         = pyqtSignal(str)
    done          = pyqtSignal(bool, str)
    progress      = pyqtSignal(int, int)        # (done_count, total)
    file_progress = pyqtSignal(str, int, int)   # (filename, bytes_done, bytes_total)
    preview       = pyqtSignal(str)             # filepath

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg    = cfg
        self._stop  = False

    def cancel(self):
        self._stop = True

    def run(self):
        cfg = self.cfg
        try:
            self.log.emit(f"Logging in as {cfg['handle']}…")
            session = bsky.bluesky_login(cfg["handle"], cfg["password"])
            jwt       = session["accessJwt"]
            my_handle = session["handle"]
            my_did    = session["did"]
            self.log.emit(f"Login OK ({my_handle}).")

            if cfg["target"]:
                target = cfg["target"]
                self.log.emit(f"Resolving {target}…")
                did = bsky.get_did_for_handle(target, jwt)
            else:
                target = my_handle
                did    = my_did

            if cfg["mode"] == "Liked Posts":
                items = bsky.fetch_likes_media(
                    jwt, did, max_pages=cfg["pages"], log_fn=self.log.emit
                )
            else:
                items = bsky.fetch_user_gallery(
                    jwt, did, max_pages=cfg["pages"], log_fn=self.log.emit
                )

            media_map = {"Both": "both", "Images Only": "images", "Videos Only": "videos"}
            stats = bsky.download_media(
                items,
                cfg["output"],
                media_type=media_map[cfg["media"]],
                target_handle=target,
                log_fn=self.log.emit,
                error_fn=self.error.emit,
                cancel_fn=lambda: self._stop,
                progress_fn=lambda d, t: self.progress.emit(d, t),
                file_progress_fn=lambda fn, d, t: self.file_progress.emit(fn, d, t),
                preview_fn=self.preview.emit,
                delay_min=cfg["delay_min"],
                delay_max=cfg["delay_max"],
            )

            total_files = stats["images"] + stats["videos"]
            nb = stats["bytes"]
            size_str = (f"{nb / 1_048_576:.1f} MB" if nb >= 1_048_576
                        else f"{nb / 1024:.1f} KB")
            self.log.emit(
                f"── Summary ──  Images: {stats['images']}  │  "
                f"Videos: {stats['videos']}  │  "
                f"Total: {total_files} files  │  {size_str}"
            )

            if self._stop:
                self.done.emit(False, "Cancelled.")
            else:
                self.done.emit(True, f"Done — {total_files} files · {size_str}")

        except Exception as e:
            self.done.emit(False, str(e))


# ── Update checker ────────────────────────────────────────────────────────────

class UpdateChecker(QThread):
    update_available = pyqtSignal(str, str)   # (tag, download_url)

    def run(self):
        result = bsky.check_latest_release()
        if result:
            self.update_available.emit(*result)


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"BlueSky Downloader v{bsky.VERSION}")
        self.setMinimumWidth(620)
        self.worker: DownloadWorker | None = None
        self._current_preview_pixmap: QPixmap | None = None
        self._update_checker: UpdateChecker | None = None
        self._update_found = False
        self._build_ui()
        self._load_saved_credentials()
        self._load_ui_state()
        self._check_for_updates(silent=True)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction("Check for Updates…").triggered.connect(
            lambda: self._check_for_updates(silent=False)
        )
        help_menu.addSeparator()
        help_menu.addAction(f"Version {bsky.VERSION}").setEnabled(False)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)

        layout.addWidget(self._credentials_group())
        layout.addWidget(self._options_group())
        layout.addWidget(self._output_group())
        layout.addLayout(self._buttons_row())
        layout.addWidget(self._progress_group())

        self._bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._bottom_splitter.setChildrenCollapsible(False)
        self._bottom_splitter.addWidget(self._preview_group())
        self._bottom_splitter.addWidget(self._log_group())
        layout.addWidget(self._bottom_splitter, 1)

        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("Ready")

    def _credentials_group(self) -> QGroupBox:
        g = QGroupBox("Credentials")
        f = QFormLayout(g)
        self.le_handle = QLineEdit(placeholderText="you.bsky.social")
        self.le_pass   = QLineEdit(placeholderText="App password  (Settings → App Passwords)")
        self.le_pass.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Handle:", self.le_handle)
        f.addRow("App Password:", self.le_pass)
        return g

    def _options_group(self) -> QGroupBox:
        g = QGroupBox("Download Options")
        f = QFormLayout(g)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["Liked Posts", "User Gallery"])
        self.cb_mode.currentTextChanged.connect(self._on_mode_changed)

        self.le_target = QLineEdit(placeholderText="Leave blank to use your own account")

        self.cb_media = QComboBox()
        self.cb_media.addItems(["Both", "Images Only", "Videos Only"])

        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 200)
        self.sp_pages.setValue(25)
        self.sp_pages.setSuffix("  pages  (~50 posts each)")

        f.addRow("Mode:", self.cb_mode)
        f.addRow("Target Handle:", self.le_target)
        f.addRow("Media Type:", self.cb_media)
        f.addRow("Max Pages:", self.sp_pages)
        f.addRow("Post Delay:", self._build_delay_widget())
        return g

    def _output_group(self) -> QGroupBox:
        g = QGroupBox("Output Folder")
        h = QHBoxLayout(g)
        self.le_output = QLineEdit(bsky.DEFAULT_DOWNLOAD_DIR)
        btn = QPushButton("Browse…")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_output)
        h.addWidget(self.le_output)
        h.addWidget(btn)
        return g

    def _buttons_row(self) -> QHBoxLayout:
        h = QHBoxLayout()
        self.btn_start  = QPushButton("Start Download")
        self.btn_cancel = QPushButton("Cancel")
        for btn in (self.btn_start, self.btn_cancel):
            btn.setFixedHeight(34)
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)
        h.addWidget(self.btn_start)
        h.addWidget(self.btn_cancel)
        return h

    def _progress_group(self) -> QGroupBox:
        g = QGroupBox("Progress")
        v = QVBoxLayout(g)
        v.setSpacing(4)

        h_overall = QHBoxLayout()
        lbl_total = QLabel("Total:")
        lbl_total.setFixedWidth(42)
        self.pb_overall = QProgressBar()
        self.pb_overall.setTextVisible(False)
        self.pb_overall.setFixedHeight(16)
        self.lbl_overall_count = QLabel("–")
        self.lbl_overall_count.setFixedWidth(90)
        self.lbl_overall_count.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        h_overall.addWidget(lbl_total)
        h_overall.addWidget(self.pb_overall, 1)
        h_overall.addWidget(self.lbl_overall_count)

        h_current = QHBoxLayout()
        lbl_file = QLabel("File:")
        lbl_file.setFixedWidth(42)
        self.pb_current = QProgressBar()
        self.pb_current.setTextVisible(False)
        self.pb_current.setFixedHeight(16)
        h_current.addWidget(lbl_file)
        h_current.addWidget(self.pb_current, 1)

        self.lbl_current_file = QLabel("")
        self.lbl_current_file.setFont(QFont("Monospace", 8))

        v.addLayout(h_overall)
        v.addLayout(h_current)
        v.addWidget(self.lbl_current_file)
        return g

    def _preview_group(self) -> QGroupBox:
        g = QGroupBox("Preview")
        g.setMinimumWidth(150)
        v = QVBoxLayout(g)
        self.lbl_preview = QLabel("No preview")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(100, 150)
        self.lbl_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.lbl_preview.setStyleSheet(
            "background-color: #1a1a2e; color: #666; border-radius: 4px;"
        )
        v.addWidget(self.lbl_preview)
        return g

    def _log_group(self) -> QGroupBox:
        g = QGroupBox("Log")
        v = QVBoxLayout(g)
        self.te_log = QTextEdit()
        self.te_log.setReadOnly(True)
        self.te_log.setFont(QFont("Monospace", 9))
        self.te_log.setMinimumHeight(160)
        v.addWidget(self.te_log)
        return g

    # ── Window events ──────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_splitter_ratio()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale_preview()

    def _apply_splitter_ratio(self):
        screen = QApplication.primaryScreen()
        screen_h = screen.size().height() if screen else 1080

        if screen_h <= 1080:
            preview_ratio = 0.30
            self._bottom_splitter.setStretchFactor(0, 3)
            self._bottom_splitter.setStretchFactor(1, 7)
        else:
            preview_ratio = 0.50
            self._bottom_splitter.setStretchFactor(0, 1)
            self._bottom_splitter.setStretchFactor(1, 1)

        total = self._bottom_splitter.width()
        if total > 0:
            preview_w = int(total * preview_ratio)
            self._bottom_splitter.setSizes([preview_w, total - preview_w])

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_mode_changed(self, mode: str):
        if mode == "User Gallery":
            self.le_target.setPlaceholderText("Handle whose gallery to download")
        else:
            self.le_target.setPlaceholderText("Leave blank to use your own account")

    def _build_delay_widget(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        self.cb_delay_type = QComboBox()
        self.cb_delay_type.addItems(["Fixed", "Variable"])
        self.cb_delay_type.setFixedWidth(84)

        self.dsb_delay_fixed = QDoubleSpinBox()
        self.dsb_delay_fixed.setRange(0.0, 60.0)
        self.dsb_delay_fixed.setSingleStep(0.1)
        self.dsb_delay_fixed.setValue(1.0)
        self.dsb_delay_fixed.setSuffix(" s")
        self.dsb_delay_fixed.setFixedWidth(72)

        self.dsb_delay_min = QDoubleSpinBox()
        self.dsb_delay_min.setRange(0.0, 60.0)
        self.dsb_delay_min.setSingleStep(0.1)
        self.dsb_delay_min.setValue(0.5)
        self.dsb_delay_min.setSuffix(" s")
        self.dsb_delay_min.setFixedWidth(72)
        self.dsb_delay_min.setVisible(False)

        self._lbl_delay_to = QLabel("to")
        self._lbl_delay_to.setVisible(False)

        self.dsb_delay_max = QDoubleSpinBox()
        self.dsb_delay_max.setRange(0.0, 60.0)
        self.dsb_delay_max.setSingleStep(0.1)
        self.dsb_delay_max.setValue(2.0)
        self.dsb_delay_max.setSuffix(" s")
        self.dsb_delay_max.setFixedWidth(72)
        self.dsb_delay_max.setVisible(False)

        h.addWidget(self.cb_delay_type)
        h.addWidget(self.dsb_delay_fixed)
        h.addWidget(self.dsb_delay_min)
        h.addWidget(self._lbl_delay_to)
        h.addWidget(self.dsb_delay_max)
        h.addStretch()

        self.cb_delay_type.currentTextChanged.connect(self._on_delay_type_changed)
        return w

    def _on_delay_type_changed(self, mode: str):
        fixed = mode == "Fixed"
        self.dsb_delay_fixed.setVisible(fixed)
        self.dsb_delay_min.setVisible(not fixed)
        self._lbl_delay_to.setVisible(not fixed)
        self.dsb_delay_max.setVisible(not fixed)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self.le_output.text()
        )
        if path:
            self.le_output.setText(path)

    def _load_saved_credentials(self):
        cfg = bsky.load_config()
        if cfg.has_option("credentials", "handle"):
            self.le_handle.setText(cfg.get("credentials", "handle"))
        if cfg.has_option("credentials", "app_password"):
            pw = bsky.get_app_password(cfg)
            if pw is not None:
                self.le_pass.setText(pw)
            else:
                self.statusBar().showMessage(
                    "Saved password could not be decrypted — please re-enter your app password.", 8000
                )

    def _load_ui_state(self):
        cfg = bsky.load_config()
        if not cfg.has_section("last_run"):
            return
        lr = cfg["last_run"]
        if "mode" in lr:
            idx = self.cb_mode.findText(lr["mode"])
            if idx >= 0:
                self.cb_mode.setCurrentIndex(idx)
        if "target" in lr:
            self.le_target.setText(lr["target"])
        if "media" in lr:
            idx = self.cb_media.findText(lr["media"])
            if idx >= 0:
                self.cb_media.setCurrentIndex(idx)
        if "pages" in lr:
            try:
                self.sp_pages.setValue(int(lr["pages"]))
            except ValueError:
                pass
        if "output" in lr:
            self.le_output.setText(lr["output"])
        if "delay_type" in lr:
            idx = self.cb_delay_type.findText(lr["delay_type"])
            if idx >= 0:
                self.cb_delay_type.setCurrentIndex(idx)
        if "delay_fixed" in lr:
            try:
                self.dsb_delay_fixed.setValue(float(lr["delay_fixed"]))
            except ValueError:
                pass
        if "delay_min" in lr:
            try:
                self.dsb_delay_min.setValue(float(lr["delay_min"]))
            except ValueError:
                pass
        if "delay_max" in lr:
            try:
                self.dsb_delay_max.setValue(float(lr["delay_max"]))
            except ValueError:
                pass

    def _save_ui_state(self):
        bsky.save_ui_state({
            "mode":        self.cb_mode.currentText(),
            "target":      self.le_target.text().strip(),
            "media":       self.cb_media.currentText(),
            "pages":       str(self.sp_pages.value()),
            "output":      self.le_output.text().strip(),
            "delay_type":  self.cb_delay_type.currentText(),
            "delay_fixed": str(self.dsb_delay_fixed.value()),
            "delay_min":   str(self.dsb_delay_min.value()),
            "delay_max":   str(self.dsb_delay_max.value()),
        })

    def _append_log(self, msg: str):
        self.te_log.append(html.escape(msg))
        self._scroll_log()

    def _append_error(self, msg: str):
        self.te_log.append(
            f'<span style="color: #ff5555;">{html.escape(msg)}</span>'
        )
        self._scroll_log()

    def _scroll_log(self):
        sb = self.te_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _update_progress(self, done: int, total: int):
        self.pb_overall.setMaximum(max(total, 1))
        self.pb_overall.setValue(done)
        self.lbl_overall_count.setText(f"{done} / {total} files")

    def _update_file_progress(self, fname: str, done: int, total: int):
        if total > 0:
            self.pb_current.setMaximum(total)
            self.pb_current.setValue(done)
            if total >= 1_048_576:
                size_str = f"{done / 1_048_576:.1f} / {total / 1_048_576:.1f} MB"
            else:
                size_str = f"{done / 1024:.1f} / {total / 1024:.1f} KB"
            self.lbl_current_file.setText(f"{fname}  ({size_str})")
        else:
            self.pb_current.setMaximum(0)
            self.pb_current.setValue(0)
            self.lbl_current_file.setText(fname)

    def _rescale_preview(self):
        if self._current_preview_pixmap and not self._current_preview_pixmap.isNull():
            size = self.lbl_preview.size()
            scaled = self._current_preview_pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.lbl_preview.setPixmap(scaled)

    def _update_preview(self, filepath: str):
        pixmap = QPixmap(filepath)
        if not pixmap.isNull():
            self._current_preview_pixmap = pixmap
            self._rescale_preview()
        else:
            self._current_preview_pixmap = None
            self.lbl_preview.setText("▶ Video")

    def _start(self):
        handle   = self.le_handle.text().strip()
        password = self.le_pass.text().strip()
        if not handle or not password:
            self._append_error("Handle and app password are required.")
            return

        bsky.save_config(handle, password)
        self._save_ui_state()

        if self.cb_delay_type.currentText() == "Fixed":
            d = self.dsb_delay_fixed.value()
            delay_min, delay_max = d, d
        else:
            delay_min = self.dsb_delay_min.value()
            delay_max = max(self.dsb_delay_max.value(), delay_min)

        cfg = {
            "handle":    handle,
            "password":  password,
            "mode":      self.cb_mode.currentText(),
            "target":    self.le_target.text().strip(),
            "media":     self.cb_media.currentText(),
            "pages":     self.sp_pages.value(),
            "output":    self.le_output.text().strip(),
            "delay_min": delay_min,
            "delay_max": delay_max,
        }

        self.te_log.clear()
        self.pb_overall.setMaximum(100)
        self.pb_overall.setValue(0)
        self.pb_current.setMaximum(100)
        self.pb_current.setValue(0)
        self.lbl_overall_count.setText("–")
        self.lbl_current_file.setText("")
        self._current_preview_pixmap = None
        self.lbl_preview.setText("No preview")

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.statusbar.showMessage("Downloading…")

        self.worker = DownloadWorker(cfg)
        self.worker.log.connect(self._append_log)
        self.worker.error.connect(self._append_error)
        self.worker.done.connect(self._on_done)
        self.worker.progress.connect(self._update_progress)
        self.worker.file_progress.connect(self._update_file_progress)
        self.worker.preview.connect(self._update_preview)
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.statusbar.showMessage("Cancelling…")

    def _check_for_updates(self, silent: bool = False):
        if self._update_checker and self._update_checker.isRunning():
            return
        self._update_found = False
        self._update_checker = UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_available)
        if not silent:
            self._update_checker.finished.connect(self._on_update_check_finished)
        self._update_checker.start()

    def _on_update_available(self, tag: str, url: str):
        self._update_found = True
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Update Available")
        dlg.setIcon(QMessageBox.Icon.Information)
        dlg.setText(f"Version <b>{tag}</b> is available.")
        dlg.setInformativeText(
            f"You are running v{bsky.VERSION}. Would you like to download the update?"
        )
        download_btn = dlg.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        dlg.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        dlg.exec()
        if dlg.clickedButton() == download_btn:
            QDesktopServices.openUrl(QUrl(url))

    def _on_update_check_finished(self):
        if not self._update_found:
            QMessageBox.information(
                self,
                "No Updates",
                f"You are already running the latest version (v{bsky.VERSION}).",
            )

    def _on_done(self, ok: bool, msg: str):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if ok or msg == "Cancelled.":
            self._append_log(msg)
        else:
            self._append_error(msg)
        self.statusbar.showMessage(msg)
        self.lbl_current_file.setText("")
        self.pb_current.setMaximum(100)
        self.pb_current.setValue(0)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BlueSky Downloader")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
