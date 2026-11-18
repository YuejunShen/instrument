"""Temperature schedules and the scan worker thread (step mode and continuous-ramp mode)."""
import csv
import json
import os
import threading
import time
import traceback
from datetime import datetime

import numpy as np
import tifffile

from .config import POLL_S, AbortRequested, SafetyError, console


# ============================================================
# Temperature schedules
# ============================================================
def build_setpoints(t_start, t_end, step):
    """23, 25, ..., 49, 50 - always ends exactly on t_end. Works for heating or cooling."""
    step = abs(float(step))
    if step <= 0:
        raise ValueError("Step must be > 0")
    direction = 1 if t_end >= t_start else -1
    pts, k = [], 0
    while True:
        t = t_start + direction * k * step
        if direction * (t - t_end) >= -1e-9:
            break
        pts.append(round(t, 3))
        k += 1
    pts.append(round(float(t_end), 3))
    return pts


def build_schedule(t_start, t_end, step, n_sweeps):
    """List of (sweep, direction, tsp). Sweep 1 goes start->end, sweep 2 end->start, sweep 3
    start->end, ... on the same temperature grid. The turnaround point is not repeated."""
    fwd = build_setpoints(t_start, t_end, step)
    sched = []
    for s in range(1, int(n_sweeps) + 1):
        pts = fwd if s % 2 == 1 else fwd[::-1]
        if s > 1:
            pts = pts[1:]
        direction = "fwd" if s % 2 == 1 else "rev"
        sched += [(s, direction, t) for t in pts]
    return sched


# ============================================================
# Scan worker (runs the protocol in a background thread)
# ============================================================
class ScanWorker(threading.Thread):
    def __init__(self, params, stage, camera, roi, abort_evt, out_q, dark=None, dark_info=None):
        super().__init__(daemon=True)
        self.p, self.stage, self.camera, self.roi = params, stage, camera, roi
        self.dark, self.dark_info = dark, dark_info      # per-pixel dark frame (float32) or None
        self.abort, self.q = abort_evt, out_q
        self.t0 = time.time()
        self.pv_failures = 0
        self.temp_writer = self.temp_fh = None

    def emit(self, *msg):
        self.q.put(msg)

    def log(self, s):
        console(s)
        self.emit("log", s)

    def _sleep(self, dt):
        if self.abort.wait(dt):
            raise AbortRequested()

    def _read_pv(self, tsp, phase):
        try:
            pv = self.stage.pv()
            self.pv_failures = 0
        except Exception as e:
            self.pv_failures += 1
            self.log(f"Temperature read failed ({self.pv_failures}/3): {e}")
            if self.pv_failures >= 3:
                raise SafetyError("cannot read stage temperature")
            return None
        if pv > self.p["max_safe"]:
            raise SafetyError(f"stage at {pv:.2f} °C exceeds safety cutoff {self.p['max_safe']:.1f} °C")
        t = time.time() - self.t0
        self.temp_writer.writerow([f"{t:.2f}", phase, f"{tsp:.3f}", f"{pv:.4f}"])
        self.temp_fh.flush()
        self.emit("temp", t, tsp, pv, phase)
        return pv

    def _equilibrate(self, tsp):
        p = self.p
        self.emit("phase", "equilibrating")
        start, stable_since = time.time(), None
        while True:
            pv = self._read_pv(tsp, "equilibrating")
            now = time.time()
            if pv is not None and abs(pv - tsp) <= p["tol"]:
                stable_since = stable_since or now
            else:
                stable_since = None
            stable_for = (now - stable_since) if stable_since else 0.0
            self.emit("stable", stable_for, p["stable_s"], now - start)
            if stable_for >= p["stable_s"]:
                return True
            if now - start > p["equil_timeout_s"]:
                self.log(f"WARNING: {tsp:g} °C not equilibrated within "
                         f"{p['equil_timeout_s']:.0f} s - taking the photo anyway.")
                return False
            self._sleep(POLL_S)

    def _hold(self, tsp):
        self.emit("phase", "holding")
        end, pvs = time.time() + self.p["hold_s"], []
        while True:
            pv = self._read_pv(tsp, "holding")
            if pv is not None:
                pvs.append(pv)
            remaining = end - time.time()
            self.emit("stable", -1, remaining, 0)
            if remaining <= 0:
                return float(np.mean(pvs)) if pvs else float("nan")
            self._sleep(min(POLL_S, remaining))

    # ---- photo + analysis (shared by both modes) ----
    def _photo(self, tsp, phase, fname):
        """Average n fresh frames, subtract the dark frame, measure the ROI.
        Saves the raw average as float32 TIFF if fname is given. Returns (pv_at_photo, stats)."""
        cam, n = self.camera, int(self.p["n_avg"])
        x0, y0, x1, y1 = self.box
        pv_a = self._read_pv(tsp, phase)
        stack = cam.snap(n, timeout_s=n * (cam.exposure_ms / 1000 + 0.1) * 3 + 10)
        pv_b = self._read_pv(tsp, phase)
        pvs = [v for v in (pv_a, pv_b) if v is not None]
        pv_photo = float(np.mean(pvs)) if pvs else float("nan")

        raw_roi = stack[:, y0:y1, x0:x1]
        sat = float((raw_roi >= cam.max_value).mean())          # on raw counts
        raw_mean = float(raw_roi.mean())
        if self.dark is not None:                              # per-pixel dark subtraction
            raw_roi = raw_roi - self.dark[None, y0:y1, x0:x1]
        per_frame = raw_roi.mean(axis=(1, 2))
        roi_avg = raw_roi.mean(axis=0)
        if fname:
            # raw average is saved; dark-subtracted image = this - dark_frame.tif
            tifffile.imwrite(os.path.join(self.img_dir, fname), stack.mean(axis=0).astype(np.float32))
        stats = dict(roi_mean=float(per_frame.mean()), roi_mean_raw=raw_mean,
                     dark_roi_mean=self.dark_roi_mean, dark_subtracted=self.dark is not None,
                     roi_mean_frame_std=float(per_frame.std(ddof=1)) if n > 1 else 0.0,
                     roi_spatial_std=float(roi_avg.std()), roi_median=float(np.median(roi_avg)),
                     roi_mean_per_ms=float(per_frame.mean()) / cam.exposure_ms,
                     saturated_fraction=sat, n_frames=n, gain=cam.gain, exposure_ms=cam.exposure_ms,
                     image_file=f"images/{fname}" if fname else "")
        return pv_photo, stats

    def _write_row(self, row):
        self.res.writerow(row)
        self.res_fh.flush()
        self.emit("point", row)
        sat = row["saturated_fraction"]
        self.log(f"  photo at {row['pv_at_photo_C']:.2f} °C: ROI mean = {row['roi_mean']:.2f} counts"
                 + (f"  (WARNING {sat*100:.2f}% saturated pixels)" if sat > 1e-3 else ""))

    # ---- step mode: equilibrate at each set point, then one photo ----
    def _run_steps(self, schedule):
        for i, (sweep, direction, tsp) in enumerate(schedule, 1):
            if self.abort.is_set():
                raise AbortRequested()
            self.emit("step", i, len(schedule), tsp, sweep, direction)
            self.log(f"Step {i}/{len(schedule)} (sweep {sweep} {direction}): going to {tsp:g} °C")
            self.stage.hold(tsp)
            eq = self._equilibrate(tsp)
            pv_hold = self._hold(tsp)
            self.emit("phase", "taking photo")
            fname = f"step{i:03d}_sweep{sweep}_{direction}_T{tsp:06.2f}C.tif"
            pv_photo, stats = self._photo(tsp, "photo", fname)
            self._write_row(dict(step=i, sweep=sweep, direction=direction, tsp_C=tsp,
                                 pv_at_photo_C=round(pv_photo, 4), pv_mean_hold_C=round(pv_hold, 4),
                                 time_s=round(time.time() - self.t0, 2), equilibrated=eq, **stats))

    # ---- ramp mode: equilibrate at start, then constant-rate ramps with periodic photos ----
    def _run_ramp(self):
        p = self.p
        n_sweeps, rate, interval = int(p["n_sweeps"]), p["rate"], p["interval_s"]
        save_every = int(p["save_every"])
        self.n_photo = 0

        def photo(sweep, direction, phase, sp, force_save=False):
            self.n_photo += 1
            k = self.n_photo
            save = save_every > 0 and (force_save or (k - 1) % save_every == 0)
            fname = f"photo{k:04d}_sweep{sweep}_{direction}_{phase}_SP{sp:06.2f}C.tif" if save else None
            pv_photo, stats = self._photo(sp, phase, fname)
            try:
                sp_ctrl = round(self.stage.set_point(), 3)
            except Exception:
                sp_ctrl = ""
            self._write_row(dict(photo=k, sweep=sweep, direction=direction, phase=phase,
                                 time_s=round(time.time() - self.t0, 2), sp_expected_C=round(sp, 3),
                                 sp_controller_C=sp_ctrl, pv_at_photo_C=round(pv_photo, 4),
                                 pv_minus_sp_C=round(pv_photo - sp, 4), ramp_rate_C_per_min=rate, **stats))

        # 1) go to the start temperature and equilibrate there
        t0 = p["t_start"]
        self.emit("ramp", 0, n_sweeps, t0, rate)
        self.log(f"Going to start temperature {t0:g} °C and equilibrating…")
        self.stage.hold(t0)
        self._equilibrate(t0)
        photo(1, "fwd", "start", t0, force_save=True)

        # 2) ramps: start -> end -> start -> ...
        for sweep in range(1, n_sweeps + 1):
            origin, target = (p["t_start"], p["t_end"]) if sweep % 2 else (p["t_end"], p["t_start"])
            direction = "fwd" if sweep % 2 else "rev"
            self.emit("ramp", sweep, n_sweeps, target, rate)
            self.log(f"Sweep {sweep}/{n_sweeps}: ramping {origin:g} → {target:g} °C at {rate:g} °C/min")
            self.emit("phase", "ramping")
            self.stage.ramp(target, rate)
            t_ramp0 = time.time()
            ramp_s = abs(target - origin) / rate * 60.0
            next_photo = t_ramp0 + interval
            lag_warned = False
            while True:
                now = time.time()
                frac = min((now - t_ramp0) / ramp_s, 1.0) if ramp_s > 0 else 1.0
                sp = origin + (target - origin) * frac
                phase = "ramp" if frac < 1 else "settle"
                pv = self._read_pv(sp, phase)
                if frac < 1:
                    self.emit("timer", f"ramp {frac*100:.0f}%  ({(now - t_ramp0)/60:.1f} / {ramp_s/60:.1f} min)")
                else:
                    self.emit("phase", "settling at end of ramp")
                    self.emit("timer", f"settling ({now - t_ramp0 - ramp_s:.0f} s)")
                if pv is not None and frac < 1 and not lag_warned and abs(pv - sp) > max(1.0, 5 * p["tol"]):
                    lag_warned = True
                    self.log(f"WARNING: stage lags the ramp by {pv - sp:+.2f} °C "
                             f"({'cooling is passive - try a slower rate' if target < origin else 'check stage'}).")
                if frac >= 1:
                    if pv is not None and abs(pv - target) <= p["tol"]:
                        break
                    if now - t_ramp0 - ramp_s > p["equil_timeout_s"]:
                        self.log(f"WARNING: {target:g} °C not reached within {p['equil_timeout_s']:.0f} s "
                                 f"after the ramp ended - continuing.")
                        break
                if now >= next_photo:
                    photo(sweep, direction, phase, sp)
                    next_photo = max(next_photo + interval, time.time())
                self._sleep(max(0.05, min(POLL_S, next_photo - time.time())))
            photo(sweep, direction, "end", target, force_save=True)

    def run(self):
        p = self.p
        run_dir = None
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = "".join(c if c.isalnum() or c in "-_" else "_" for c in p["sample"]) or "scan"
            run_dir = os.path.join(p["save_dir"], f"{stamp}_{name}")
            img_dir = os.path.join(run_dir, "images")
            os.makedirs(img_dir, exist_ok=True)
            self.emit("rundir", run_dir)
            self.log(f"Saving to {run_dir}")

            cam = self.camera
            H, W = cam.height, cam.width
            x0, y0, x1, y1 = self.roi if self.roi else (0, 0, W, H)
            self.img_dir, self.box = img_dir, (x0, y0, x1, y1)
            ramp = p["mode"] == "ramp"
            if ramp:
                schedule = [[s, "fwd" if s % 2 else "rev", p["t_end"] if s % 2 else p["t_start"]]
                            for s in range(1, int(p["n_sweeps"]) + 1)]       # ramp targets
            else:
                schedule = build_schedule(p["t_start"], p["t_end"], p["t_step"], p["n_sweeps"])
            info = dict(p, schedule=[list(s) for s in schedule], roi_x0y0x1y1=[x0, y0, x1, y1],
                        roi_is_full_frame=self.roi is None,
                        camera_model=cam.model, camera_serial=cam.serial,
                        gain=cam.gain, gain_db=cam.gain_db.get(cam.gain),
                        exposure_ms=cam.exposure_ms,
                        dark_subtracted=self.dark is not None, dark_info=self.dark_info,
                        started=stamp)
            with open(os.path.join(run_dir, "run_info.json"), "w") as f:
                json.dump(info, f, indent=2)
            if self.dark is not None:
                if self.dark.shape != (H, W):
                    raise RuntimeError(f"dark frame shape {self.dark.shape} != camera {(H, W)}")
                tifffile.imwrite(os.path.join(run_dir, "dark_frame.tif"), self.dark.astype(np.float32))
                dark_roi_mean = float(self.dark[y0:y1, x0:x1].mean())
                self.log(f"Dark frame subtraction ON (dark ROI mean {dark_roi_mean:.2f} counts).")
            else:
                dark_roi_mean = 0.0
                self.log("Dark frame subtraction OFF.")
            self.dark_roi_mean = dark_roi_mean

            if ramp:
                dur = int(p["n_sweeps"]) * abs(p["t_end"] - p["t_start"]) / p["rate"]
                self.log(f"Continuous ramp: {int(p['n_sweeps'])} sweep(s) between {p['t_start']:g} and "
                         f"{p['t_end']:g} °C at {p['rate']:g} °C/min (~{dur:.0f} min of ramping), "
                         f"photo every {p['interval_s']:g} s")
            else:
                self.log(f"{int(p['n_sweeps'])} sweep(s), {len(schedule)} photos. Sweep 1 set points: "
                         f"{', '.join(f'{t:g}' for s, _, t in schedule if s == 1)} °C")
            self.log(f"Stage connected: {self.stage.connect()}")

            self.temp_fh = open(os.path.join(run_dir, "temperature_log.csv"), "w", newline="")
            self.temp_writer = csv.writer(self.temp_fh)
            self.temp_writer.writerow(["time_s", "phase", "tsp_C", "pv_C"])

            res_fh = open(os.path.join(run_dir, "results.csv"), "w", newline="")
            if ramp:
                cols = ["photo", "sweep", "direction", "phase", "time_s", "sp_expected_C", "sp_controller_C",
                        "pv_at_photo_C", "pv_minus_sp_C", "ramp_rate_C_per_min"]
            else:
                cols = ["step", "sweep", "direction", "tsp_C", "pv_at_photo_C", "pv_mean_hold_C", "time_s",
                        "equilibrated"]
            cols += ["roi_mean", "roi_mean_raw", "dark_roi_mean", "dark_subtracted",
                     "roi_mean_frame_std", "roi_spatial_std", "roi_median",
                     "roi_mean_per_ms", "saturated_fraction", "n_frames", "gain", "exposure_ms", "image_file"]
            self.res = csv.DictWriter(res_fh, fieldnames=cols)
            self.res_fh = res_fh
            self.res.writeheader()

            try:
                if ramp:
                    self._run_ramp()
                else:
                    self._run_steps(schedule)
                self.log("Scan complete.")
            finally:
                res_fh.close()
        except AbortRequested:
            self.log("Scan aborted by user.")
        except Exception as e:
            tb = traceback.format_exc()
            self.log(f"ERROR: {e}\n{tb}")
            self.emit("error", str(e))
        finally:
            # ---- SAFETY: always turn the heater off ----
            off_ok = True
            if self.stage.ctrl is not None or self.stage.heating_commanded:
                self.emit("phase", "turning heater off")
                off_ok = self.stage.safe_off("end of run")
            self.stage.disconnect()
            if self.temp_fh:
                self.temp_fh.close()
            self.emit("done", run_dir, off_ok)
