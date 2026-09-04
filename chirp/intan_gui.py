#!/usr/bin/env python3
"""
Intan .dat -> audio / spike video, with a point-and-click front end.

A tkinter GUI (standard library, nothing to install) over dat_to_audio.py and
dat_to_video.py. Browse to a recording folder, tick the channels, set the
parameters, press Run. Jobs execute on a worker thread so the window stays
responsive and can be cancelled mid-render.

Run directly with `python intan_gui.py`, or build the double-clickable
executable with `python build_exe.py`.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# When frozen by PyInstaller the engine modules sit alongside this file inside
# the bundle; when run from source they sit next to it on disk. Both work
# because the bundle directory is already on sys.path, but a source checkout
# launched from elsewhere needs the nudge.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dat_to_audio as eng_audio
import dat_to_video as eng_video

APP_TITLE = "CHIRP GUI"
PAD = dict(padx=6, pady=3)

# Statistics are averaged over this many of the best-scoring windows per
# channel, rather than the single one that gets rendered - one excerpt is a
# thin basis for a firing rate. Rendering still uses only the best of them.
STAT_SEGMENTS = 3


# --------------------------------------------------------------------- utils --
def find_ffmpeg() -> str | None:
    """Look beside the executable and in the bundle before falling back to PATH."""
    import shutil
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    candidates = [here / "ffmpeg.exe", here / "ffmpeg"]
    if hasattr(sys, "_MEIPASS"):
        candidates += [Path(sys._MEIPASS) / "ffmpeg.exe",
                       Path(sys._MEIPASS) / "ffmpeg"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("ffmpeg")


class QueueWriter:
    """File-like object that funnels worker stdout into the GUI log queue."""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, text):
        self._buf += text
        while True:
            i = max(self._buf.find("\n"), self._buf.find("\r"))
            if i < 0:
                break
            line, self._buf = self._buf[:i], self._buf[i + 1:]
            if line.strip():
                self.q.put(("log", line))
        return len(text)

    def flush(self):
        if self._buf.strip():
            self.q.put(("log", self._buf))
        self._buf = ""


# ----------------------------------------------------------------------- app --
class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.folder: Path | None = None
        self.paths: list[Path] = []

        self._build_folder_row()
        self._build_middle()
        self._build_run_row()
        self._poll_queue()

        ff = find_ffmpeg()
        self.v_ffmpeg.set(ff or "")
        self.log(f"{APP_TITLE} ready.")
        if ff:
            self.log(f"ffmpeg: {ff}")
        else:
            self.log("ffmpeg NOT found. WAV export is possible but MP4 export is not. "
                     "Bring a ffmpeg.exe next to this file, or set the path below.")

    # ------------------------------------------------------------ layout --
    def _build_folder_row(self):
        f = ttk.LabelFrame(self, text="Data folder", padding=6)
        f.grid(row=0, column=0, sticky="ew", **PAD)
        f.columnconfigure(1, weight=1)
        ttk.Button(f, text="Browse...", command=self.pick_folder) \
            .grid(row=0, column=0, **PAD)
        self.v_folder = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_folder, state="readonly") \
            .grid(row=0, column=1, sticky="ew", **PAD)

    def _build_middle(self):
        mid = ttk.Frame(self)
        mid.grid(row=1, column=0, sticky="nsew")
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(0, weight=1)

        # ---- file list -------------------------------------------------
        lf = ttk.LabelFrame(mid, text="Channels (.dat)", padding=6)
        lf.grid(row=0, column=0, sticky="nsew", **PAD)
        lf.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(lf, selectmode="extended", width=34,
                                  exportselection=False, activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.config(yscrollcommand=sb.set)
        btns = ttk.Frame(lf)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="All", width=6,
                   command=lambda: self.listbox.select_set(0, tk.END)) \
            .pack(side="left", padx=2)
        ttk.Button(btns, text="None", width=6,
                   command=lambda: self.listbox.select_clear(0, tk.END)) \
            .pack(side="left", padx=2)
        self.lbl_count = ttk.Label(btns, text="")
        self.lbl_count.pack(side="right")

        # ---- parameter tabs --------------------------------------------
        nb = ttk.Notebook(mid)
        nb.grid(row=0, column=1, sticky="nsew", **PAD)
        self._tab_common(nb)
        self._tab_audio(nb)
        self._tab_video(nb)
        self._tab_output(nb)

        # ---- statistics table, right hand side -------------------------
        mid.columnconfigure(2, weight=2)            # the table gets the room
        self._build_table(mid)

    def _build_table(self, parent):
        """Per-cluster statistics, filled in as each channel is analysed."""
        tf = ttk.LabelFrame(parent, text="Cluster statistics", padding=6)
        tf.grid(row=0, column=2, sticky="nsew", **PAD)
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)

        cols = [("channel", "channel", 96, "w"), ("segment", "seg", 34, "center"),
                ("start_s", "start s", 60, "e"),
                ("cluster", "cl", 30, "center"),
                ("n_spikes", "n", 46, "e"),
                ("mean_amplitude_uV", "amp uV", 58, "e"),
                ("firing_rate_sp_s", "rate sp/s", 64, "e"),
                ("snr", "SNR", 44, "e"),
                ("half_width_ms", "HW ms", 52, "e"),
                ("trough_to_peak_ms", "t2p ms", 54, "e")]
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in cols],
                                 show="headings", height=14)
        for key, title, width, anchor in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        # Rows are tinted by cluster index, matching the video's pane colours.
        for j, colour in enumerate(eng_video.CLUSTER_COLORS):
            self.tree.tag_configure(f"c{j}", foreground=colour)

        row = ttk.Frame(tf)
        row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(row, text="Clear", width=7,
                   command=lambda: self.tree.delete(*self.tree.get_children())) \
            .pack(side="left", padx=2)
        self.btn_report = ttk.Button(row, text="Show report", width=12,
                                     command=self._reopen_report,
                                     state="disabled")
        self.btn_report.pack(side="left", padx=2)
        self.lbl_rows = ttk.Label(row, text="")
        self.lbl_rows.pack(side="right")

    def _add_stat_rows(self, rows):
        for r in rows:
            self.tree.insert("", tk.END, tags=(f"c{(r['cluster'] - 1) % 3}",),
                             values=(r["channel"], r["segment"],
                                     f"{r['start_s']:.0f}",
                                     r["cluster"], r["n_spikes"],
                                     f"{r['mean_amplitude_uV']:.1f}",
                                     f"{r['firing_rate_sp_s']:.1f}",
                                     f"{r['snr']:.1f}",
                                     f"{r['half_width_ms']:.3f}",
                                     f"{r['trough_to_peak_ms']:.2f}"))
        self.tree.yview_moveto(1.0)
        self.lbl_rows.config(text=f"{len(self.tree.get_children())} cluster(s)")

    def _row(self, parent, r, label, var, width=12, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", **PAD)
        ttk.Entry(parent, textvariable=var, width=width) \
            .grid(row=r, column=1, sticky="w", **PAD)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666") \
                .grid(row=r, column=2, sticky="w", **PAD)

    def _tab_common(self, nb):
        t = ttk.Frame(nb, padding=8)
        nb.add(t, text="Signal")
        self.v_start = tk.StringVar(value="")
        self.v_dur = tk.StringVar(value="10")
        self.v_lo = tk.StringVar(value=str(int(eng_audio.DEFAULT_BAND[0])))
        self.v_hi = tk.StringVar(value=str(int(eng_audio.DEFAULT_BAND[1])))
        self.v_fs = tk.StringVar(value=str(eng_audio.SAMPLE_RATE))
        self._row(t, 0, "Start time (s)", self.v_start,
                  hint="Leave empty to scan for a clean window")
        self._row(t, 1, "Window duration (s)", self.v_dur)
        self._row(t, 2, "Band-pass low (Hz)", self.v_lo)
        self._row(t, 3, "Band-pass high (Hz)", self.v_hi,
                  hint="must be below Acquisition rate/2")
        self._row(t, 4, "Acquisition rate (Hz)", self.v_fs,
                  hint="from settings.xml")

    def _tab_audio(self, nb):
        t = ttk.Frame(nb, padding=8)
        nb.add(t, text="Audio")
        self.v_resample = tk.StringVar(value="")
        self.v_bits = tk.StringVar(value="16")
        self.v_headroom = tk.StringVar(value="1.0")
        self._row(t, 0, "Resample to (Hz)", self.v_resample,
                  hint="Empty to keep Acquisition rate")
        ttk.Label(t, text="Bit depth").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Combobox(t, textvariable=self.v_bits, values=["16", "32"],
                     width=9, state="readonly") \
            .grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(t, text="16 = PCM, 32 = IEEE float",
                  foreground="#666").grid(row=1, column=2, sticky="w", **PAD)
        self._row(t, 2, "Headroom (dB)", self.v_headroom)

    def _tab_video(self, nb):
        t = ttk.Frame(nb, padding=8)
        nb.add(t, text="Video / spikes")
        self.v_fps = tk.StringVar(value="60")
        self.v_negk = tk.StringVar(value="5.0")
        self.v_posk = tk.StringVar(value="8.5")
        self.v_ylim = tk.StringVar(value="100")
        self.v_w = tk.StringVar(value="1600")
        self.v_h = tk.StringVar(value="900")
        self.v_slow = tk.StringVar(value="1")
        self.v_crf = tk.StringVar(value="16")
        self.v_wavefrac = tk.StringVar(value="0.26")
        self.v_artifactk = tk.StringVar(value="18")
        self.v_maxk = tk.StringVar(value="3")
        self._row(t, 0, "Frame rate (fps)", self.v_fps)
        self._row(t, 1, "Detection threshold (sigma)", self.v_negk,
                  hint="negative crossing")
        self._row(t, 2, "Rejection threshold (sigma)", self.v_posk,
                  hint="positive crossing")
        self._row(t, 3, "Voltage scale (+/- uV)", self.v_ylim)
        ttk.Label(t, text="Frame size").grid(row=4, column=0, sticky="w", **PAD)
        fr = ttk.Frame(t)
        fr.grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Entry(fr, textvariable=self.v_w, width=6).pack(side="left")
        ttk.Label(fr, text=" x ").pack(side="left")
        ttk.Entry(fr, textvariable=self.v_h, width=6).pack(side="left")
        self._row(t, 5, "Slow motion factor", self.v_slow, hint="1 = real time")
        self._row(t, 6, "x264 quality (CRF)", self.v_crf, hint="lower = better")
        self._row(t, 7, "Cluster pane width", self.v_wavefrac,
                  hint="fraction of frame, same for every pane")
        self._row(t, 8, "Artifact scan threshold (sigma)", self.v_artifactk,
                  hint="scan calls a window clean below this")
        ttk.Label(t, text="Max clusters").grid(row=9, column=0, sticky="w",
                                               **PAD)
        ttk.Combobox(t, textvariable=self.v_maxk, values=["1", "2", "3"],
                     width=9, state="readonly") \
            .grid(row=9, column=1, sticky="w", **PAD)
        ttk.Label(t, text="k-means tries 1 up to this many",
                  foreground="#666").grid(row=9, column=2, sticky="w", **PAD)

    def _tab_output(self, nb):
        t = ttk.Frame(nb, padding=8)
        nb.add(t, text="Output")
        t.columnconfigure(1, weight=1)
        self.v_out = tk.StringVar(value="")
        self.v_name = tk.StringVar(value="{stem}_{start}s+{dur}s_{lo}-{hi}Hz")
        self.v_wav = tk.BooleanVar(value=False)
        self.v_mp4 = tk.BooleanVar(value=True)
        self.v_stats = tk.BooleanVar(value=True)
        self.v_ffmpeg = tk.StringVar(value="")

        ttk.Label(t, text="Output folder").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(t, textvariable=self.v_out).grid(row=0, column=1,
                                                   sticky="ew", **PAD)
        ttk.Button(t, text="...", width=3, command=self.pick_out) \
            .grid(row=0, column=2, **PAD)
        ttk.Label(t, text="Output name").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(t, textvariable=self.v_name).grid(row=1, column=1,
                                                    sticky="ew", **PAD)
        ttk.Label(t, text="placeholders: {stem} {start} {dur} {lo} {hi} {fs}",
                  foreground="#666").grid(row=2, column=1, sticky="w", **PAD)
        ttk.Checkbutton(t, text="Export audio (.wav)", variable=self.v_wav) \
            .grid(row=3, column=1, sticky="w", **PAD)
        ttk.Checkbutton(t, text="Export video (.mp4)", variable=self.v_mp4) \
            .grid(row=4, column=1, sticky="w", **PAD)
        ttk.Checkbutton(t, text="Cluster statistics (CSV + report)",
                        variable=self.v_stats) \
            .grid(row=5, column=1, sticky="w", **PAD)
        ttk.Label(t, text="writes chirp_cluster_stats.csv",
                  foreground="#666").grid(row=6, column=1, sticky="w", **PAD)
        ttk.Label(t, text="ffmpeg").grid(row=7, column=0, sticky="w", **PAD)
        ttk.Entry(t, textvariable=self.v_ffmpeg).grid(row=7, column=1,
                                                      sticky="ew", **PAD)
        ttk.Button(t, text="...", width=3, command=self.pick_ffmpeg) \
            .grid(row=7, column=2, **PAD)

    def _build_run_row(self):
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", **PAD)
        bar.columnconfigure(2, weight=1)
        self.btn_run = ttk.Button(bar, text="Run", command=self.start)
        self.btn_run.grid(row=0, column=0, **PAD)
        self.btn_cancel = ttk.Button(bar, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.btn_cancel.grid(row=0, column=1, **PAD)
        self.prog = ttk.Progressbar(bar, mode="determinate", maximum=1000)
        self.prog.grid(row=0, column=2, sticky="ew", **PAD)
        self.lbl_status = ttk.Label(bar, text="idle", width=28)
        self.lbl_status.grid(row=0, column=3, **PAD)

        lf = ttk.LabelFrame(self, text="Log", padding=4)
        lf.grid(row=3, column=0, sticky="nsew", **PAD)
        self.rowconfigure(3, weight=1)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.txt = tk.Text(lf, height=11, wrap="none", font=("Consolas", 9))
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.config(yscrollcommand=sb.set, state="disabled")

    # ------------------------------------------------------------ actions --
    def pick_folder(self):
        d = filedialog.askdirectory(title="Select the raw data folder")
        if not d:
            return
        self.folder = Path(d)
        self.v_folder.set(d)
        if not self.v_out.get():
            self.v_out.set(str(self.folder / "converted"))
        self.refresh_files()

    def pick_out(self):
        d = filedialog.askdirectory(title="Select the output folder")
        if d:
            self.v_out.set(d)

    def pick_ffmpeg(self):
        f = filedialog.askopenfilename(title="Locate ffmpeg.exe",
                                       filetypes=[("ffmpeg", "ffmpeg.exe"),
                                                  ("All files", "*.*")])
        if f:
            self.v_ffmpeg.set(f)

    def refresh_files(self):
        self.listbox.delete(0, tk.END)
        # Intan drops non-amplifier .dat files beside the channels (time.dat,
        # board-DIGITAL-IN-*.dat, supply.dat). Those are not signal channels
        # and would decode to nonsense, so list amp-*.dat and fall back to
        # every .dat only if a folder happens to have none.
        amp = sorted(self.folder.glob("amp-*.dat"))
        self.paths = amp or sorted(self.folder.glob("*.dat"))
        fallback = not amp and bool(self.paths)
        try:
            fs = int(float(self.v_fs.get()))
        except ValueError:
            fs = eng_audio.SAMPLE_RATE
        for p in self.paths:
            mins = eng_audio.file_duration_s(p, fs) / 60.0
            self.listbox.insert(tk.END, f"{p.name}   ({mins:.1f} min)")
        self.lbl_count.config(text=f"{len(self.paths)} file(s)")
        if not self.paths:
            self.log(f"No .dat files in {self.folder}")
        elif fallback:
            self.log(f"{len(self.paths)} .dat file(s) in {self.folder} "
                     f"- no amp-*.dat found, so showing every .dat")
        else:
            self.log(f"{len(self.paths)} channel file(s) in {self.folder}")

    def log(self, msg):
        self.txt.config(state="normal")
        self.txt.insert(tk.END, msg.rstrip() + "\n")
        self.txt.see(tk.END)
        self.txt.config(state="disabled")

    # ----------------------------------------------------------- validate --
    def _num(self, var, name, cast=float, allow_blank=False):
        raw = var.get().strip()
        if not raw:
            if allow_blank:
                return None
            raise ValueError(f"{name} is required")
        try:
            return cast(raw)
        except ValueError:
            raise ValueError(f"{name}: '{raw}' is not a number")

    def gather(self):
        if not self.folder:
            raise ValueError("Choose a recording folder first")
        sel = self.listbox.curselection()
        if not sel:
            raise ValueError("Select at least one .dat file")
        if not (self.v_wav.get() or self.v_mp4.get() or self.v_stats.get()):
            raise ValueError("Tick at least one of the output options")

        cfg = dict(
            files=[self.paths[i] for i in sel],
            start=self._num(self.v_start, "Start time", allow_blank=True),
            dur=self._num(self.v_dur, "Duration"),
            lo=self._num(self.v_lo, "Band-pass low"),
            hi=self._num(self.v_hi, "Band-pass high"),
            fs=self._num(self.v_fs, "Acquisition rate", int),
            resample=self._num(self.v_resample, "Resample", int, True),
            bits=self._num(self.v_bits, "Bit depth", int),
            headroom=self._num(self.v_headroom, "Headroom"),
            fps=self._num(self.v_fps, "Frame rate", int),
            negk=self._num(self.v_negk, "Detection threshold"),
            posk=self._num(self.v_posk, "Rejection threshold"),
            ylim=self._num(self.v_ylim, "Overview scale"),
            w=self._num(self.v_w, "Frame width", int),
            h=self._num(self.v_h, "Frame height", int),
            slow=self._num(self.v_slow, "Slow motion factor"),
            crf=self._num(self.v_crf, "CRF", int),
            wavefrac=self._num(self.v_wavefrac, "Cluster pane width"),
            artifactk=self._num(self.v_artifactk, "Artifact scan threshold"),
            maxk=self._num(self.v_maxk, "Max clusters", int),
            out=Path(self.v_out.get().strip() or (self.folder / "converted")),
            name=self.v_name.get().strip() or "{stem}_{start}s+{dur}s_{lo}-{hi}Hz",
            wav=self.v_wav.get(), mp4=self.v_mp4.get(),
            stats=self.v_stats.get(),
            ffmpeg=self.v_ffmpeg.get().strip(),
        )
        if cfg["dur"] <= 0:
            raise ValueError("Duration must be positive")
        if not 1 <= cfg["maxk"] <= 3:
            raise ValueError("Max clusters must be 1, 2 or 3")
        if cfg["artifactk"] <= 0:
            raise ValueError("Artifact scan threshold must be positive")
        if not 0 < cfg["lo"] < cfg["hi"] < cfg["fs"] / 2:
            raise ValueError(f"Band-pass must satisfy 0 < low < high < "
                             f"{cfg['fs'] / 2:.0f} Hz (Nyquist)")
        if cfg["mp4"] and not cfg["ffmpeg"]:
            raise ValueError("MP4 export needs ffmpeg. Set its path on the "
                             "Output tab, or untick 'Export video'")
        return cfg

    # -------------------------------------------------------------- worker --
    def start(self):
        try:
            cfg = self.gather()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.cancel_flag.clear()
        if cfg["stats"]:                    # a run owns the table it fills
            self.tree.delete(*self.tree.get_children())
            self.lbl_rows.config(text="")
        self.btn_run.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.prog["value"] = 0
        self.worker = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_flag.set()
        self.lbl_status.config(text="cancelling...")

    def _run(self, cfg):
        """Runs on the worker thread. Talks to the GUI only through self.q."""
        writer = QueueWriter(self.q)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer
        if cfg["ffmpeg"]:
            # dat_to_video resolves ffmpeg via PATH; prepend ours for this run.
            os.environ["PATH"] = (str(Path(cfg["ffmpeg"]).parent) + os.pathsep
                                  + os.environ.get("PATH", ""))
        per_file = int(cfg["wav"]) + int(cfg["mp4"]) + int(cfg["stats"])
        n_jobs = max(1, len(cfg["files"]) * per_file)
        done = 0
        all_rows = []
        band = (cfg["lo"], cfg["hi"])
        try:
            cfg["out"].mkdir(parents=True, exist_ok=True)
            for path in cfg["files"]:
                if self.cancel_flag.is_set():
                    raise eng_video.Cancelled("cancelled")
                self.q.put(("status", path.name))

                # Statistics run over the best STAT_SEGMENTS windows, so they
                # describe the channel rather than one arbitrary excerpt.
                # Rendering still uses only the single best of them.
                segments = [cfg["start"]] if cfg["start"] is not None else []
                if not segments and (cfg["mp4"] or cfg["stats"]):
                    want = STAT_SEGMENTS if cfg["stats"] else 1
                    segments = eng_video.find_best_windows(
                        path, cfg["dur"], band, cfg["fs"],
                        60.0, cfg["artifactk"], cfg["negk"], cfg["posk"],
                        1.0, 2.0, 1.0, None, True, True,
                        cancel=self.cancel_flag.is_set,
                        progress=lambda f, d=done: self.q.put(
                            ("prog", (d + f) / n_jobs)),
                        max_k=cfg["maxk"], top_n=want)
                if not segments:
                    segments = [(eng_audio.file_duration_s(path, cfg["fs"])
                                 - cfg["dur"]) / 2]
                # Everything rendered or exported describes segments[0].
                eff_start = segments[0]
                name = self._format_name(cfg, path, eff_start)

                if cfg["stats"]:
                    for rank, seg_start in enumerate(segments, start=1):
                        if self.cancel_flag.is_set():
                            raise eng_video.Cancelled("cancelled")
                        rows, _ = eng_video.channel_stats(
                            path, seg_start, cfg["dur"], band, cfg["fs"],
                            cfg["negk"], cfg["posk"], max_k=cfg["maxk"],
                            segment=rank)
                        all_rows += rows
                        self.q.put(("stats", rows))
                        print(f"{path.name} segment {rank} "
                              f"({seg_start:.0f} s): {len(rows)} cluster(s) - "
                              + "; ".join(
                                  f"#{r['cluster']} "
                                  f"{r['mean_amplitude_uV']:.0f} uV, "
                                  f"{r['firing_rate_sp_s']:.1f} sp/s, "
                                  f"SNR {r['snr']:.1f}" for r in rows))
                    done += 1
                    self.q.put(("prog", done / n_jobs))

                if cfg["wav"]:
                    eng_audio.convert(
                        path, cfg["out"], cfg["dur"], eff_start, band,
                        cfg["fs"], cfg["resample"], cfg["bits"],
                        cfg["headroom"], True, out_name=name + ".wav")
                    done += 1
                    self.q.put(("prog", done / n_jobs))

                if cfg["mp4"]:
                    eng_video.render(
                        path, cfg["out"], cfg["dur"], eff_start, band,
                        cfg["fs"], cfg["fps"],
                        (cfg["w"], cfg["h"]), 100, cfg["slow"], cfg["crf"],
                        cfg["bits"], cfg["headroom"], cfg["ylim"],
                        cfg["negk"], cfg["posk"], 1.0, 2.0, 1.0,
                        cfg["wavefrac"], False,
                        out_stem=name,
                        cancel=self.cancel_flag.is_set,
                        progress=lambda f, d=done: self.q.put(
                            ("prog", (d + f) / n_jobs)),
                        max_k=cfg["maxk"])
                    done += 1
                    self.q.put(("prog", done / n_jobs))

            if cfg["stats"] and all_rows:
                s_csv = eng_video.write_stats_csv(
                    all_rows, cfg["out"] / "chirp_cluster_stats.csv")
                print(f"  -> {s_csv.name} ({len(all_rows)} rows)")
                self.q.put(("report", self._build_report(all_rows, cfg,
                                                         [s_csv])))
            self.q.put(("done", f"Finished. {done} job(s), output in "
                                f"{cfg['out']}"))
        except eng_video.Cancelled:
            self.q.put(("done", "Cancelled."))
        except Exception as exc:
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("done", f"FAILED: {exc}"))
        finally:
            writer.flush()
            sys.stdout, sys.stderr = old_out, old_err

    # -------------------------------------------------------------- report --
    @staticmethod
    def _build_report(rows, cfg, files):
        """Plain-text summary of the whole run, shown at the end and logged."""
        import statistics as st
        from collections import Counter

        # Cluster count is a property of one excerpt, so tally it per
        # channel x segment rather than collapsing it onto the channel.
        per_seg = {}
        for r in rows:
            per_seg[(r["channel"], r["segment"])] = r["n_clusters"]
        kdist = Counter(per_seg.values())
        channels = {r["channel"] for r in rows}

        def pm(key, fmt):
            vals = [r[key] for r in rows]
            m = st.fmean(vals)
            sd = st.pstdev(vals) if len(vals) > 1 else 0.0
            return f"{fmt.format(m)} +/- {fmt.format(sd)}"

        L = []
        L.append("CHIRP - cluster statistics report")
        L.append("=" * 60)
        L.append(f"channels analysed   : {len(channels)}")
        L.append(f"segments analysed   : {len(per_seg)} "
                 f"({len(per_seg) / max(1, len(channels)):.1f} per channel)")
        L.append(f"excerpt             : {cfg['dur']:.0f} s" + (
            f" from {cfg['start']:.0f} s" if cfg["start"] is not None
            else f", best {STAT_SEGMENTS} non-overlapping windows per channel"
                 f" (the first was rendered)"))
        L.append(f"band-pass           : {cfg['lo']:.0f}-{cfg['hi']:.0f} Hz")
        L.append(f"detect / reject     : -{cfg['negk']:g} sigma / "
                 f"+{cfg['posk']:g} sigma")
        L.append(f"artifact scan       : {cfg['artifactk']:g} sigma")
        L.append(f"max clusters        : {cfg['maxk']}")
        L.append("")
        L.append("clusters per segment : " + ", ".join(
            f"{k} cluster(s) x {v} segment(s)" for k, v in sorted(kdist.items())))
        L.append(f"cluster rows in total: {len(rows)}")
        L.append(f"spikes in total      : {sum(r['n_spikes'] for r in rows)}")
        L.append("")
        L.append("across all cluster rows (mean +/- sd):")
        L.append(f"  mean amplitude     : {pm('mean_amplitude_uV', '{:.1f}')} uV")
        L.append(f"  firing rate        : {pm('firing_rate_sp_s', '{:.1f}')} sp/s")
        L.append(f"  SNR                : {pm('snr', '{:.1f}')}")
        L.append(f"  half-width         : {pm('half_width_ms', '{:.3f}')} ms")
        L.append(f"  trough-to-peak     : {pm('trough_to_peak_ms', '{:.2f}')} ms")
        L.append(f"  noise sigma        : {pm('sigma_uV', '{:.2f}')} uV")
        L.append("")
        L.append("written:")
        for f in files:
            L.append(f"  {f}")
        L.append("")
        L.append("note: half-width and trough-to-peak shift with the")
        L.append("      band-pass, so compare them only between recordings")
        L.append("      filtered the same way.")
        return "\n".join(L)

    def _reopen_report(self):
        if getattr(self, "_last_report", None):
            self._show_report(self._last_report)

    def _show_report(self, text):
        self._last_report = text
        self.btn_report.config(state="normal")
        win = tk.Toplevel(self)
        win.title("CHIRP - final report")
        win.geometry("620x560")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        txt = tk.Text(win, wrap="none", font=("Consolas", 9))
        txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        txt.config(yscrollcommand=sb.set)
        txt.insert("1.0", text)
        txt.config(state="disabled")
        bar = ttk.Frame(win)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)

        def copy():
            self.clipboard_clear()
            self.clipboard_append(text)

        ttk.Button(bar, text="Copy", command=copy).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right",
                                                                padx=6)

    @staticmethod
    def _format_name(cfg, path, start):
        s = 0 if start is None else start
        try:
            return cfg["name"].format(
                stem=path.stem, start=f"{s:.0f}", dur=f"{cfg['dur']:.0f}",
                lo=f"{cfg['lo']:.0f}", hi=f"{cfg['hi']:.0f}", fs=cfg["fs"])
        except (KeyError, IndexError, ValueError):
            # An unknown placeholder should not abort a long batch.
            return f"{path.stem}_{s:.0f}s+{cfg['dur']:.0f}s"

    # --------------------------------------------------------- gui pumping --
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "prog":
                    self.prog["value"] = max(0, min(1000, int(payload * 1000)))
                elif kind == "status":
                    self.lbl_status.config(text=payload)
                elif kind == "stats":
                    self._add_stat_rows(payload)
                elif kind == "report":
                    self.log("")
                    self.log(payload)
                    self._show_report(payload)
                elif kind == "done":
                    self.log(payload)
                    self.lbl_status.config(text="idle")
                    self.prog["value"] = 0
                    self.btn_run.config(state="normal")
                    self.btn_cancel.config(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)


def _selftest(work_dir: str, dat: str) -> int:
    """
    Headless smoke test, used to verify a frozen build.

        IntanConverter.exe --selftest <out_dir> <file.dat>

    A windowed build has no console, so everything goes to selftest.log inside
    <out_dir>. Exercises the real conversion path: read, filter, detect, WAV,
    and an ffmpeg-muxed MP4.
    """
    out = Path(work_dir)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "selftest.log"
    with open(log, "w", encoding="utf-8") as fh:
        sys.stdout = sys.stderr = fh
        try:
            print(f"frozen={getattr(sys, 'frozen', False)}  exe={sys.executable}")
            print(f"ffmpeg={find_ffmpeg()}")
            ff = find_ffmpeg()
            if ff:
                os.environ["PATH"] = (str(Path(ff).parent) + os.pathsep
                                      + os.environ.get("PATH", ""))
            p = Path(dat)
            eng_audio.convert(p, out, 2.0, 6000.0, (700.0, 8000.0), 30000,
                              None, 16, 1.0, True, out_name="selftest.wav")
            eng_video.render(p, out, 2.0, 6000.0, (700.0, 8000.0), 30000, 30,
                             (800, 480), 100, 1.0, 23, 16, 1.0, 100.0,
                             6.0, 7.0, 1.0, 2.0, 1.0, 0.26, False,
                             out_stem="selftest")
            ok = (out / "selftest.wav").is_file() and (out / "selftest.mp4").is_file()
            print("SELFTEST OK" if ok else "SELFTEST FAILED: outputs missing")
            return 0 if ok else 1
        except Exception:
            traceback.print_exc()
            print("SELFTEST FAILED")
            return 1


def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return _selftest(sys.argv[i + 1], sys.argv[i + 2])

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1560x820")                # the statistics table needs room
    root.minsize(1100, 660)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    raise SystemExit(main())
