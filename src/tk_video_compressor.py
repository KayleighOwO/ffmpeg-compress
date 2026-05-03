import sys
import os
import json
import shutil
import subprocess
import threading
import re
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("tkinter is required. Install it with: sudo apt-get install python3-tk")
    sys.exit(1)

# ─── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_compressor_config.json")

DEFAULT_CONFIG = {
    "preferred_codec": None,
    "last_directory": str(Path.home()),
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
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

# ─── FFmpeg helpers ─────────────────────────────────────────────────────────────

def find_ffmpeg():
    return shutil.which("ffmpeg"), shutil.which("ffprobe")

def get_video_info(ffprobe_bin, file_path):
    """Returns (duration_seconds, width, height, fps, filesize_bytes)."""
    cmd = [
        ffprobe_bin, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-show_entries", "format=duration,size",
        "-of", "json",
        file_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr}")
    data = json.loads(result.stdout)

    streams = data.get("streams", [{}])
    fmt = data.get("format", {})

    # Duration (prefer format-level)
    duration = float(fmt.get("duration") or streams[0].get("duration") or 0)

    width = int(streams[0].get("width", 0))
    height = int(streams[0].get("height", 0))

    # FPS as fraction
    fps_raw = streams[0].get("r_frame_rate", "30/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0

    size = int(fmt.get("size", os.path.getsize(file_path)))
    return duration, width, height, fps, size

def detect_best_codec(ffmpeg_bin):
    """Test hardware and software codecs, return the fastest available."""
    # Create a tiny dummy video in /tmp for testing
    test_file = "/tmp/_vc_test_.mp4"
    gen_cmd = [
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=128x128:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", test_file
    ]
    subprocess.run(gen_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    candidates = [
        ("h264_nvenc", [
            ffmpeg_bin, "-y", "-i", test_file,
            "-c:v", "h264_nvenc", "-frames:v", "2", "-f", "null", "-"
        ]),
        ("h264_vaapi", [
            ffmpeg_bin, "-y",
            "-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128",
            "-i", test_file,
            "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
            "-frames:v", "2", "-f", "null", "-"
        ]),
        ("h264_qsv", [
            ffmpeg_bin, "-y", "-i", test_file,
            "-c:v", "h264_qsv", "-frames:v", "2", "-f", "null", "-"
        ]),
        ("libx264", [
            ffmpeg_bin, "-y", "-i", test_file,
            "-c:v", "libx264", "-frames:v", "2", "-f", "null", "-"
        ]),
    ]

    for codec, cmd in candidates:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            try:
                os.remove(test_file)
            except Exception:
                pass
            return codec

    try:
        os.remove(test_file)
    except Exception:
        pass
    return "libx264"

def build_ffmpeg_command(ffmpeg_bin, input_path, output_path, codec,
                          target_bitrate_bps, target_fps, original_fps):
    fps_filter = ""
    if target_fps < original_fps - 0.5:
        fps_filter = f"fps={target_fps:.3f}"

    maxrate = int(target_bitrate_bps * 1.5)
    bufsize = int(target_bitrate_bps * 2)

    if codec == "h264_vaapi":
        vf_parts = ["format=nv12", "hwupload"]
        if fps_filter:
            vf_parts.insert(0, fps_filter)
        vf = ",".join(vf_parts)
        cmd = [
            ffmpeg_bin, "-y",
            "-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128",
            "-i", input_path,
            "-vf", vf,
            "-c:v", "h264_vaapi",
            "-b:v", str(int(target_bitrate_bps)),
            "-maxrate", str(maxrate),
            "-bufsize", str(bufsize),
            "-c:a", "copy",
            output_path
        ]
    else:
        cmd = [ffmpeg_bin, "-y", "-i", input_path]
        if fps_filter:
            cmd += ["-vf", fps_filter]
        cmd += [
            "-c:v", codec,
            "-b:v", str(int(target_bitrate_bps)),
            "-maxrate:v", str(maxrate),
            "-bufsize:v", str(bufsize),
            "-c:a", "copy",
            output_path
        ]
    return cmd

# ─── Main Application ───────────────────────────────────────────────────────────

CODEC_OPTIONS = ["auto (from config)", "libx264", "h264_nvenc", "h264_vaapi", "h264_qsv", "libx265", "vp9"]
COMMON_FPS = ["source", "60", "48", "30", "29.97", "25", "24", "23.976", "20", "15", "10"]

# Try to import tkinterdnd2 so we can use it as the base class if available.
# It MUST be the Tk base class — you can't retrofit DnD onto a plain tk.Tk window.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
    _TkBase = TkinterDnD.Tk
except Exception:
    _DND_AVAILABLE = False
    _TkBase = tk.Tk


class VideoCompressorApp(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor")
        self.resizable(True, True)
        self.minsize(640, 760)
        self.configure(bg="#1e1e2e")

        self.config_data = load_config()
        self.ffmpeg_bin, self.ffprobe_bin = find_ffmpeg()

        # State
        self.input_file = tk.StringVar()
        self.video_duration = 0.0
        self.video_fps = 30.0
        self.video_size_bytes = 0
        self.video_width = 0
        self.video_height = 0

        self.pct_var = tk.DoubleVar(value=50.0)
        self.target_size_var = tk.DoubleVar(value=0.0)
        self.size_unit_var = tk.StringVar(value="MB")
        self.fps_var = tk.DoubleVar(value=30.0)
        self.fps_preset_var = tk.StringVar(value="source")
        self.codec_var = tk.StringVar(value="auto (from config)")
        self.progress_var = tk.DoubleVar(value=0.0)
        self._ignore_fps_spin = False  # re-entrancy guard

        self._build_ui()
        self._check_ffmpeg()
        self._setup_dnd()

    # ── UI Build ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        S = {
            "bg": "#1e1e2e",
            "fg": "#cdd6f4",
            "entry_bg": "#313244",
            "accent": "#89b4fa",
            "btn_bg": "#45475a",
            "btn_active": "#585b70",
            "green": "#a6e3a1",
            "red": "#f38ba8",
            "yellow": "#f9e2af",
            "frame_bg": "#181825",
        }
        self.S = S
        self.configure(bg=S["bg"])

        pad = {"padx": 14}

        # ── Drop / Browse zone ──────────────────────────────────────────────────
        drop_frame = tk.Frame(self, bg=S["frame_bg"], relief="ridge", bd=2)
        drop_frame.pack(fill="x", **pad, pady=(14, 6))

        dnd_hint = "  (install tkinterdnd2 for drag & drop)" if not _DND_AVAILABLE else "  drag & drop active ✓"
        self.drop_label = tk.Label(
            drop_frame,
            text=f"📂  Drop a video here, or click Browse{dnd_hint}",
            bg=S["frame_bg"], fg=S["accent"],
            font=("Segoe UI", 11), pady=18, cursor="hand2"
        )
        self.drop_label.pack(fill="x")
        self.drop_label.bind("<Button-1>", lambda e: self._browse_file())

        # Path paste row — reliable on Wayland / any environment
        paste_row = tk.Frame(drop_frame, bg=S["frame_bg"])
        paste_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(paste_row, text="Or paste path:", bg=S["frame_bg"],
                 fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
        self._paste_var = tk.StringVar()
        paste_entry = tk.Entry(paste_row, textvariable=self._paste_var,
                               bg=S["entry_bg"], fg=S["fg"],
                               insertbackground=S["fg"], relief="flat",
                               font=("Segoe UI", 9))
        paste_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        paste_entry.bind("<Return>", lambda e: self._load_from_paste())
        paste_entry.bind("<FocusOut>", lambda e: self._load_from_paste())
        tk.Button(paste_row, text="Load", bg=S["btn_bg"], fg=S["fg"],
                  activebackground=S["btn_active"], relief="flat",
                  font=("Segoe UI", 9), padx=6,
                  command=self._load_from_paste).pack(side="left")

        self.file_label = tk.Label(
            drop_frame, text="No file selected.",
            bg=S["frame_bg"], fg=S["fg"],
            font=("Segoe UI", 9), pady=4, wraplength=560
        )
        self.file_label.pack(fill="x", padx=8)

        self.info_label = tk.Label(
            drop_frame, text="",
            bg=S["frame_bg"], fg=S["yellow"],
            font=("Segoe UI", 9), pady=2
        )
        self.info_label.pack(fill="x", padx=8, pady=(0, 6))

        # ── Compression controls ────────────────────────────────────────────────
        ctrl_frame = tk.LabelFrame(self, text=" Compression ", bg=S["bg"],
                                   fg=S["accent"], font=("Segoe UI", 10, "bold"),
                                   relief="groove", bd=2)
        ctrl_frame.pack(fill="x", **pad)

        # Row: percentage slider
        pct_row = tk.Frame(ctrl_frame, bg=S["bg"])
        pct_row.pack(fill="x", padx=10, pady=(8, 2))

        tk.Label(pct_row, text="Target size (% of original):", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left")
        self.pct_value_lbl = tk.Label(pct_row, text="50%", width=6,
                                       bg=S["bg"], fg=S["accent"],
                                       font=("Segoe UI", 10, "bold"))
        self.pct_value_lbl.pack(side="right")

        self.pct_slider = ttk.Scale(ctrl_frame, from_=1, to=99,
                                     orient="horizontal",
                                     variable=self.pct_var,
                                     command=self._on_pct_slider)
        self.pct_slider.pack(fill="x", padx=10, pady=(0, 8))

        # Separator
        ttk.Separator(ctrl_frame, orient="horizontal").pack(fill="x", padx=10, pady=4)

        # Row: fixed size spinbox + unit
        size_row = tk.Frame(ctrl_frame, bg=S["bg"])
        size_row.pack(fill="x", padx=10, pady=(4, 8))
        tk.Label(size_row, text="  — or —  Target size:", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left")

        self.size_spin = tk.Spinbox(
            size_row, from_=0, to=99999, increment=1,
            textvariable=self.target_size_var,
            width=8, bg=S["entry_bg"], fg=S["fg"],
            insertbackground=S["fg"], relief="flat",
            font=("Segoe UI", 10), command=self._on_size_spin
        )
        self.size_spin.pack(side="left", padx=6)
        self.size_spin.bind("<KeyRelease>", lambda e: self._on_size_spin())

        self.unit_menu = ttk.Combobox(size_row, textvariable=self.size_unit_var,
                                       values=["KB", "MB", "GB"], width=5,
                                       state="readonly")
        self.unit_menu.pack(side="left")
        self.unit_menu.bind("<<ComboboxSelected>>", lambda e: self._on_size_spin())

        tk.Label(size_row, text="  (0 = use % slider)", bg=S["bg"],
                 fg="#6c7086", font=("Segoe UI", 8)).pack(side="left")

        # ── FPS control ─────────────────────────────────────────────────────────
        fps_frame = tk.LabelFrame(self, text=" Frame Rate ", bg=S["bg"],
                                   fg=S["accent"], font=("Segoe UI", 10, "bold"),
                                   relief="groove", bd=2)
        fps_frame.pack(fill="x", **pad)

        fps_top = tk.Frame(fps_frame, bg=S["bg"])
        fps_top.pack(fill="x", padx=10, pady=(8, 4))

        tk.Label(fps_top, text="Preset:", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left")
        self.fps_preset_menu = ttk.Combobox(
            fps_top, textvariable=self.fps_preset_var,
            values=COMMON_FPS, width=10, state="readonly"
        )
        self.fps_preset_menu.pack(side="left", padx=(6, 16))
        self.fps_preset_menu.bind("<<ComboboxSelected>>", self._on_fps_preset)

        tk.Label(fps_top, text="Fine-tune:", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left")
        self.fps_spin = tk.Spinbox(
            fps_top, from_=1, to=240, increment=0.001,
            textvariable=self.fps_var, format="%.3f",
            width=8, bg=S["entry_bg"], fg=S["fg"],
            insertbackground=S["fg"], relief="flat",
            font=("Segoe UI", 10), command=self._on_fps_spin
        )
        self.fps_spin.pack(side="left", padx=4)
        self.fps_spin.bind("<KeyRelease>", lambda e: self._on_fps_spin())

        tk.Label(fps_top, text="fps", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left", padx=(2, 0))

        self.fps_value_lbl = tk.Label(fps_top, text="= source", width=16,
                                       bg=S["bg"], fg=S["accent"],
                                       font=("Segoe UI", 9, "italic"), anchor="e")
        self.fps_value_lbl.pack(side="right")

        fps_slider_row = tk.Frame(fps_frame, bg=S["bg"])
        fps_slider_row.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(fps_slider_row, text="1", bg=S["bg"], fg="#6c7086",
                 font=("Segoe UI", 8)).pack(side="left")
        self.fps_slider = ttk.Scale(fps_slider_row, from_=1, to=60,
                                     orient="horizontal",
                                     variable=self.fps_var,
                                     command=self._on_fps_slider)
        self.fps_slider.pack(side="left", fill="x", expand=True, padx=4)
        self.fps_max_lbl = tk.Label(fps_slider_row, text="60", bg=S["bg"],
                                     fg="#6c7086", font=("Segoe UI", 8))
        self.fps_max_lbl.pack(side="left")
        tk.Label(fps_frame, text="↑ slider is capped to source FPS",
                 bg=S["bg"], fg="#6c7086", font=("Segoe UI", 8)
                 ).pack(anchor="e", padx=12, pady=(0, 6))

        # ── Codec & settings ─────────────────────────────────────────────────────
        codec_frame = tk.LabelFrame(self, text=" Codec & Settings ", bg=S["bg"],
                                     fg=S["accent"], font=("Segoe UI", 10, "bold"),
                                     relief="groove", bd=2)
        codec_frame.pack(fill="x", **pad)

        codec_row = tk.Frame(codec_frame, bg=S["bg"])
        codec_row.pack(fill="x", padx=10, pady=8)
        tk.Label(codec_row, text="Codec:", bg=S["bg"],
                 fg=S["fg"], font=("Segoe UI", 10)).pack(side="left")

        self.codec_menu = ttk.Combobox(codec_row, textvariable=self.codec_var,
                                        values=CODEC_OPTIONS, width=22,
                                        state="readonly")
        self.codec_menu.pack(side="left", padx=8)

        self.detect_btn = tk.Button(
            codec_row, text="⚡ Auto-detect best codec",
            bg=S["btn_bg"], fg=S["fg"], activebackground=S["btn_active"],
            relief="flat", padx=10, font=("Segoe UI", 9),
            cursor="hand2", command=self._detect_codec
        )
        self.detect_btn.pack(side="left", padx=4)

        self.config_codec_lbl = tk.Label(
            codec_frame,
            text=f"Config codec: {self.config_data.get('preferred_codec') or 'not set'}",
            bg=S["bg"], fg="#6c7086", font=("Segoe UI", 8)
        )
        self.config_codec_lbl.pack(anchor="w", padx=14, pady=(0, 6))

        # ── Estimate ─────────────────────────────────────────────────────────────
        self.estimate_lbl = tk.Label(
            self, text="Estimated output size: —",
            bg=S["bg"], fg=S["green"],
            font=("Segoe UI", 11, "bold")
        )
        self.estimate_lbl.pack(pady=(8, 2))

        # ── Progress ─────────────────────────────────────────────────────────────
        self.progress_bar = ttk.Progressbar(
            self, variable=self.progress_var,
            maximum=100, length=400, mode="determinate"
        )
        self.progress_bar.pack(fill="x", padx=14, pady=(4, 2))

        self.status_lbl = tk.Label(
            self, text="Ready.",
            bg=S["bg"], fg="#6c7086",
            font=("Segoe UI", 9)
        )
        self.status_lbl.pack(pady=(0, 4))

        # ── Convert button ────────────────────────────────────────────────────────
        self.convert_btn = tk.Button(
            self, text="▶  Convert",
            bg=S["accent"], fg="#1e1e2e",
            activebackground="#74c7ec",
            relief="flat", padx=20, pady=10,
            font=("Segoe UI", 12, "bold"),
            cursor="hand2", command=self._start_convert
        )
        self.convert_btn.pack(pady=(4, 14))

        # Style ttk widgets
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TScale", background=S["bg"], troughcolor=S["btn_bg"],
                         sliderlength=18, sliderrelief="flat")
        style.configure("TCombobox", fieldbackground=S["entry_bg"],
                         background=S["entry_bg"], foreground=S["fg"],
                         selectbackground=S["entry_bg"])
        style.configure("Horizontal.TProgressbar",
                         troughcolor=S["btn_bg"], background=S["accent"],
                         thickness=16)

    # ── DnD ─────────────────────────────────────────────────────────────────────

    def _setup_dnd(self):
        if not _DND_AVAILABLE:
            return
        # Register entire window + the drop label as drop targets
        for widget in (self, self.drop_label):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        raw = event.data.strip()
        # TkinterDnD wraps paths with spaces in {braces}; handle both cases
        if raw.startswith("{") and raw.endswith("}"):
            path = raw[1:-1]
        else:
            # Multiple files: take just the first one
            path = raw.split()[0]
        # Strip file:// URI prefix if present (common on Wayland/XWayland)
        if path.startswith("file://"):
            from urllib.parse import unquote
            path = unquote(path[7:])
        self._load_file(path)

    # ── Path-paste fallback (works everywhere, especially Wayland) ───────────────

    def _load_from_paste(self):
        raw = self._paste_var.get().strip().strip('"').strip("'")
        if not raw:
            return
        # Strip file:// prefix
        if raw.startswith("file://"):
            from urllib.parse import unquote
            raw = unquote(raw[7:])
        self._load_file(raw)

    # ── File loading ─────────────────────────────────────────────────────────────

    def _browse_file(self):
        init_dir = self.config_data.get("last_directory", str(Path.home()))
        path = filedialog.askopenfilename(
            initialdir=init_dir,
            title="Select Video File",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts"),
                ("All files", "*.*"),
            ]
        )
        if path:
            self._load_file(path)

    def _load_file(self, path):
        if not os.path.isfile(path):
            messagebox.showerror("Error", f"File not found:\n{path}")
            return
        if not self.ffprobe_bin:
            messagebox.showerror("Error", "ffprobe not found in PATH.")
            return
        try:
            dur, w, h, fps, size = get_video_info(self.ffprobe_bin, path)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read video info:\n{e}")
            return

        self.input_file.set(path)
        self._paste_var.set("")  # clear paste box after successful load
        self.video_duration = dur
        self.video_fps = fps
        self.video_size_bytes = size
        self.video_width = w
        self.video_height = h

        self.file_label.config(text=path)
        size_mb = size / 1_048_576
        self.info_label.config(
            text=f"{w}×{h}  |  {fps:.2f} fps  |  {dur:.1f}s  |  {size_mb:.2f} MB"
        )

        # Update FPS slider max and reset controls to source
        fps_max = max(1.0, fps)
        self.fps_slider.config(to=fps_max)
        self.fps_max_lbl.config(text=f"{fps_max:.2f}")
        self.fps_var.set(round(fps, 3))
        self.fps_preset_var.set("source")
        self.fps_value_lbl.config(text="= source")

        # Save last directory
        self.config_data["last_directory"] = str(Path(path).parent)
        save_config(self.config_data)

        self._update_estimate()

    # ── Slider / spin callbacks ──────────────────────────────────────────────────

    def _on_pct_slider(self, val=None):
        pct = self.pct_var.get()
        self.pct_value_lbl.config(text=f"{pct:.0f}%")
        # Clear fixed-size spinbox to signal we use percentage
        self.target_size_var.set(0.0)
        self._update_estimate()

    def _on_size_spin(self):
        self._update_estimate()

    def _on_fps_slider(self, val=None):
        if self._ignore_fps_spin:
            return
        fps = self.fps_var.get()
        self._sync_fps_ui(fps, source="slider")

    def _on_fps_spin(self):
        if self._ignore_fps_spin:
            return
        try:
            fps = float(self.fps_var.get())
        except Exception:
            return
        fps = max(0.1, min(fps, self.video_fps if self.video_fps else 240))
        self._sync_fps_ui(fps, source="spin")

    def _on_fps_preset(self, event=None):
        preset = self.fps_preset_var.get()
        if preset == "source":
            fps = self.video_fps if self.video_fps else 30.0
        else:
            try:
                fps = float(preset)
            except ValueError:
                return
        fps = min(fps, self.video_fps if self.video_fps else fps)
        self._sync_fps_ui(fps, source="preset")

    def _sync_fps_ui(self, fps, source=""):
        """Update all FPS widgets to reflect a new fps value without re-entrancy."""
        self._ignore_fps_spin = True
        self.fps_var.set(round(fps, 3))
        src_fps = self.video_fps if self.video_fps else fps
        if abs(fps - src_fps) < 0.05:
            self.fps_value_lbl.config(text="= source")
        else:
            self.fps_value_lbl.config(text=f"↓ {fps:.3f} fps")
        self._ignore_fps_spin = False
        self._update_estimate()

    # ── Estimate ─────────────────────────────────────────────────────────────────

    def _calc_target_bytes(self):
        """Return the target byte count based on current UI state."""
        fixed = self.target_size_var.get()
        if fixed and fixed > 0:
            unit = self.size_unit_var.get()
            multipliers = {"KB": 1_024, "MB": 1_048_576, "GB": 1_073_741_824}
            return fixed * multipliers.get(unit, 1_048_576)
        else:
            pct = self.pct_var.get() / 100.0
            return self.video_size_bytes * pct

    def _update_estimate(self):
        if not self.video_size_bytes:
            self.estimate_lbl.config(text="Estimated output size: —")
            return

        target_bytes = self._calc_target_bytes()

        # FPS reduction: bitrate scales roughly proportional to fps
        fps_ratio = self.fps_var.get() / max(self.video_fps, 0.001)
        fps_ratio = min(fps_ratio, 1.0)

        # Estimated real size: target_bytes adjusted by fps ratio impact on video stream
        # Audio is typically ~128kbps fixed — estimate audio size
        audio_bytes = 128_000 / 8 * self.video_duration  # ~128kbps audio
        video_bytes = max(0, target_bytes - audio_bytes) * fps_ratio + audio_bytes

        # Also sync percentage label if using fixed size
        if self.target_size_var.get() and self.target_size_var.get() > 0:
            pct = (video_bytes / self.video_size_bytes) * 100
            self.pct_value_lbl.config(text=f"{pct:.0f}%")

        self.estimate_lbl.config(
            text=f"Estimated output size: {self._fmt_bytes(video_bytes)}  "
                 f"({video_bytes / self.video_size_bytes * 100:.0f}% of original)"
        )

    def _fmt_bytes(self, b):
        if b >= 1_073_741_824:
            return f"{b/1_073_741_824:.2f} GB"
        elif b >= 1_048_576:
            return f"{b/1_048_576:.2f} MB"
        elif b >= 1_024:
            return f"{b/1_024:.1f} KB"
        return f"{b:.0f} B"

    # ── Codec detection ──────────────────────────────────────────────────────────

    def _detect_codec(self):
        if not self.ffmpeg_bin:
            messagebox.showerror("Error", "ffmpeg not found in PATH.")
            return
        self.detect_btn.config(state="disabled", text="Detecting…")
        self.status_lbl.config(text="Testing codecs…")

        def run():
            codec = detect_best_codec(self.ffmpeg_bin)
            self.config_data["preferred_codec"] = codec
            save_config(self.config_data)
            self.after(0, lambda: self._codec_detected(codec))

        threading.Thread(target=run, daemon=True).start()

    def _codec_detected(self, codec):
        self.config_codec_lbl.config(
            text=f"Config codec: {codec}  ✅ saved to config"
        )
        self.detect_btn.config(state="normal", text="⚡ Auto-detect best codec")
        self.status_lbl.config(text=f"Detected best codec: {codec}")

    # ── Convert ──────────────────────────────────────────────────────────────────

    def _start_convert(self):
        if not self.input_file.get():
            messagebox.showwarning("No file", "Please select a video file first.")
            return
        if not self.ffmpeg_bin:
            messagebox.showerror("Error", "ffmpeg not found in PATH.")
            return
        if self.video_duration <= 0:
            messagebox.showerror("Error", "Could not determine video duration.")
            return

        # Resolve codec
        chosen = self.codec_var.get()
        if chosen.startswith("auto"):
            codec = self.config_data.get("preferred_codec") or "libx264"
        else:
            codec = chosen

        target_bytes = self._calc_target_bytes()
        if target_bytes <= 0:
            messagebox.showerror("Error", "Target size must be > 0.")
            return

        # Reserve ~128kbps for audio
        audio_bps = 128_000
        video_bytes = max(target_bytes - (audio_bps / 8 * self.video_duration), target_bytes * 0.8)
        target_bitrate_bps = (video_bytes * 8) / self.video_duration

        target_fps = self.fps_var.get()

        # Build output path
        inp = self.input_file.get()
        base, ext = os.path.splitext(inp)
        output_path = base + "_compressed" + ext

        cmd = build_ffmpeg_command(
            self.ffmpeg_bin, inp, output_path, codec,
            target_bitrate_bps, target_fps, self.video_fps
        )

        # Add progress parsing: -progress pipe:1
        cmd_with_progress = [cmd[0]] + cmd[1:]
        # Insert progress flag before output
        cmd_with_progress = (
            [self.ffmpeg_bin, "-y"] +
            cmd[2:-1] +  # everything between ffmpeg and output
            ["-progress", "pipe:1", "-nostats", output_path]
        )

        self.convert_btn.config(state="disabled")
        self.progress_var.set(0)
        self.status_lbl.config(text="Converting…")

        threading.Thread(
            target=self._run_ffmpeg,
            args=(cmd, output_path),
            daemon=True
        ).start()

    def _run_ffmpeg(self, cmd, output_path):
        # Rebuild command with -progress pipe:1
        # Insert -progress pipe:1 before the output file (last arg)
        prog_cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]

        try:
            proc = subprocess.Popen(
                prog_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            total_duration_us = self.video_duration * 1_000_000

            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = float(line.split("=")[1])
                        pct = min(us / total_duration_us * 100, 99.0)
                        self.after(0, lambda p=pct: self.progress_var.set(p))
                        self.after(0, lambda p=pct: self.status_lbl.config(
                            text=f"Converting… {p:.0f}%"
                        ))
                    except Exception:
                        pass

            proc.wait()
            stderr_out = proc.stderr.read()

            if proc.returncode == 0 and os.path.exists(output_path):
                out_size = os.path.getsize(output_path)
                self.after(0, lambda: self._on_success(output_path, out_size))
            else:
                # Try to extract useful error from stderr
                err_lines = [l for l in stderr_out.splitlines()
                             if "error" in l.lower() or "invalid" in l.lower()]
                err_msg = "\n".join(err_lines[-6:]) if err_lines else stderr_out[-600:]
                self.after(0, lambda: self._on_failure(err_msg))

        except Exception as e:
            self.after(0, lambda: self._on_failure(str(e)))

    def _on_success(self, output_path, out_size):
        self.progress_var.set(100)
        self.convert_btn.config(state="normal")
        orig = self.video_size_bytes
        ratio = out_size / orig * 100 if orig else 0
        self.status_lbl.config(
            text=f"✅ Done! Saved to: {os.path.basename(output_path)}"
        )
        messagebox.showinfo(
            "Conversion Complete",
            f"✅ Video compressed successfully!\n\n"
            f"Output: {output_path}\n\n"
            f"Original:  {self._fmt_bytes(orig)}\n"
            f"Output:    {self._fmt_bytes(out_size)}  ({ratio:.1f}% of original)"
        )

    def _on_failure(self, error_msg):
        self.progress_var.set(0)
        self.convert_btn.config(state="normal")
        self.status_lbl.config(text="❌ Conversion failed.")
        messagebox.showerror(
            "Conversion Failed",
            f"FFmpeg encountered an error:\n\n{error_msg}\n\n"
            "Tips:\n"
            "• Try 'auto-detect best codec' to pick a compatible encoder\n"
            "• Make sure the input file is a valid video\n"
            "• Check that you have write permissions to the output folder"
        )

    # ── FFmpeg check ─────────────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        if not self.ffmpeg_bin or not self.ffprobe_bin:
            missing = []
            if not self.ffmpeg_bin:
                missing.append("ffmpeg")
            if not self.ffprobe_bin:
                missing.append("ffprobe")
            messagebox.showwarning(
                "FFmpeg Not Found",
                f"The following tools were not found in your PATH:\n  {', '.join(missing)}\n\n"
                "Please install FFmpeg and ensure it is accessible from the command line."
            )


# ─── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
