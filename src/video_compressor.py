#!/usr/bin/python

import sys
import os
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QSlider, QDoubleSpinBox, QComboBox,
        QProgressBar, QFrame, QSizePolicy, QFileDialog, QMessageBox,
        QGroupBox,
    )
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
    from PyQt6.QtGui import QDragEnterEvent, QDropEvent
except ImportError:
    print("PyQt6 is required.  Run:  pip install PyQt6")
    sys.exit(1)


# ─── Config ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "video_compressor_config.json")
DEFAULT_CONFIG = {
    "preferred_codec": None,
    "last_directory": str(Path.home()),
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save config: {e}")


# ─── FFmpeg helpers ──────────────────────────────────────────────────────────────

def find_ffmpeg():
    return shutil.which("ffmpeg"), shutil.which("ffprobe")

def get_video_info(ffprobe_bin, file_path):
    """Returns (duration_s, width, height, fps, size_bytes)."""
    cmd = [
        ffprobe_bin, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-show_entries", "format=duration,size",
        "-of", "json", file_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe error:\n{r.stderr}")
    data = json.loads(r.stdout)
    streams = data.get("streams", [{}])
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or streams[0].get("duration") or 0)
    width  = int(streams[0].get("width",  0))
    height = int(streams[0].get("height", 0))
    fps_raw = streams[0].get("r_frame_rate", "30/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0
    size = int(fmt.get("size", os.path.getsize(file_path)))
    return duration, width, height, fps, size

def build_ffmpeg_command(ffmpeg_bin, input_path, output_path, codec,
                         target_bitrate_bps, target_fps, original_fps):
    fps_filter = f"fps={target_fps:.3f}" if target_fps < original_fps - 0.5 else ""
    maxrate = int(target_bitrate_bps * 1.5)
    bufsize = int(target_bitrate_bps * 2)
    if codec == "h264_vaapi":
        vf_parts = ["format=nv12", "hwupload"]
        if fps_filter:
            vf_parts.insert(0, fps_filter)
        cmd = [
            ffmpeg_bin, "-y",
            "-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128",
            "-i", input_path,
            "-vf", ",".join(vf_parts),
            "-c:v", "h264_vaapi",
            "-b:v", str(int(target_bitrate_bps)),
            "-maxrate", str(maxrate), "-bufsize", str(bufsize),
            "-c:a", "copy",
        ]
    else:
        cmd = [ffmpeg_bin, "-y", "-i", input_path]
        if fps_filter:
            cmd += ["-vf", fps_filter]
        cmd += [
            "-c:v", codec,
            "-b:v", str(int(target_bitrate_bps)),
            "-maxrate:v", str(maxrate), "-bufsize:v", str(bufsize),
            "-c:a", "copy",
        ]
    cmd += ["-progress", "pipe:1", "-nostats", output_path]
    return cmd


# ─── Background workers ──────────────────────────────────────────────────────────

class CodecDetectWorker(QThread):
    finished = pyqtSignal(str)

    def __init__(self, ffmpeg_bin):
        super().__init__()
        self.ffmpeg_bin = ffmpeg_bin

    def run(self):
        test_file = "/tmp/_vc_test_.mp4"
        gen = [self.ffmpeg_bin, "-y", "-f", "lavfi",
               "-i", "testsrc=duration=1:size=128x128:rate=30",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", test_file]
        subprocess.run(gen, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        candidates = [
            ("h264_nvenc", [self.ffmpeg_bin, "-y", "-i", test_file,
                            "-c:v", "h264_nvenc", "-frames:v", "2", "-f", "null", "-"]),
            ("h264_vaapi", [self.ffmpeg_bin, "-y",
                            "-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128",
                            "-i", test_file, "-vf", "format=nv12,hwupload",
                            "-c:v", "h264_vaapi", "-frames:v", "2", "-f", "null", "-"]),
            ("h264_qsv",   [self.ffmpeg_bin, "-y", "-i", test_file,
                            "-c:v", "h264_qsv", "-frames:v", "2", "-f", "null", "-"]),
            ("libx264",    [self.ffmpeg_bin, "-y", "-i", test_file,
                            "-c:v", "libx264", "-frames:v", "2", "-f", "null", "-"]),
        ]
        result = "libx264"
        for codec, cmd in candidates:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                result = codec
                break
        try:
            os.remove(test_file)
        except Exception:
            pass
        self.finished.emit(result)


class FFmpegWorker(QThread):
    progress  = pyqtSignal(float)
    succeeded = pyqtSignal(str, int)
    failed    = pyqtSignal(str)

    def __init__(self, cmd, output_path, duration_s):
        super().__init__()
        self.cmd = cmd
        self.output_path = output_path
        self.duration_us = duration_s * 1_000_000

    def run(self):
        try:
            proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = float(line.split("=")[1])
                        pct = min(us / self.duration_us * 100, 99.0)
                        self.progress.emit(pct)
                    except Exception:
                        pass
            proc.wait()
            stderr_out = proc.stderr.read()
            if proc.returncode == 0 and os.path.exists(self.output_path):
                self.succeeded.emit(self.output_path, os.path.getsize(self.output_path))
            else:
                err_lines = [l for l in stderr_out.splitlines()
                             if "error" in l.lower() or "invalid" in l.lower()]
                msg = "\n".join(err_lines[-6:]) if err_lines else stderr_out[-600:]
                self.failed.emit(msg)
        except Exception as e:
            self.failed.emit(str(e))


# ─── Drop Zone ───────────────────────────────────────────────────────────────────

class DropZone(QLabel):
    """Label that accepts file drops and emits file_dropped(path)."""
    file_dropped = pyqtSignal(str)

    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv",
                  ".flv", ".webm", ".m4v", ".ts", ".m2ts"}

    _IDLE   = ("border: 2px dashed #45475a; border-radius: 8px;"
               "background: #181825; color: #89b4fa;"
               "padding: 28px; font-size: 14px;")
    _ACTIVE = ("border: 2px dashed #89b4fa; border-radius: 8px;"
               "background: #1e2030; color: #cdd6f4;"
               "padding: 28px; font-size: 14px;")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setStyleSheet(self._IDLE)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("📂   Drop a video file here\nor click  Browse")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(90)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            path = e.mimeData().urls()[0].toLocalFile()
            if Path(path).suffix.lower() in self.VIDEO_EXTS:
                e.acceptProposedAction()
                self.setStyleSheet(self._ACTIVE)
                self.setText("⬇   Release to load")
                return
        e.ignore()

    def dragLeaveEvent(self, e):
        self._reset()

    def dropEvent(self, e: QDropEvent):
        self._reset()
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if not path:
                raw = urls[0].toString()
                if raw.startswith("file://"):
                    path = unquote(raw[7:])
            if path:
                self.file_dropped.emit(path)

    def _reset(self):
        self.setStyleSheet(self._IDLE)
        self.setText("📂   Drop a video file here\nor click  Browse")


# ─── Stylesheet ──────────────────────────────────────────────────────────────────

APP_STYLE = """
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 6px;
    font-weight: bold;
    color: #89b4fa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QSlider::groove:horizontal {
    height: 6px; background: #45475a; border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #89b4fa; width: 16px; height: 16px;
    margin: -5px 0; border-radius: 8px;
}
QSlider::sub-page:horizontal { background: #89b4fa; border-radius: 3px; }
QComboBox, QDoubleSpinBox {
    background: #313244; border: 1px solid #45475a;
    border-radius: 4px; padding: 3px 8px; color: #cdd6f4; min-height: 24px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background: #313244; selection-background-color: #45475a; color: #cdd6f4;
}
QPushButton {
    background: #45475a; border: none; border-radius: 5px;
    padding: 6px 16px; color: #cdd6f4;
}
QPushButton:hover   { background: #585b70; }
QPushButton:pressed { background: #6c7086; }
QPushButton:disabled { background: #313244; color: #6c7086; }
QPushButton#convertBtn {
    background: #89b4fa; color: #1e1e2e;
    font-size: 15px; font-weight: bold;
    padding: 10px 32px; border-radius: 7px;
}
QPushButton#convertBtn:hover    { background: #74c7ec; }
QPushButton#convertBtn:disabled { background: #45475a; color: #6c7086; }
QProgressBar {
    background: #313244; border: none; border-radius: 5px;
    height: 16px; text-align: center; color: #1e1e2e; font-weight: bold;
}
QProgressBar::chunk { background: #89b4fa; border-radius: 5px; }
QLabel#infoLabel   { color: #f9e2af; }
QLabel#estimLabel  { color: #a6e3a1; font-size: 14px; font-weight: bold; }
QLabel#statusLabel { color: #6c7086; font-size: 11px; }
QLabel#configLabel { color: #6c7086; font-size: 11px; }
"""


# ─── Main Window ─────────────────────────────────────────────────────────────────

CODEC_OPTIONS = ["auto (from config)", "libx264", "h264_nvenc",
                 "h264_vaapi", "h264_qsv", "libx265", "vp9"]
COMMON_FPS    = ["source", "60", "48", "30", "29.97", "25",
                 "24", "23.976", "20", "15", "10"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Compressor")
        self.setMinimumSize(660, 700)

        self.cfg = load_config()
        self.ffmpeg_bin, self.ffprobe_bin = find_ffmpeg()

        self.input_path   = ""
        self.vid_duration = 0.0
        self.vid_fps      = 30.0
        self.vid_size     = 0
        self.vid_w = self.vid_h = 0

        self._worker: FFmpegWorker | None = None
        self._codec_worker: CodecDetectWorker | None = None
        self._block = False   # re-entrancy guard for estimate updates

        self._build_ui()
        self._check_ffmpeg()

    # ── UI ───────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        # Drop zone
        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self.load_file)
        self.drop_zone.mousePressEvent = lambda e: self._browse()
        lay.addWidget(self.drop_zone)

        self.file_label = QLabel("No file selected.")
        self.file_label.setWordWrap(True)
        self.file_label.setStyleSheet("color: #6c7086; font-size: 11px;")
        lay.addWidget(self.file_label)

        self.info_label = QLabel("")
        self.info_label.setObjectName("infoLabel")
        lay.addWidget(self.info_label)

        # ── Compression group ────────────────────────────────────────────────────
        comp = QGroupBox("Compression")
        cl = QVBoxLayout(comp)

        pct_row = QHBoxLayout()
        pct_row.addWidget(QLabel("Target size (% of original):"))
        pct_row.addStretch()
        self.pct_label = QLabel("50%")
        self.pct_label.setStyleSheet("color:#89b4fa; font-weight:bold; min-width:42px;")
        pct_row.addWidget(self.pct_label)
        cl.addLayout(pct_row)

        self.pct_slider = QSlider(Qt.Orientation.Horizontal)
        self.pct_slider.setRange(1, 99)
        self.pct_slider.setValue(50)
        self.pct_slider.valueChanged.connect(self._on_pct_slider)
        cl.addWidget(self.pct_slider)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#45475a;")
        cl.addWidget(sep)

        fixed_row = QHBoxLayout()
        fixed_row.addWidget(QLabel("— or —  Target size:"))
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0, 999999)
        self.size_spin.setDecimals(2)
        self.size_spin.setSingleStep(1)
        self.size_spin.setValue(0)
        self.size_spin.setMinimumWidth(100)
        self.size_spin.valueChanged.connect(self._on_size_spin)
        fixed_row.addWidget(self.size_spin)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["KB", "MB", "GB"])
        self.unit_combo.setCurrentText("MB")
        self.unit_combo.setFixedWidth(64)
        self.unit_combo.currentTextChanged.connect(self._on_size_spin)
        fixed_row.addWidget(self.unit_combo)
        hl = QLabel("(0 = use % slider)")
        hl.setStyleSheet("color:#6c7086; font-size:11px;")
        fixed_row.addWidget(hl)
        fixed_row.addStretch()
        cl.addLayout(fixed_row)
        lay.addWidget(comp)

        # ── FPS group ────────────────────────────────────────────────────────────
        fps_g = QGroupBox("Frame Rate")
        fl = QVBoxLayout(fps_g)

        fps_top = QHBoxLayout()
        fps_top.addWidget(QLabel("Preset:"))
        self.fps_preset = QComboBox()
        self.fps_preset.addItems(COMMON_FPS)
        self.fps_preset.setCurrentText("source")
        self.fps_preset.setFixedWidth(90)
        self.fps_preset.currentTextChanged.connect(self._on_fps_preset)
        fps_top.addWidget(self.fps_preset)
        fps_top.addSpacing(16)
        fps_top.addWidget(QLabel("Fine-tune:"))
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0.1, 240)
        self.fps_spin.setDecimals(3)
        self.fps_spin.setSingleStep(0.001)
        self.fps_spin.setValue(30.0)
        self.fps_spin.setFixedWidth(100)
        self.fps_spin.valueChanged.connect(self._on_fps_spin)
        fps_top.addWidget(self.fps_spin)
        fps_top.addWidget(QLabel("fps"))
        fps_top.addStretch()
        self.fps_status = QLabel("= source")
        self.fps_status.setStyleSheet("color:#89b4fa; font-style:italic;")
        fps_top.addWidget(self.fps_status)
        fl.addLayout(fps_top)

        fps_sl_row = QHBoxLayout()
        self.fps_min_lbl = QLabel("1")
        self.fps_min_lbl.setStyleSheet("color:#6c7086; font-size:11px;")
        fps_sl_row.addWidget(self.fps_min_lbl)
        self.fps_slider = QSlider(Qt.Orientation.Horizontal)
        # store ×100 fixed-point so slider handles fractional fps
        self.fps_slider.setRange(10, 3000)
        self.fps_slider.setValue(3000)
        self.fps_slider.valueChanged.connect(self._on_fps_slider)
        fps_sl_row.addWidget(self.fps_slider, 1)
        self.fps_max_lbl = QLabel("30.000")
        self.fps_max_lbl.setStyleSheet("color:#6c7086; font-size:11px;")
        fps_sl_row.addWidget(self.fps_max_lbl)
        fl.addLayout(fps_sl_row)

        fhint = QLabel("Slider is capped to source FPS.  Reducing FPS lowers file size.")
        fhint.setStyleSheet("color:#6c7086; font-size:10px;")
        fhint.setAlignment(Qt.AlignmentFlag.AlignRight)
        fl.addWidget(fhint)
        lay.addWidget(fps_g)

        # ── Codec group ──────────────────────────────────────────────────────────
        cg = QGroupBox("Codec && Settings")
        cgl = QVBoxLayout(cg)
        cr = QHBoxLayout()
        cr.addWidget(QLabel("Codec:"))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(CODEC_OPTIONS)
        self.codec_combo.setCurrentText("auto (from config)")
        self.codec_combo.setMinimumWidth(180)
        cr.addWidget(self.codec_combo)
        self.detect_btn = QPushButton("⚡ Auto-detect best codec")
        self.detect_btn.clicked.connect(self._detect_codec)
        cr.addWidget(self.detect_btn)
        cr.addStretch()
        cgl.addLayout(cr)
        self.config_label = QLabel(
            f"Config codec: {self.cfg.get('preferred_codec') or 'not set'}")
        self.config_label.setObjectName("configLabel")
        cgl.addWidget(self.config_label)
        lay.addWidget(cg)

        # Estimate
        self.estim_label = QLabel("Estimated output size: —")
        self.estim_label.setObjectName("estimLabel")
        self.estim_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.estim_label)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        lay.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.status_label)

        # Convert button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.convert_btn = QPushButton("▶   Convert")
        self.convert_btn.setObjectName("convertBtn")
        self.convert_btn.clicked.connect(self._start_convert)
        btn_row.addWidget(self.convert_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    # ── FFmpeg check ─────────────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        missing = []
        if not self.ffmpeg_bin:  missing.append("ffmpeg")
        if not self.ffprobe_bin: missing.append("ffprobe")
        if missing:
            QMessageBox.warning(self, "FFmpeg Not Found",
                f"Not found in PATH: {', '.join(missing)}\n\n"
                "Please install FFmpeg and make sure it's accessible from the terminal.")

    # ── Browse / load ─────────────────────────────────────────────────────────────

    def _browse(self):
        init = self.cfg.get("last_directory", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", init,
            "Video Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts);;"
            "All Files (*)")
        if path:
            self.load_file(path)

    def load_file(self, path):
        path = path.strip().strip('"')
        if not os.path.isfile(path):
            QMessageBox.critical(self, "Error", f"File not found:\n{path}")
            return
        if not self.ffprobe_bin:
            QMessageBox.critical(self, "Error", "ffprobe not found in PATH.")
            return
        try:
            dur, w, h, fps, size = get_video_info(self.ffprobe_bin, path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read video info:\n{e}")
            return

        self.input_path   = path
        self.vid_duration = dur
        self.vid_fps      = fps
        self.vid_size     = size
        self.vid_w, self.vid_h = w, h

        self.file_label.setText(path)
        self.file_label.setStyleSheet("color:#cdd6f4; font-size:11px;")
        self.info_label.setText(
            f"{w}×{h}  |  {fps:.3f} fps  |  {dur:.1f} s  |  {self._fmt(size)}")

        # Reset FPS controls
        self._block = True
        fps_max_fp = max(10, int(round(fps * 100)))
        self.fps_slider.setRange(10, fps_max_fp)
        self.fps_slider.setValue(fps_max_fp)
        self.fps_spin.setMaximum(fps + 0.001)
        self.fps_spin.setValue(fps)
        self.fps_preset.setCurrentText("source")
        self.fps_max_lbl.setText(f"{fps:.3f}")
        self.fps_status.setText("= source")
        self._block = False

        self.cfg["last_directory"] = str(Path(path).parent)
        save_config(self.cfg)
        self._update_estimate()

    # ── Compression callbacks ─────────────────────────────────────────────────────

    def _on_pct_slider(self, val):
        self.pct_label.setText(f"{val}%")
        self._block = True
        self.size_spin.setValue(0)
        self._block = False
        self._update_estimate()

    def _on_size_spin(self):
        if not self._block:
            self._update_estimate()

    # ── FPS callbacks ─────────────────────────────────────────────────────────────

    def _on_fps_preset(self, text):
        if self._block:
            return
        if text == "source":
            fps = self.vid_fps or 30.0
        else:
            try:
                fps = float(text)
            except ValueError:
                return
        fps = min(fps, self.vid_fps or fps)
        self._sync_fps(fps)

    def _on_fps_spin(self, val):
        if self._block:
            return
        fps = min(val, self.vid_fps or val)
        self._sync_fps(fps, skip="spin")

    def _on_fps_slider(self, val):
        if self._block:
            return
        fps = val / 100.0
        fps = min(fps, self.vid_fps or fps)
        self._sync_fps(fps, skip="slider")

    def _sync_fps(self, fps, skip=""):
        self._block = True
        if skip != "spin":
            self.fps_spin.setValue(fps)
        if skip != "slider":
            self.fps_slider.setValue(int(round(fps * 100)))
        src = self.vid_fps or fps
        self.fps_status.setText("= source" if abs(fps - src) < 0.05 else f"↓ {fps:.3f} fps")
        self._block = False
        self._update_estimate()

    # ── Estimate ──────────────────────────────────────────────────────────────────

    def _calc_target_bytes(self):
        fixed = self.size_spin.value()
        if fixed > 0:
            mult = {"KB": 1_024, "MB": 1_048_576, "GB": 1_073_741_824}
            return fixed * mult.get(self.unit_combo.currentText(), 1_048_576)
        return self.vid_size * (self.pct_slider.value() / 100.0)

    def _update_estimate(self):
        if self._block or not self.vid_size:
            if not self.vid_size:
                self.estim_label.setText("Estimated output size: —")
            return
        target = self._calc_target_bytes()
        fps_ratio = min(self.fps_spin.value() / max(self.vid_fps, 0.001), 1.0)
        audio_bytes = 128_000 / 8 * self.vid_duration
        est = max(0, target - audio_bytes) * fps_ratio + audio_bytes
        pct = est / self.vid_size * 100
        if self.size_spin.value() > 0:
            self.pct_label.setText(f"{pct:.0f}%")
            self.pct_slider.blockSignals(True)
            self.pct_slider.setValue(max(1, min(99, int(pct))))
            self.pct_slider.blockSignals(False)
        self.estim_label.setText(
            f"Estimated output size:  {self._fmt(est)}  ({pct:.0f}% of original)")

    # ── Codec detection ──────────────────────────────────────────────────────────

    def _detect_codec(self):
        if not self.ffmpeg_bin:
            QMessageBox.critical(self, "Error", "ffmpeg not found in PATH.")
            return
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting…")
        self.status_label.setText("Testing codecs, please wait…")
        self._codec_worker = CodecDetectWorker(self.ffmpeg_bin)
        self._codec_worker.finished.connect(self._on_codec_detected)
        self._codec_worker.start()

    def _on_codec_detected(self, codec):
        self.cfg["preferred_codec"] = codec
        save_config(self.cfg)
        self.config_label.setText(f"Config codec: {codec}  ✅ saved")
        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("⚡ Auto-detect best codec")
        self.status_label.setText(f"Best codec detected: {codec}")

    # ── Convert ──────────────────────────────────────────────────────────────────

    def _start_convert(self):
        if not self.input_path:
            QMessageBox.warning(self, "No File", "Please select a video file first.")
            return
        if not self.ffmpeg_bin:
            QMessageBox.critical(self, "Error", "ffmpeg not found in PATH.")
            return
        if self.vid_duration <= 0:
            QMessageBox.critical(self, "Error", "Could not determine video duration.")
            return

        chosen = self.codec_combo.currentText()
        codec  = (self.cfg.get("preferred_codec") or "libx264") \
                 if chosen.startswith("auto") else chosen

        target_bytes = self._calc_target_bytes()
        if target_bytes <= 0:
            QMessageBox.critical(self, "Error", "Target size must be greater than 0.")
            return

        audio_bps   = 128_000
        video_bytes = max(target_bytes - audio_bps / 8 * self.vid_duration,
                          target_bytes * 0.8)
        bitrate_bps = (video_bytes * 8) / self.vid_duration
        target_fps  = self.fps_spin.value()

        base, ext = os.path.splitext(self.input_path)
        output_path = base + "_compressed" + ext

        cmd = build_ffmpeg_command(
            self.ffmpeg_bin, self.input_path, output_path,
            codec, bitrate_bps, target_fps, self.vid_fps)

        self.convert_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Converting…")

        self._worker = FFmpegWorker(cmd, output_path, self.vid_duration)
        self._worker.progress.connect(
            lambda p: (self.progress_bar.setValue(int(p)),
                       self.status_label.setText(f"Converting… {p:.0f}%")))
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.start()

    def _on_success(self, output_path, out_size):
        self.progress_bar.setValue(100)
        self.convert_btn.setEnabled(True)
        ratio = out_size / self.vid_size * 100 if self.vid_size else 0
        self.status_label.setText(f"✅ Done! → {os.path.basename(output_path)}")
        QMessageBox.information(self, "Done!",
            f"✅ Compressed successfully!\n\n"
            f"Output:    {output_path}\n\n"
            f"Original:  {self._fmt(self.vid_size)}\n"
            f"Output:    {self._fmt(out_size)}  ({ratio:.1f}% of original)")

    def _on_failure(self, msg):
        self.progress_bar.setValue(0)
        self.convert_btn.setEnabled(True)
        self.status_label.setText("❌ Conversion failed.")
        QMessageBox.critical(self, "Conversion Failed",
            f"FFmpeg error:\n\n{msg}\n\n"
            "Tips:\n"
            "• Run ⚡ Auto-detect to pick a compatible codec\n"
            "• Make sure the input file is a valid video\n"
            "• Check write permissions on the output folder")

    @staticmethod
    def _fmt(b):
        b = float(b)
        if b >= 1_073_741_824: return f"{b/1_073_741_824:.2f} GB"
        if b >= 1_048_576:     return f"{b/1_048_576:.2f} MB"
        if b >= 1_024:         return f"{b/1_024:.1f} KB"
        return f"{b:.0f} B"


# ─── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Tell Qt to use the native Wayland backend rather than XWayland.
    # This is what makes drag-and-drop work on Wayland compositors (KDE, GNOME, etc.)
    # Falls back silently to X11 if Wayland is unavailable.
    if sys.platform == "linux":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
