"""Thorlabs scientific camera (CS135MU etc.): one thread owns the camera and streams frames."""
import ctypes
import os
import threading
import time

import numpy as np

from .config import DLL_PATH, SIM_STATE, console

# ============================================================
# Camera (one thread owns the camera; free-running live view)
# ============================================================
def load_thorlabs_sdk():
    os.environ["PATH"] += os.pathsep + DLL_PATH
    try:
        os.add_dll_directory(DLL_PATH)
    except Exception:
        pass
    ctypes.windll.LoadLibrary(os.path.join(DLL_PATH, "thorlabs_tsi_camera_sdk.dll"))
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK, OPERATION_MODE
    return TLCameraSDK, OPERATION_MODE


class CameraThread(threading.Thread):
    def __init__(self, serial, gain, exposure_ms, simulate=False, log=console):
        super().__init__(daemon=True)
        self.req_serial, self.simulate, self.log = serial, simulate, log
        self.cond = threading.Condition()
        self.latest, self.count, self.settings_count = None, 0, 0
        self._pending = (int(gain), float(exposure_ms))
        self.gain, self.exposure_ms = int(gain), float(exposure_ms)
        self.ready, self._stop_evt = threading.Event(), threading.Event()
        self.error = None
        self.available, self.serial, self.model = [], None, ""
        self.width = self.height = 0
        self.max_value = 1023
        self.gain_range, self.gain_db = (0, 0), {}
        self.exp_range_ms = (0.1, 60000.0)

    # ---- called from other threads ----
    def set_settings(self, gain, exposure_ms):
        with self.cond:
            self._pending = (int(gain), float(exposure_ms))

    def stop(self):
        self._stop_evt.set()

    def get_latest(self):
        with self.cond:
            return self.latest, self.count

    def snap(self, n_frames, timeout_s):
        """Average n fresh frames (all exposed after this call and after any settings change)."""
        frames = []
        deadline = time.time() + timeout_s
        with self.cond:
            first_ok = max(self.count + 2, self.settings_count + 3)
            last = self.count
            while len(frames) < n_frames:
                if self.error is not None:
                    raise RuntimeError(f"camera failed: {self.error}")
                if not self.is_alive():
                    raise RuntimeError("camera thread is not running")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"camera: got {len(frames)}/{n_frames} frames in {timeout_s:.0f}s")
                self.cond.wait(min(remaining, 0.5))
                first_ok = max(first_ok, self.settings_count + 3)
                if self._pending is not None:       # settings change not applied yet
                    first_ok = max(first_ok, self.count + 3)
                    continue
                if self.count > last:
                    last = self.count
                    if self.count >= first_ok:
                        frames.append(self.latest)
        return np.stack(frames).astype(np.float32)

    # ---- camera thread ----
    def run(self):
        try:
            if self.simulate:
                self._run_sim()
            else:
                self._run_real()
        except Exception as e:
            self.error = e
            self.log(f"Camera error: {e}")
        finally:
            self.ready.set()
            with self.cond:
                self.cond.notify_all()

    def _take_pending(self):
        with self.cond:
            p, self._pending = self._pending, None
        return p

    def _publish(self, img):
        with self.cond:
            self.latest = img
            self.count += 1
            self.cond.notify_all()

    MAX_REOPENS = 3

    def _run_real(self):
        """Open the camera and stream. If frames stop, re-arm; if that fails, reopen the camera."""
        TLCameraSDK, OPERATION_MODE = load_thorlabs_sdk()
        reopens = 0
        while not self._stop_evt.is_set():
            try:
                self._stream_once(TLCameraSDK, OPERATION_MODE)
                return                                   # stopped on request
            except Exception as e:
                reopens += 1
                if reopens > self.MAX_REOPENS or not self.ready.is_set():
                    raise                                # give up -> self.error is set in run()
                self.log(f"Camera problem: {e} - reopening camera ({reopens}/{self.MAX_REOPENS})…")
                time.sleep(1.0)

    def _stream_once(self, TLCameraSDK, OPERATION_MODE):
        with TLCameraSDK() as sdk:
            self.available = list(sdk.discover_available_cameras())
            if not self.available:
                raise RuntimeError("no Thorlabs camera found")
            serial = self.req_serial if self.req_serial in self.available else self.available[0]
            self.req_serial = serial
            with sdk.open_camera(serial) as cam:
                self.serial, self.model = serial, cam.model
                W, H = cam.image_width_pixels, cam.image_height_pixels
                self.width, self.height = W, H
                self.max_value = 2 ** cam.bit_depth - 1
                gr = cam.gain_range
                self.gain_range = (gr.min, gr.max)
                if gr.max - gr.min <= 64:
                    self.gain_db = {g: cam.convert_gain_to_decibels(g) for g in range(gr.min, gr.max + 1)}
                er = cam.exposure_time_range_us
                self.exp_range_ms = (er.min / 1000.0, er.max / 1000.0)

                cam.frames_per_trigger_zero_for_unlimited = 0
                cam.image_poll_timeout_ms = 50
                cam.operation_mode = OPERATION_MODE.SOFTWARE_TRIGGERED
                self._armed = False
                p = self._take_pending() or (self.gain, self.exposure_ms)   # keep settings on reopen
                self._apply(cam, p)
                self._arm(cam)
                self.log(f"Camera {self.model} #{serial} open ({W}x{H}, {cam.bit_depth}-bit).")
                self.ready.set()
                last_frame, rearms = time.time(), 0
                try:
                    while not self._stop_evt.is_set():
                        p = self._take_pending()
                        if p is not None:
                            self._apply(cam, p)
                            last_frame = time.time()
                        frame = cam.get_pending_frame_or_null()
                        now = time.time()
                        if frame is not None:
                            self._publish(np.copy(frame.image_buffer).reshape(H, W))
                            last_frame, rearms = now, 0
                            continue
                        # ---- stall watchdog ----
                        if now - last_frame > max(2.0, 3 * self.exposure_ms / 1000 + 1.0):
                            rearms += 1
                            if rearms > 2:
                                raise RuntimeError(f"no frames for {now - last_frame:.1f} s after re-arming")
                            self.log(f"Camera: no new frame for {now - last_frame:.1f} s "
                                     f"- re-arming (attempt {rearms}/2)")
                            self._disarm(cam)
                            self._arm(cam)
                            last_frame = time.time()
                finally:
                    self._disarm(cam)
                    self.log("Camera closed.")

    def _arm(self, cam):
        cam.arm(2)
        cam.issue_software_trigger()
        self._armed = True

    def _disarm(self, cam):
        if getattr(self, "_armed", False):
            try:
                cam.disarm()
            except Exception as e:
                self.log(f"Camera disarm error: {e}")
            self._armed = False

    def _apply(self, cam, p):
        if p is None:
            return
        g, exp_ms = p
        g = int(min(max(g, self.gain_range[0]), self.gain_range[1]))
        exp_ms = min(max(exp_ms, self.exp_range_ms[0]), self.exp_range_ms[1])
        if cam is not None:
            # change settings with the camera disarmed, then restart streaming
            was_armed = getattr(self, "_armed", False)
            self._disarm(cam)
            cam.exposure_time_us = int(round(exp_ms * 1000))
            cam.gain = g
            exp_ms = cam.exposure_time_us / 1000.0
            if was_armed:
                self._arm(cam)
        self.gain, self.exposure_ms = g, exp_ms
        with self.cond:
            self.settings_count = self.count
        self.log(f"Camera settings: gain {g}"
                 + (f" ({self.gain_db[g]:+.1f} dB)" if g in self.gain_db else "")
                 + f", exposure {exp_ms:.2f} ms")

    def _run_sim(self):
        self.available, self.serial, self.model = ["SIM"], "SIM", "CS135MU-simulated"
        W, H = 1280, 1024
        self.width, self.height, self.max_value = W, H, 1023
        self.gain_range = (0, 3)
        self.gain_db = {0: -7.5, 1: -5.6, 2: 0.0, 3: 5.3}
        self.exp_range_ms = (0.1, 59269.0)
        yy, xx = np.mgrid[0:H, 0:W]
        blob = np.exp(-((xx - 640) ** 2 + (yy - 512) ** 2) / (2 * 220.0 ** 2)).astype(np.float32)
        rng = np.random.default_rng()
        self._apply(None, self._take_pending())
        self.ready.set()
        while not self._stop_evt.is_set():
            p = self._take_pending()
            if p is not None:
                self._apply(None, p)
            time.sleep(max(self.exposure_ms / 1000.0, 0.03))
            T = SIM_STATE["T"]
            scale = 10 ** (self.gain_db[self.gain] / 20) * self.exposure_ms / 10.0
            bright = 600 * (1 - 0.012 * (T - 23)) * scale       # ~1.2 %/C thermal quenching
            bright *= 1.0 if SIM_STATE["laser"] else 0.0
            img = 40 + bright * blob + rng.normal(0, 4, (H, W)).astype(np.float32)
            self._publish(np.clip(img, 0, 1023).astype(np.uint16))
