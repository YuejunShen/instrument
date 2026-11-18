"""
Temperature-dependent fluorescence scan
=======================================
Instec mK2000B heating stage  +  Thorlabs CS135MU camera, with a live GUI.

Before the run: a per-pixel DARK FRAME (laser off) is measured and averaged. The
program asks you to turn the laser off, records it, then asks you to turn it back on.
It is valid only for the gain/exposure/camera it was taken with; it is subtracted
from every photo before the ROI mean is computed (raw images are still saved,
together with dark_frame.tif).

Sweeps: 1 = start->end, 2 = start->end->start, 3 = start->end->start->end, ...
(same temperature grid each way, turnaround point not repeated). If the heating
and cooling curves overlay, the signal change is reversible (no photobleaching).

Two modes (selectable in the GUI):

STEP mode, per set point (e.g. 23, 25, ..., 49, 50 C):
    1. HOLD the stage at the set point
    2. wait until equilibrated: |PV - TSP| <= tolerance for N seconds in a row
    3. hold an extra few seconds
    4. grab and average a few camera frames, subtract the dark frame,
       measure mean intensity in the ROI
    5. save the averaged raw image (float32 TIFF) and append a row to results.csv

CONTINUOUS RAMP mode:
    1. HOLD at the start temperature and equilibrate (same criterion as above)
    2. the controller ramps its set point at a constant rate (C/min, TEMP:RAMP)
       towards the end temperature (and back, for more sweeps)
    3. a photo is taken every N seconds during the ramp (PV recorded at each photo);
       a sweep ends when the stage is within tolerance of the target
    4. ROI statistics are logged for every photo; the full image is saved every
       N-th photo (and at the start/end of each sweep) to limit disk use

The GUI shows the live camera image (drag a rectangle on it to set the ROI),
temperature vs time, and fluorescence intensity vs temperature as it builds up.

SAFETY - the heater is switched OFF (instec stop()) when:
    * the scan finishes normally
    * the user presses Abort or HEATER OFF
    * ANY error happens (camera, stage, file saving, ...)
    * the stage reads above the "Safety cutoff" temperature
    * the stage temperature can't be read 3 times in a row
    * the window is closed, the program exits, Ctrl+C, or the console is closed
If the controller can't confirm it stopped, the GUI tells you to press STOP on
the controller's front panel. (A hard kill or power loss of the PC can't be
caught by software.)

Code layout (package fluoscan/):
    config.py   settings (ports, paths, limits) and the protocol fields shown in the GUI
    stage.py    Instec mK2000B wrapper, simulator, emergency shutdown hooks
    camera.py   Thorlabs camera thread (live streaming, settings, frame averaging)
    scan.py     temperature schedules + the scan worker (step and ramp modes)
    gui.py      Tkinter / matplotlib user interface

Run with Anaconda Python (it has instec, thorlabs_tsi_sdk, tifffile):
    "C:\\ProgramData\\anaconda3\\python.exe" temp_fluorescence_scan.py
    "C:\\ProgramData\\anaconda3\\python.exe" temp_fluorescence_scan.py --simulate   # no hardware
"""

import argparse
import tkinter as tk

from fluoscan.config import SIM_STATE
from fluoscan.stage import SimStage, Stage, emergency_all_off, install_safety_hooks
from fluoscan.camera import CameraThread
from fluoscan.scan import ScanWorker, build_schedule, build_setpoints
from fluoscan.gui import App

# re-exported so tests / notebooks can `import temp_fluorescence_scan as tfs`
__all__ = ["App", "CameraThread", "ScanWorker", "SimStage", "Stage", "SIM_STATE",
           "build_schedule", "build_setpoints", "emergency_all_off", "install_safety_hooks", "main"]


def main():
    ap = argparse.ArgumentParser(description="Temperature-dependent fluorescence scan")
    ap.add_argument("--simulate", action="store_true", help="run with a fake stage and camera")
    args = ap.parse_args()

    install_safety_hooks()
    root = tk.Tk()
    app = App(root, simulate=args.simulate)
    try:
        root.mainloop()
    finally:
        if app.abort_evt is not None:
            app.abort_evt.set()
        if app.worker is not None and app.worker.is_alive():
            app.worker.join(timeout=30)      # worker's finally turns the heater off
        emergency_all_off("main loop exited")
        if app.camera is not None:
            app.camera.stop()


if __name__ == "__main__":
    main()
