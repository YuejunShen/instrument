"""Instec mK2000B heating stage: thread-safe wrapper, simulator and safety shutdown."""
import atexit
import ctypes
import sys
import threading
import time
import traceback

import numpy as np

from .config import BAUDRATE, MAX_RAMP_RATE, SIM_STATE, console

# ============================================================
# Heating stage
# ============================================================
_ALL_STAGES = []               # every Stage ever created - used by emergency shutdown


class SimStage:
    """Fake mK2000B: first-order approach to the set point."""

    def __init__(self):
        self.pv, self.tsp, self.running, self.t = 22.6, None, False, time.time()
        self.ramp_info = None          # (t0, sp0, target, rate C/min)

    def _sp(self):
        if self.ramp_info is None:
            return self.tsp
        t0, sp0, target, rate = self.ramp_info
        moved = rate / 60.0 * (time.time() - t0)
        return target if moved >= abs(target - sp0) else sp0 + np.sign(target - sp0) * moved

    def _update(self):
        now = time.time()
        dt, self.t = now - self.t, now
        target, tau = (self._sp(), 4.0) if self.running else (22.6, 30.0)
        self.pv += (target - self.pv) * (1 - np.exp(-dt / tau))
        SIM_STATE["T"] = self.pv

    def connect(self): pass
    def disconnect(self): pass
    def get_system_information(self): return ("SIM", "MK2000B-simulated", "0", "0")

    def hold_check(self, tsp):
        self._update()
        self.tsp, self.running, self.ramp_info = tsp, True, None

    def ramp_check(self, tsp, rt):
        if not 0 <= rt <= MAX_RAMP_RATE:
            raise ValueError("Ramp rate is out of range")
        self._update()
        sp0 = self._sp() if (self.running and self.tsp is not None) else self.pv
        self.tsp, self.running, self.ramp_info = tsp, True, (time.time(), sp0, tsp, rt)

    def get_set_point_temperature(self):
        return self._sp() if self.running else (self.tsp or 0.0)

    def get_process_variable(self):
        self._update()
        return self.pv + np.random.normal(0, 0.01)

    def stop(self):
        self._update()
        self.running = False

    def get_system_status(self):
        if not self.running:
            return "STOP"
        return "RAMP" if self.ramp_info is not None and self._sp() != self.tsp else "HOLD"


class Stage:
    """Thread-safe wrapper around instec.MK2000B with a robust safe_off()."""

    def __init__(self, port, simulate=False, log=console):
        self.port, self.simulate, self.log = port, simulate, log
        self.ctrl = None
        self.lock = threading.RLock()
        self.heating_commanded = False
        _ALL_STAGES.append(self)

    def _new_ctrl(self):
        if self.simulate:
            return SimStage()
        import instec
        return instec.MK2000B(instec.mode.USB, BAUDRATE, self.port)

    def connect(self):
        with self.lock:
            ctrl = self._new_ctrl()
            ctrl.connect()
            self.ctrl = ctrl
            return ctrl.get_system_information()

    def hold(self, tsp):
        with self.lock:
            self.heating_commanded = True
            self.ctrl.hold_check(float(tsp))   # raises if outside the stage's range

    def ramp(self, tsp, rate_c_per_min):
        """Controller ramps its set point from the current value to tsp at the given rate,
        then holds at tsp."""
        with self.lock:
            self.heating_commanded = True
            self.ctrl.ramp_check(float(tsp), float(rate_c_per_min))   # raises if out of range

    def set_point(self):
        with self.lock:
            return float(self.ctrl.get_set_point_temperature())

    def pv(self):
        with self.lock:
            return float(self.ctrl.get_process_variable())

    @staticmethod
    def _is_stop(status):
        return str(getattr(status, "name", status)).upper().endswith("STOP")

    def safe_off(self, reason=""):
        """Stop all temperature control. Retries (reconnecting if needed).
        Returns True only if the controller confirms it is stopped."""
        why = f" ({reason})" if reason else ""
        for attempt in range(1, 4):
            got = self.lock.acquire(timeout=10)   # don't deadlock on a stuck call
            try:
                if self.ctrl is None or attempt > 1:
                    try:
                        if self.ctrl is not None:
                            self.ctrl.disconnect()
                    except Exception:
                        pass
                    self.ctrl = None
                    ctrl = self._new_ctrl()
                    ctrl.connect()
                    self.ctrl = ctrl
                self.ctrl.stop()
                time.sleep(0.2)
                if self._is_stop(self.ctrl.get_system_status()):
                    self.heating_commanded = False
                    self.log(f"Heater OFF confirmed{why}.")
                    return True
                self.log(f"Heater stop sent but status not STOP (attempt {attempt}).")
            except Exception as e:
                self.log(f"Heater-off attempt {attempt} failed: {e}")
            finally:
                if got:
                    self.lock.release()
            time.sleep(0.5)
        self.log("!!! COULD NOT CONFIRM HEATER OFF - PRESS STOP ON THE CONTROLLER !!!")
        return False

    def disconnect(self):
        with self.lock:
            try:
                if self.ctrl is not None:
                    self.ctrl.disconnect()
            except Exception:
                pass
            self.ctrl = None


def emergency_all_off(reason="emergency"):
    for s in list(_ALL_STAGES):
        if s.heating_commanded:
            try:
                s.safe_off(reason)
            except Exception as e:
                console(f"emergency_all_off: {e}")


def install_safety_hooks():
    atexit.register(emergency_all_off, "program exit")

    def thread_hook(args):
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        emergency_all_off("unhandled thread error")
    threading.excepthook = thread_hook

    if sys.platform == "win32":   # Ctrl+C / Ctrl+Break / closing the console window
        try:
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def console_handler(ctrl_type):
                emergency_all_off(f"console event {ctrl_type}")
                return False
            install_safety_hooks._ref = handler_type(console_handler)   # keep alive
            ctypes.windll.kernel32.SetConsoleCtrlHandler(install_safety_hooks._ref, True)
        except Exception as e:
            console(f"Could not install console handler: {e}")
