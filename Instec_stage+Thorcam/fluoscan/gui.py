"""Tkinter GUI: live camera view, ROI, dark frame, protocol settings, live plots, safety buttons."""
import json
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.widgets import RectangleSelector  # noqa: E402

from .config import (DEFAULT_PORT, DEFAULT_SAVE_DIR, DISPLAY_BIN, HARD_MAX_TEMP,  # noqa: E402
                     MAX_RAMP_RATE, PROTOCOL_FIELDS, console)
from .stage import Stage, emergency_all_off  # noqa: E402
from .camera import CameraThread  # noqa: E402
from .scan import ScanWorker, build_schedule, build_setpoints  # noqa: E402

# ============================================================
# GUI
# ============================================================
class App:
    def __init__(self, root, simulate=False):
        self.root, self.sim = root, simulate
        self.q = queue.Queue()
        self.running = self.closing = self.destroyed = False
        self.worker = self.stage = self.abort_evt = self.camera = None
        self.roi = None
        self.cam_info_loaded = False
        self.t_data, self.tsp_data, self.pv_data, self.points = [], [], [], []
        self.plot_dirty, self.last_plot, self.last_cam, self.last_cam_count = False, 0.0, 0.0, -1
        self.run_dir, self.log_fh, self.run_error = None, None, None
        self.dark, self.dark_info, self.dark_busy = None, None, False   # per-pixel dark frame

        self._build_ui()
        saved = self.load_settings()
        self._apply_mode_state()
        self._start_camera(saved.get("camera") or None)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._tk_exception
        root.after(100, self._poll)

    # ---------------- logging (thread-safe) ----------------
    def log(self, s):
        console(s)
        self.q.put(("log", s))

    def _append_log(self, s):
        line = f"[{datetime.now():%H:%M:%S}] {s}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        if self.log_fh:
            self.log_fh.write(line)
            self.log_fh.flush()

    # ---------------- remembered settings ----------------
    def _settings_path(self):
        name = "last_settings_sim.json" if self.sim else "last_settings.json"
        # stored next to temp_fluorescence_scan.py (one level above this package)
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)

    def _text_vars(self):
        """Entry fields remembered between sessions (stored as the text you typed)."""
        v = {f"protocol.{k}": var for k, var in self.pvars.items()}
        v.update({"port": self.port_var, "exposure_ms": self.exp_var, "n_avg": self.navg_var,
                  "n_dark": self.ndark_var, "save_dir": self.dir_var, "sample": self.sample_var,
                  "comments": self.comment_var})
        return v

    def save_settings(self):
        try:
            s = {k: var.get() for k, var in self._text_vars().items()}
            try:
                s["gain"] = self._gain()
            except ValueError:
                pass
            s["mode"] = self.mode_var.get()
            s["camera"] = self.cam_var.get()
            s["auto_contrast"] = bool(self.autocon_var.get())
            s["live_dark"] = bool(self.live_dark_var.get())
            s["roi"] = list(self.roi) if self.roi else None
            path = self._settings_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            self.log(f"Could not save settings: {e}")

    def load_settings(self):
        path = self._settings_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except Exception as e:
            self.log(f"Could not read saved settings ({e}) - using defaults.")
            return {}
        for k, var in self._text_vars().items():
            if k in s and s[k] is not None:
                var.set(str(s[k]))
        if s.get("mode") in ("step", "ramp"):
            self.mode_var.set(s["mode"])
        if isinstance(s.get("gain"), int):
            self.gain_var.set(str(s["gain"]))
        for k, var in (("auto_contrast", self.autocon_var), ("live_dark", self.live_dark_var)):
            if isinstance(s.get(k), bool):
                var.set(s[k])
        roi = s.get("roi")
        if isinstance(roi, list) and len(roi) == 4:
            try:
                x0, y0, x1, y1 = (int(v) for v in roi)
                if x1 - x0 >= 2 and y1 - y0 >= 2 and x0 >= 0 and y0 >= 0:
                    self.roi = (x0, y0, x1, y1)
                    self.roi_var.set(f"ROI: x {x0}-{x1}, y {y0}-{y1}  ({x1-x0}×{y1-y0} px)  [restored]")
                    try:
                        self.selector.extents = (x0, x1, y0, y1)
                        self.selector.set_visible(True)
                    except Exception:
                        pass
            except (TypeError, ValueError):
                pass
        self.log(f"Loaded last-used settings from {os.path.basename(path)}")
        return s

    # ---------------- UI construction ----------------
    def _build_ui(self):
        r = self.root
        r.title("Temperature fluorescence scan" + ("  [SIMULATION]" if self.sim else ""))
        r.geometry("1880x980")
        r.columnconfigure(1, weight=1)
        r.rowconfigure(0, weight=1)

        left = ttk.Frame(r, padding=8)
        left.grid(row=0, column=0, sticky="ns")
        colA = ttk.Frame(left)                  # settings column
        colA.grid(row=0, column=0, sticky="n", padx=(0, 6))
        colB = ttk.Frame(left)                  # camera / dark / run column
        colB.grid(row=0, column=1, sticky="n")
        right = ttk.Frame(r, padding=(0, 8, 8, 8))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.columnconfigure(1, weight=1)
        right.rowconfigure(0, weight=1)

        self.inputs = []   # widgets locked during a run

        def entry_row(parent, row, label, default, width=10):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(default))
            e = ttk.Entry(parent, textvariable=var, width=width)
            e.grid(row=row, column=1, sticky="ew", pady=2, padx=(6, 0))
            self.inputs.append(e)
            return var

        # --- hardware ---
        hw = ttk.LabelFrame(colA, text="Hardware", padding=6)
        hw.grid(row=0, column=0, sticky="ew", pady=4)
        ttk.Label(hw, text="Stage port").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value="SIM" if self.sim else DEFAULT_PORT)
        self.port_cb = ttk.Combobox(hw, textvariable=self.port_var, width=12, values=self._serial_ports())
        self.port_cb.grid(row=0, column=1, padx=6, sticky="ew")
        self.inputs.append(self.port_cb)
        ttk.Label(hw, text="Camera").grid(row=1, column=0, sticky="w")
        self.cam_var = tk.StringVar()
        self.cam_cb = ttk.Combobox(hw, textvariable=self.cam_var, width=12, state="readonly")
        self.cam_cb.grid(row=1, column=1, padx=6, sticky="ew", pady=2)
        self.cam_cb.bind("<<ComboboxSelected>>", lambda e: self._start_camera(self.cam_var.get()))
        self.inputs.append(self.cam_cb)
        b = ttk.Button(hw, text="Reconnect camera", command=self.on_reconnect_camera)
        b.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.inputs.append(b)

        # --- protocol ---
        pf = ttk.LabelFrame(colA, text="Temperature protocol", padding=6)
        pf.grid(row=1, column=0, sticky="ew", pady=4)
        self.mode_var = tk.StringVar(value="step")
        mf = ttk.Frame(pf)
        mf.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(mf, text="Mode:").pack(side="left")
        for val, txt in (("step", "Step (equilibrate)"), ("ramp", "Continuous ramp")):
            rb = ttk.Radiobutton(mf, text=txt, value=val, variable=self.mode_var,
                                 command=self._apply_mode_state)
            rb.pack(side="left", padx=(6, 0))
            self.inputs.append(rb)
        self.pvars, self.pfield_widgets = {}, {}
        for i, (key, label, d, dsim, _mode) in enumerate(PROTOCOL_FIELDS, 1):
            self.pvars[key] = entry_row(pf, i, label, dsim if self.sim else d)
            self.pfield_widgets[key] = self.inputs[-1]

        # --- camera ---
        cf = ttk.LabelFrame(colB, text="Camera", padding=6)
        cf.grid(row=0, column=0, sticky="ew", pady=4)
        ttk.Label(cf, text="Gain").grid(row=0, column=0, sticky="w")
        self.gain_var = tk.StringVar(value="2")
        self.gain_cb = ttk.Combobox(cf, textvariable=self.gain_var, width=14, state="readonly",
                                    values=["0", "1", "2", "3"])
        self.gain_cb.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=2)
        self.inputs.append(self.gain_cb)
        self.exp_var = entry_row(cf, 1, "Exposure (ms)", 10.0)
        self.navg_var = entry_row(cf, 2, "Frames to average", 5)
        self.apply_btn = ttk.Button(cf, text="Apply gain / exposure", command=self.on_apply_camera)
        self.apply_btn.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        self.inputs.append(self.apply_btn)
        self.gain_cb.bind("<<ComboboxSelected>>", lambda e: self.on_apply_camera())
        self.autocon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cf, text="Auto contrast (display only)", variable=self.autocon_var).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.roi_var = tk.StringVar(value="ROI: full frame (drag on image to set)")
        ttk.Label(cf, textvariable=self.roi_var, foreground="#1a7abf", wraplength=270).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=2)
        self.clear_roi_btn = ttk.Button(cf, text="Clear ROI", command=self.clear_roi)
        self.clear_roi_btn.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.inputs.append(self.clear_roi_btn)

        # --- dark frame (pixel noise base, laser off) ---
        df = ttk.LabelFrame(colB, text="Dark frame (laser OFF)", padding=6)
        df.grid(row=1, column=0, sticky="ew", pady=4)
        self.ndark_var = entry_row(df, 0, "Dark frames to average", 50)
        self.dark_btn = ttk.Button(df, text="Measure dark frame now…", command=lambda: self._measure_dark())
        self.dark_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        self.inputs.append(self.dark_btn)
        self.dark_status_var = tk.StringVar(value="Dark: none")
        self.dark_status_lbl = ttk.Label(df, textvariable=self.dark_status_var, wraplength=270)
        self.dark_status_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        self.live_dark_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(df, text="Subtract dark in live view", variable=self.live_dark_var).grid(
            row=3, column=0, columnspan=2, sticky="w")

        # --- output ---
        of = ttk.LabelFrame(colA, text="Output", padding=6)
        of.grid(row=2, column=0, sticky="ew", pady=4)
        self.dir_var = entry_row(of, 0, "Save folder", DEFAULT_SAVE_DIR, width=22)
        b = ttk.Button(of, text="Browse…", command=self._browse)
        b.grid(row=1, column=1, sticky="e", pady=2)
        self.inputs.append(b)
        self.sample_var = entry_row(of, 2, "Sample name", "sample", width=22)
        self.comment_var = entry_row(of, 3, "Comments", "", width=22)

        # --- buttons ---
        bf = ttk.Frame(colB)
        bf.grid(row=2, column=0, sticky="ew", pady=6)
        bf.columnconfigure(0, weight=1)
        self.start_btn = tk.Button(bf, text="▶  Start scan", bg="#2e7d32", fg="white",
                                   font=("Segoe UI", 11, "bold"), command=self.on_start)
        self.start_btn.grid(row=0, column=0, sticky="ew", pady=2)
        self.abort_btn = tk.Button(bf, text="■  Abort scan", bg="#ef6c00", fg="white",
                                   font=("Segoe UI", 11, "bold"), command=self.on_abort, state="disabled")
        self.abort_btn.grid(row=1, column=0, sticky="ew", pady=2)
        tk.Button(bf, text="⚠  HEATER OFF NOW", bg="#c62828", fg="white",
                  font=("Segoe UI", 11, "bold"), command=self.on_heater_off).grid(
            row=2, column=0, sticky="ew", pady=2)

        # --- status ---
        sf = ttk.LabelFrame(colA, text="Status", padding=6)
        sf.grid(row=3, column=0, sticky="ew", pady=4)
        self.svars = {}
        for i, (k, label) in enumerate([("state", "State"), ("step", "Step"), ("tsp", "Set point"),
                                        ("pv", "Stage T"), ("timer", "Timer"), ("roi", "Live ROI mean"),
                                        ("sat", "Saturated px")]):
            ttk.Label(sf, text=label + ":").grid(row=i, column=0, sticky="w")
            self.svars[k] = tk.StringVar(value="-")
            ttk.Label(sf, textvariable=self.svars[k], font=("Segoe UI", 10, "bold")).grid(
                row=i, column=1, sticky="w", padx=6)
        self.svars["state"].set("idle")
        self.heater_lbl = tk.Label(sf, text="HEATER: not commanded", bg="#9e9e9e", fg="white",
                                   font=("Segoe UI", 10, "bold"))
        self.heater_lbl.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # --- camera view ---
        self.cam_fig = Figure(figsize=(6.4, 5.6), dpi=100)
        self.ax_img = self.cam_fig.add_axes([0.01, 0.01, 0.98, 0.92])
        self.im = self.ax_img.imshow(np.zeros((512, 640), np.uint16), cmap="gray", vmin=0, vmax=1023,
                                     extent=(0, 1280, 1024, 0), interpolation="nearest")
        self.ax_img.set_xticks([])
        self.ax_img.set_yticks([])
        self.cam_title = self.ax_img.set_title("Camera: connecting…", fontsize=9)
        self.cam_canvas = FigureCanvasTkAgg(self.cam_fig, master=right)
        self.cam_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self.selector = RectangleSelector(self.ax_img, self._on_roi, useblit=False, button=[1],
                                          minspanx=5, minspany=5, spancoords="data", interactive=True,
                                          props=dict(edgecolor="lime", fill=False, linewidth=1.5))

        # --- plots ---
        self.plot_fig = Figure(figsize=(6.4, 5.6), dpi=100)
        self.ax_T = self.plot_fig.add_subplot(211)
        self.ax_I = self.plot_fig.add_subplot(212)
        self.pv_line, = self.ax_T.plot([], [], "-", color="tab:red", lw=1.2, label="stage PV")
        self.tsp_line, = self.ax_T.plot([], [], "--", color="k", lw=1, label="set point")
        self.ax_T.set_xlabel("time (min)")
        self.ax_T.set_ylabel("T (°C)")
        self.ax_T.legend(loc="upper left", fontsize=8)
        self.ax_T.grid(alpha=0.3)
        self._style_I()
        self.plot_fig.tight_layout()
        self.plot_canvas = FigureCanvasTkAgg(self.plot_fig, master=right)
        self.plot_canvas.get_tk_widget().grid(row=0, column=1, sticky="nsew")

        # --- log ---
        self.log_box = ScrolledText(right, height=11, state="disabled", font=("Consolas", 9))
        self.log_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

    def _style_I(self):
        self.ax_I.set_xlabel("stage temperature at photo (°C)")
        self.ax_I.set_ylabel(self._intensity_label())
        self.ax_I.grid(alpha=0.3)

    @staticmethod
    def _serial_ports():
        try:
            from serial.tools import list_ports
            return [p.device for p in list_ports.comports()]
        except Exception:
            return [DEFAULT_PORT]

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or "D:/")
        if d:
            self.dir_var.set(d)

    def _set_inputs_enabled(self, enabled, running=True):
        for w in self.inputs:
            try:
                w.state(["!disabled"] if enabled else ["disabled"])
            except Exception:
                pass
        self.selector.set_active(enabled)
        self.start_btn.configure(state="normal" if enabled else "disabled")
        self.abort_btn.configure(state="normal" if (not enabled and running) else "disabled")
        if enabled:
            self._apply_mode_state()

    def _apply_mode_state(self):
        """Grey out the protocol fields that the selected mode does not use."""
        mode = self.mode_var.get()
        for key, _, _, _, used_in in PROTOCOL_FIELDS:
            on = used_in in ("both", mode)
            self.pfield_widgets[key].state(["!disabled"] if on else ["disabled"])

    def _set_heater(self, text, color):
        self.heater_lbl.configure(text=text, bg=color)

    # ---------------- camera ----------------
    def _start_camera(self, serial):
        if self.running:
            return
        if self.camera is not None:
            self.camera.stop()
            self.camera.join(timeout=5)
        try:
            g, e = self._gain(), float(self.exp_var.get())
        except ValueError:
            g, e = 2, 10.0
        self.camera = CameraThread(serial, g, e, self.sim, self.log)
        self.cam_info_loaded = False
        self.last_cam_count, self.last_frame_seen, self.stall_shown = -1, time.time(), False
        self.camera.start()

    def _gain(self):
        return int(self.gain_var.get().split()[0])

    def _check_camera_info(self):
        cam = self.camera
        if cam is None or self.cam_info_loaded or not cam.ready.is_set():
            return
        self.cam_info_loaded = True
        if cam.error is not None:
            self.cam_title.set_text(f"Camera error: {cam.error}")
            self.cam_canvas.draw_idle()
            return
        self.cam_cb.configure(values=cam.available)
        self.cam_var.set(cam.serial)
        vals = [f"{g}  ({cam.gain_db[g]:+.1f} dB)" if g in cam.gain_db else str(g)
                for g in range(cam.gain_range[0], cam.gain_range[1] + 1)]
        self.gain_cb.configure(values=vals)
        self.gain_var.set(vals[min(max(cam.gain, cam.gain_range[0]), cam.gain_range[1]) - cam.gain_range[0]])
        self.im.set_extent((0, cam.width, cam.height, 0))
        self.ax_img.set_xlim(0, cam.width)
        self.ax_img.set_ylim(cam.height, 0)

    def on_apply_camera(self):
        """Send gain/exposure to the camera. Returns True if the values were valid."""
        if self.running or self.camera is None:
            return False
        try:
            g, e = self._gain(), float(self.exp_var.get())
        except ValueError:
            messagebox.showerror("Camera", "Exposure must be a number (ms).")
            return False
        lo, hi = self.camera.exp_range_ms
        if not lo <= e <= hi:
            messagebox.showerror("Camera", f"Exposure must be between {lo} and {hi:.0f} ms.")
            return False
        self.camera.set_settings(g, e)
        return True

    # ---------------- dark frame ----------------
    def _dark_valid(self):
        """Dark frame exists and matches the current camera + requested gain/exposure."""
        if self.dark is None or self.camera is None:
            return False
        try:
            g, e = self._gain(), float(self.exp_var.get())
        except ValueError:
            return False
        d = self.dark_info
        return (d["camera_serial"] == self.camera.serial and d["gain"] == g
                and abs(d["exposure_ms"] - e) < 0.01
                and self.dark.shape == (self.camera.height, self.camera.width))

    def _dark_info_json(self):
        return {k: v for k, v in self.dark_info.items()}

    def _measure_dark(self, then=None):
        if self.running or self.dark_busy:
            return
        cam = self.camera
        if cam is None or not cam.ready.is_set() or cam.error is not None or not cam.is_alive():
            messagebox.showerror("Camera", "Camera is not running.")
            return
        try:
            n = int(self.ndark_var.get())
            if n < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Dark frame", "'Dark frames to average' must be an integer ≥ 1.")
            return
        if not self.on_apply_camera():
            return
        if not messagebox.askokcancel(
                "Dark frame - turn laser OFF",
                "Turn the laser OFF (block all excitation light).\n\n"
                f"Press OK when it is off - the camera will average {n} frames "
                f"at gain {self._gain()}, exposure {float(self.exp_var.get()):g} ms."):
            return
        self.dark_busy = True
        self._set_inputs_enabled(False, running=False)
        self.svars["state"].set("measuring dark frame…")
        self.log(f"Measuring dark frame ({n} frames)…")

        def go():
            try:
                time.sleep(0.3)   # let the last laser-on frame clear
                stack = cam.snap(n, timeout_s=n * (cam.exposure_ms / 1000 + 0.1) * 3 + 10)
                dark = stack.mean(axis=0)
                info = dict(camera_serial=cam.serial, gain=cam.gain, exposure_ms=cam.exposure_ms,
                            n_frames=n, timestamp=time.time(),
                            taken=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            mean=float(dark.mean()), spatial_std=float(dark.std()),
                            min=float(dark.min()), max=float(dark.max()),
                            pixel_noise=float(stack.std(axis=0).mean()) if n > 1 else 0.0)
                self.q.put(("dark", dark.astype(np.float32), info, then))
            except Exception as e:
                self.q.put(("dark_err", str(e)))
        threading.Thread(target=go, daemon=True).start()

    def _on_dark_done(self, dark, info, then):
        self.dark, self.dark_info, self.dark_busy = dark, info, False
        self._set_inputs_enabled(True)
        self.svars["state"].set("idle")
        self.log(f"Dark frame recorded: mean {info['mean']:.2f} counts, pixel-to-pixel std "
                 f"{info['spatial_std']:.2f}, temporal noise {info['pixel_noise']:.2f} counts/pixel "
                 f"(gain {info['gain']}, {info['exposure_ms']:g} ms, {info['n_frames']} frames).")
        messagebox.showinfo(
            "Dark frame recorded",
            f"Dark frame: mean {info['mean']:.2f} counts (min {info['min']:.1f}, max {info['max']:.1f})\n"
            f"Temporal noise ≈ {info['pixel_noise']:.2f} counts per pixel\n\n"
            "Turn the laser back ON, then press OK.")
        if then is not None:
            # give the camera a couple of exposures with the laser on before checking
            self.root.after(int(self.camera.exposure_ms * 3) + 400, then)

    def _update_dark_status(self):
        if self.dark is None:
            txt, color = "Dark: none", "gray"
        elif self._dark_valid():
            d = self.dark_info
            txt = (f"Dark OK: mean {d['mean']:.2f} counts, noise {d['pixel_noise']:.2f}\n"
                   f"(gain {d['gain']}, {d['exposure_ms']:g} ms, {d['taken'][11:]})")
            color = "#2e7d32"
        else:
            txt, color = "Dark: STALE - gain/exposure/camera changed, re-measure", "#c62828"
        if self.dark_status_var.get() != txt:
            self.dark_status_var.set(txt)
            self.dark_status_lbl.configure(foreground=color)

    def _on_roi(self, eclick, erelease):
        cam = self.camera
        W, H = (cam.width, cam.height) if cam and cam.width else (1280, 1024)
        x0, x1 = sorted([int(round(eclick.xdata)), int(round(erelease.xdata))])
        y0, y1 = sorted([int(round(eclick.ydata)), int(round(erelease.ydata))])
        x0, x1 = max(0, x0), min(W, x1)
        y0, y1 = max(0, y0), min(H, y1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        self.roi = (x0, y0, x1, y1)
        self.roi_var.set(f"ROI: x {x0}-{x1}, y {y0}-{y1}  ({x1-x0}×{y1-y0} px)")

    def clear_roi(self):
        self.roi = None
        try:
            self.selector.clear()
        except Exception:
            self.selector.set_visible(False)
        self.roi_var.set("ROI: full frame (drag on image to set)")
        self.cam_canvas.draw_idle()

    def _update_camera_view(self):
        now = time.time()
        if self.camera is None or now - self.last_cam < 0.15:
            return
        img, count = self.camera.get_latest()
        if img is None or count == self.last_cam_count:
            self._check_camera_stall(now)
            return
        self.last_cam, self.last_cam_count = now, count
        self.last_frame_seen = now
        self.stall_shown = False
        cam = self.camera
        raw = img
        sub = self.live_dark_var.get() and self._dark_valid()
        if sub:
            img = img.astype(np.float32) - self.dark
        disp = img[::DISPLAY_BIN, ::DISPLAY_BIN]
        self.im.set_data(disp)
        if self.autocon_var.get():
            lo, hi = np.percentile(disp[::4, ::4], (0.5, 99.9))
            self.im.set_clim(lo, max(hi, lo + 1))
        else:
            self.im.set_clim(0, cam.max_value)
        x0, y0, x1, y1 = self.roi if self.roi else (0, 0, img.shape[1], img.shape[0])
        region = img[y0:y1, x0:x1]
        self.svars["roi"].set(f"{region.mean():.1f} counts" + ("  (dark-subtracted)" if sub else "  (raw)"))
        sat = (raw[y0:y1, x0:x1] >= cam.max_value).mean() * 100
        self.svars["sat"].set(f"{sat:.2f} %" + ("  ← lower gain/exposure!" if sat > 0.1 else ""))
        self.cam_title.set_color("black")
        gdb = f" ({cam.gain_db[cam.gain]:+.1f} dB)" if cam.gain in cam.gain_db else ""
        self.cam_title.set_text(f"{cam.model} #{cam.serial} | gain {cam.gain}{gdb} | "
                                f"exp {cam.exposure_ms:.2f} ms | frame {count}")
        self.cam_canvas.draw_idle()

    def _check_camera_stall(self, now):
        """Show clearly on the image when no new frames arrive (instead of a silently frozen view)."""
        cam = self.camera
        if cam is None or not cam.ready.is_set():
            return
        if not cam.is_alive():
            msg = f"⚠ CAMERA STOPPED: {cam.error or 'closed'} - press 'Reconnect camera'"
        else:
            idle = now - getattr(self, "last_frame_seen", now)
            if idle < max(3.0, 3 * cam.exposure_ms / 1000 + 2.0):
                return
            msg = f"⚠ NO NEW FRAMES for {idle:.0f} s (last frame {self.last_cam_count}) - recovering…"
        if self.cam_title.get_text() != msg:
            self.cam_title.set_text(msg)
            self.cam_title.set_color("red")
            self.cam_canvas.draw_idle()
            if not getattr(self, "stall_shown", False):
                self.stall_shown = True
                self.log(msg)

    def on_reconnect_camera(self):
        if self.running or self.dark_busy:
            return
        self.log("Reconnecting camera…")
        self.last_frame_seen = time.time()
        self.cam_title.set_text("Camera: reconnecting…")
        self.cam_title.set_color("black")
        self._start_camera(self.cam_var.get() or None)

    # ---------------- plots ----------------
    def _update_plots(self, force=False):
        if not (self.plot_dirty or force) or (time.time() - self.last_plot < 1.0 and not force):
            return
        self.plot_dirty, self.last_plot = False, time.time()
        tm = np.asarray(self.t_data) / 60.0
        self.pv_line.set_data(tm, self.pv_data)
        self.tsp_line.set_data(tm, self.tsp_data)
        self.ax_T.relim()
        self.ax_T.autoscale_view()
        self.ax_I.cla()
        self._style_I()
        self._plot_points(self.ax_I)
        self.plot_canvas.draw_idle()

    def _intensity_label(self):
        return ("ROI mean, dark-subtracted (counts)" if getattr(self, "run_dark_used", False)
                else "ROI mean intensity (counts)")

    def _plot_points(self, ax):
        """Intensity vs T, one colour per sweep; ▲ heating, ▼ cooling."""
        if not self.points:
            return
        heating_fwd = self.run_params["t_end"] >= self.run_params["t_start"]
        colors = matplotlib.rcParams["axes.prop_cycle"].by_key()["color"]
        for s in sorted({p["sweep"] for p in self.points}):
            pts = [p for p in self.points if p["sweep"] == s]
            heating = (pts[0]["direction"] == "fwd") == heating_fwd
            dense = len(pts) > 40                      # ramp mode: many points, skip error bars
            ax.errorbar([p["pv_at_photo_C"] for p in pts], [p["roi_mean"] for p in pts],
                        yerr=None if dense else [p["roi_mean_frame_std"] for p in pts],
                        fmt="^-" if heating else "v--", color=colors[(s - 1) % len(colors)],
                        ms=2.5 if dense else 5, lw=0.8 if dense else 1.5, capsize=3,
                        label=f"sweep {s} ({'heating' if heating else 'cooling'})")
        ax.legend(fontsize=8)

    def _save_summary(self):
        if not self.run_dir or not self.points:
            return
        fig = Figure(figsize=(7, 8))
        FigureCanvasAgg(fig)
        a1, a2 = fig.add_subplot(211), fig.add_subplot(212)
        self._plot_points(a1)
        a1.set_xlabel("stage temperature at photo (°C)")
        a1.set_ylabel(self._intensity_label())
        p0 = self.points[0]
        rp = self.run_params
        mode_txt = (f"ramp {rp['rate']:g} °C/min" if rp["mode"] == "ramp" else f"steps of {rp['t_step']:g} °C")
        a1.set_title(f"{self.sample_var.get()}  |  {mode_txt}  |  gain {p0['gain']}, exposure {p0['exposure_ms']:.2f} ms, "
                     f"{p0['n_frames']} frames avg" + (", dark-subtracted" if p0["dark_subtracted"] else ""),
                     fontsize=10)
        a1.grid(alpha=0.3)
        a2.plot(np.asarray(self.t_data) / 60, self.pv_data, "-", color="tab:red", label="stage PV")
        a2.plot(np.asarray(self.t_data) / 60, self.tsp_data, "--", color="k", label="set point")
        a2.set_xlabel("time (min)")
        a2.set_ylabel("T (°C)")
        a2.legend()
        a2.grid(alpha=0.3)
        fig.tight_layout()
        path = os.path.join(self.run_dir, "intensity_vs_temperature.png")
        fig.savefig(path, dpi=150)
        self.log(f"Saved plot {path}")

    # ---------------- run control ----------------
    def _read_params(self):
        mode = self.mode_var.get()
        p = {"mode": mode}
        for key, label, _, _, used_in in PROTOCOL_FIELDS:
            if used_in not in ("both", mode):
                continue
            try:
                p[key] = float(self.pvars[key].get())
            except ValueError:
                raise ValueError(f"'{label}' must be a number")
        try:
            p["n_avg"] = int(self.navg_var.get())
        except ValueError:
            raise ValueError("'Frames to average' must be an integer")
        if p["n_avg"] < 1:
            raise ValueError("'Frames to average' must be ≥ 1")
        if p["tol"] <= 0 or p["stable_s"] < 0 or p["equil_timeout_s"] < 0:
            raise ValueError("Tolerance must be > 0; times must be ≥ 0")
        if p["max_safe"] > HARD_MAX_TEMP:
            raise ValueError(f"Safety cutoff must be ≤ {HARD_MAX_TEMP} °C")
        if p["n_sweeps"] != int(p["n_sweeps"]) or p["n_sweeps"] < 1:
            raise ValueError("'Sweeps' must be a whole number ≥ 1")
        p["n_sweeps"] = int(p["n_sweeps"])
        if mode == "step":
            if p["t_step"] <= 0 or p["hold_s"] < 0:
                raise ValueError("Step must be > 0 and hold time ≥ 0")
            pts = build_setpoints(p["t_start"], p["t_end"], p["t_step"])
        else:
            if not 0 < p["rate"] <= MAX_RAMP_RATE:
                raise ValueError(f"Ramp rate must be > 0 and ≤ {MAX_RAMP_RATE:g} °C/min")
            if p["t_start"] == p["t_end"]:
                raise ValueError("Start and end temperature must differ")
            if p["interval_s"] < 0:
                raise ValueError("'Photo every' must be ≥ 0 s")
            if p["save_every"] != int(p["save_every"]) or p["save_every"] < 0:
                raise ValueError("'Save image every N photos' must be a whole number ≥ 0")
            p["save_every"] = int(p["save_every"])
            pts = [p["t_start"], p["t_end"]]
        if max(pts) + p["tol"] >= p["max_safe"]:
            raise ValueError(f"Highest set point ({max(pts)} °C) must be below the safety cutoff "
                             f"({p['max_safe']} °C) by more than the tolerance")
        p["port"] = self.port_var.get().strip()
        p["save_dir"] = self.dir_var.get().strip()
        p["sample"] = self.sample_var.get().strip()
        p["comments"] = self.comment_var.get().strip()
        if not p["save_dir"]:
            raise ValueError("Choose a save folder")
        return p, pts

    def on_start(self):
        if self.running:
            return
        try:
            p, pts = self._read_params()
        except ValueError as e:
            messagebox.showerror("Invalid settings", str(e))
            return
        cam = self.camera
        if cam is None or not cam.ready.is_set() or cam.error is not None or not cam.is_alive():
            messagebox.showerror("Camera", "Camera is not running. Select / reconnect the camera first.")
            return
        if not self.on_apply_camera():
            return
        if self._dark_valid():
            age = (time.time() - self.dark_info["timestamp"]) / 60
            ans = messagebox.askyesnocancel(
                "Dark frame",
                f"A dark frame for these settings exists (taken {age:.0f} min ago, "
                f"mean {self.dark_info['mean']:.2f} counts).\n\n"
                "Yes = use it\nNo = measure a new one now (laser OFF)\nCancel = don't start")
            if ans is None:
                return
            if ans:
                self._confirm_and_launch(p, pts)
            else:
                self._measure_dark(then=lambda: self._confirm_and_launch(p, pts))
            return
        ans = messagebox.askyesnocancel(
            "Dark frame",
            f"No dark frame for gain {self._gain()}, exposure {float(self.exp_var.get()):g} ms.\n\n"
            "Yes = measure it now (you will be asked to turn the laser OFF)\n"
            "No = run WITHOUT dark subtraction\nCancel = don't start")
        if ans is None:
            return
        if ans:
            self._measure_dark(then=lambda: self._confirm_and_launch(p, pts))
        else:
            self._confirm_and_launch(p, pts, use_dark=False)

    def _confirm_and_launch(self, p, pts, use_dark=True):
        cam = self.camera
        use_dark = use_dark and self._dark_valid()
        dark_txt = "none (NOT subtracted)"
        if use_dark:
            dark_txt = f"subtracted (mean {self.dark_info['mean']:.2f} counts)"
            # check the laser is back on: live ROI should be clearly above the dark level
            img, _ = cam.get_latest()
            if img is not None and img.shape == self.dark.shape:
                x0, y0, x1, y1 = self.roi if self.roi else (0, 0, img.shape[1], img.shape[0])
                live = float(img[y0:y1, x0:x1].mean())
                dk = float(self.dark[y0:y1, x0:x1].mean())
                margin = max(3.0, 5 * self.dark_info["pixel_noise"] / np.sqrt(max(1, (x1 - x0) * (y1 - y0))))
                dark_txt += f"\nLive ROI now {live:.1f} counts vs dark {dk:.1f} counts"
                if live - dk < margin and not messagebox.askyesno(
                        "Laser off?", f"The live ROI ({live:.1f} counts) is almost the same as the dark "
                                      f"frame ({dk:.1f} counts).\nIs the laser back ON?\n\n"
                                      "Yes = continue anyway, No = cancel"):
                    return
        sweep_txt = f"{p['n_sweeps']} sweep(s) " + " → ".join(
            [f"{p['t_start']:g}", f"{p['t_end']:g}"] + [f"{p['t_start'] if s % 2 == 0 else p['t_end']:g}"
                                                         for s in range(2, p["n_sweeps"] + 1)])
        cool_txt = ""
        if p["n_sweeps"] > 1 and min(pts) < 26:
            cool_txt = (f"\nNote: the stage cools passively, so cooling near room temperature "
                        f"(down to {min(pts):g} °C) can be slow" + (" and may lag the ramp."
                                                                    if p["mode"] == "ramp" else "."))
        if p["mode"] == "ramp":
            ramp_min = p["n_sweeps"] * abs(p["t_end"] - p["t_start"]) / p["rate"]
            n_photos = ramp_min * 60 / max(p["interval_s"], 1.0) + p["n_sweeps"] + 1
            n_saved = 0 if p["save_every"] == 0 else n_photos / p["save_every"] + p["n_sweeps"] + 1
            cam = self.camera
            gb = n_saved * cam.width * cam.height * 4 / 1e9
            plan_txt = (f"Continuous ramp, {sweep_txt} °C at {p['rate']:g} °C/min\n"
                        f"≈ {ramp_min:.0f} min of ramping (+ equilibration at start), "
                        f"photo every {p['interval_s']:g} s → ≈ {n_photos:.0f} photos\n"
                        f"Images saved: every {p['save_every']} photo(s) → ≈ {gb:.1f} GB{cool_txt}\n")
        else:
            sched = build_schedule(p["t_start"], p["t_end"], p["t_step"], p["n_sweeps"])
            plan_txt = (f"Step mode, {sweep_txt} °C, {len(sched)} photos\n"
                        f"Set points: {', '.join(f'{t:g}' for t in pts)} °C{cool_txt}\n")
        if not messagebox.askokcancel(
                "Start scan",
                plan_txt +
                f"Gain {self._gain()}, exposure {float(self.exp_var.get()):g} ms, {p['n_avg']} frames averaged\n"
                f"ROI: {'full frame' if self.roi is None else self.roi}\n"
                f"Dark frame: {dark_txt}\n"
                f"Safety cutoff {p['max_safe']:g} °C\n\nStart heating?"):
            return

        self.save_settings()
        self.t_data, self.tsp_data, self.pv_data, self.points = [], [], [], []
        self.run_dir, self.run_error = None, None
        self.run_dark_used = use_dark
        self.run_params = p
        self._update_plots(force=True)
        self.stage = Stage(p["port"], self.sim, self.log)
        self.abort_evt = threading.Event()
        self.worker = ScanWorker(p, self.stage, cam, self.roi, self.abort_evt, self.q,
                                 dark=self.dark if use_dark else None,
                                 dark_info=self._dark_info_json() if use_dark else None)
        self.running = True
        self._set_inputs_enabled(False)
        self.svars["state"].set("starting")
        self.worker.start()

    def on_abort(self):
        if self.running and self.abort_evt is not None:
            self.log("Abort requested - stopping and turning heater off…")
            self.abort_evt.set()

    def on_heater_off(self):
        """Emergency button: works during a run or when idle."""
        if self.running:
            self.abort_evt.set()
            stage = self.stage
        else:
            stage = Stage(self.port_var.get().strip(), self.sim, self.log)
            stage.heating_commanded = True
        self.log("HEATER OFF requested.")

        def go():
            ok = stage.safe_off("HEATER OFF button")
            if not self.running:
                stage.disconnect()
            self.q.put(("heater", ok))
        threading.Thread(target=go, daemon=True).start()

    # ---------------- message pump ----------------
    def _poll(self):
        if self.destroyed:
            return
        try:
            self._drain_queue()
            self._check_camera_info()
            self._update_dark_status()
            self._update_camera_view()
            self._update_plots()
        except Exception:
            self._tk_exception(*sys.exc_info())
        if not self.destroyed:
            self.root.after(100, self._poll)

    def _drain_queue(self):
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                return
            kind = msg[0]
            if kind == "log":
                self._append_log(msg[1])
            elif kind == "rundir":
                self.run_dir = msg[1]
                self.log_fh = open(os.path.join(self.run_dir, "run_log.txt"), "a", encoding="utf-8")
            elif kind == "temp":
                _, t, tsp, pv, phase = msg
                self.t_data.append(t)
                self.tsp_data.append(tsp)
                self.pv_data.append(pv)
                self.svars["pv"].set(f"{pv:.3f} °C   (Δ {pv - tsp:+.3f})")
                self.svars["tsp"].set(f"{tsp:.2f} °C")
                self.plot_dirty = True
            elif kind == "ramp":
                _, sweep, n, target, rate = msg
                if sweep == 0:
                    self.svars["step"].set(f"to start ({target:g} °C), then {n} ramp(s)")
                    self._set_heater(f"HEATER: ON  (holding {target:g} °C)", "#c62828")
                else:
                    self.svars["step"].set(f"sweep {sweep}/{n}: → {target:g} °C at {rate:g} °C/min")
                    self._set_heater(f"HEATER: ON  (ramping to {target:g} °C)", "#c62828")
            elif kind == "timer":
                self.svars["timer"].set(msg[1])
            elif kind == "step":
                _, i, n, tsp, sweep, direction = msg
                self.svars["step"].set(f"{i} / {n}   (sweep {sweep}, {direction})")
                self.svars["tsp"].set(f"{tsp:g} °C")
                self._set_heater(f"HEATER: ON  (holding {tsp:g} °C)", "#c62828")
            elif kind == "phase":
                self.svars["state"].set(msg[1])
            elif kind == "stable":
                _, a, b, c = msg
                if a < 0:
                    self.svars["timer"].set(f"hold: {max(b, 0):.1f} s left")
                else:
                    self.svars["timer"].set(f"stable {a:.0f}/{b:.0f} s  (waited {c:.0f} s)")
            elif kind == "point":
                self.points.append(msg[1])
                self._update_plots(force=True)
            elif kind == "error":
                self.run_error = msg[1]
            elif kind == "dark":
                self._on_dark_done(msg[1], msg[2], msg[3])
            elif kind == "dark_err":
                self.dark_busy = False
                self._set_inputs_enabled(True)
                self.svars["state"].set("idle")
                self.log(f"Dark frame failed: {msg[1]}")
                messagebox.showerror("Dark frame", f"Dark frame measurement failed:\n{msg[1]}")
            elif kind == "heater":
                ok = msg[1]
                self._set_heater("HEATER: OFF (confirmed)" if ok else "HEATER: UNKNOWN - PRESS STOP ON CONTROLLER",
                                 "#2e7d32" if ok else "#b71c1c")
                if not ok and not self.closing:
                    messagebox.showwarning("Heater", "Could not confirm the heater is off.\n"
                                                     "Press STOP on the controller front panel.")
            elif kind == "done":
                self._on_done(msg[1], msg[2])

    def _on_done(self, run_dir, off_ok):
        self.running = False
        self._set_inputs_enabled(True)
        self.svars["state"].set("idle")
        self.svars["timer"].set("-")
        self._set_heater("HEATER: OFF (confirmed)" if off_ok else "HEATER: UNKNOWN - PRESS STOP ON CONTROLLER",
                         "#2e7d32" if off_ok else "#b71c1c")
        self._update_plots(force=True)
        try:
            self._save_summary()
        except Exception as e:
            self.log(f"Could not save summary plot: {e}")
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None
        if self.closing:
            self._finish_close()
            return
        heater = "Heater is OFF." if off_ok else "COULD NOT CONFIRM HEATER OFF - press STOP on the controller!"
        if self.run_error:
            messagebox.showerror("Scan stopped by error", f"{self.run_error}\n\n{heater}")
        elif not off_ok:
            messagebox.showwarning("Heater", heater)
        else:
            messagebox.showinfo("Scan finished", f"{len(self.points)} points saved to\n{run_dir}\n\n{heater}")

    # ---------------- errors / closing ----------------
    def _tk_exception(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        console(text)
        try:
            self._append_log("GUI error:\n" + text)
        except Exception:
            pass
        if self.running and self.abort_evt is not None:
            self._append_log("Aborting scan for safety (heater will be turned off).")
            self.abort_evt.set()

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Quit", "A scan is running.\nAbort it, turn the heater off and quit?"):
                return
            self.closing = True
            self.abort_evt.set()
            self.svars["state"].set("aborting before exit…")
            return          # _on_done() will finish closing once the heater is off
        self._finish_close()

    def _finish_close(self):
        self.save_settings()
        if self.camera is not None:
            self.camera.stop()
            self.camera.join(timeout=5)
        emergency_all_off("window closed")
        self.destroyed = True
        self.root.destroy()
