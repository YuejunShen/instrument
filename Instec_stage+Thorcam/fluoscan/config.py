"""User settings, protocol fields and small shared helpers."""
import os

# ---------------- hardware / paths ----------------
# Folder with the Thorlabs TSI native DLLs (thorlabs_tsi_camera_sdk.dll, from ThorCam /
# "Scientific Camera Interfaces"). Override with the THORLABS_TSI_DLL_PATH environment variable.
DLL_PATH = os.environ.get("THORLABS_TSI_DLL_PATH",
                          r"D:\Liquid crystal\control_code\Native Toolkit\dlls\Native_64_lib")
DEFAULT_PORT = "COM4"          # Instec mK2000B USB serial port
BAUDRATE = 38400
DEFAULT_SAVE_DIR = r"D:\Active_matter\temperature_scans"
HARD_MAX_TEMP = 120.0          # C, the GUI refuses any safety cutoff above this
POLL_S = 0.5                   # s, temperature polling interval
DISPLAY_BIN = 2                # live view shows every 2nd pixel (speed)

MAX_RAMP_RATE = 30.0           # C/min, mK2000B ramp-rate limit (TEMP:RTR?)

# (key, label, default, default in --simulate, used in mode: "both" / "step" / "ramp")
PROTOCOL_FIELDS = [
    ("t_start",         "Start T (°C)",                 23.0,  23.0,  "both"),
    ("t_end",           "End T (°C)",                   50.0,  31.0,  "both"),
    ("n_sweeps",        "Sweeps (1=up, 2=up+down, …)",  1,     3,     "both"),
    ("t_step",          "Step (°C)",                    2.0,   2.0,   "step"),
    ("hold_s",          "Extra hold before photo (s)",  10.0,  1.0,   "step"),
    ("rate",            "Ramp rate (°C/min)",           0.5,   6.0,   "ramp"),
    ("interval_s",      "Photo every (s)",              30.0,  2.0,   "ramp"),
    ("save_every",      "Save image every N photos (0=never)", 1, 5,  "ramp"),
    ("tol",             "Equilibrium tolerance ± (°C)", 0.1,   0.1,   "both"),
    ("stable_s",        "Stay within tolerance for (s)", 30.0, 3.0,   "both"),
    ("equil_timeout_s", "Equilibration timeout (s)",    900.0, 60.0,  "both"),
    ("max_safe",        "Safety cutoff T (°C)",         55.0,  40.0,  "both"),
]


def console(s):
    """print() that can never raise (e.g. a console that cannot show °/→ characters)."""
    try:
        print(s, flush=True)
    except Exception:
        try:
            print(str(s).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


class SafetyError(RuntimeError):
    pass


class AbortRequested(Exception):
    pass


# Shared state of the --simulate mode (fake stage temperature, fake laser on/off)
SIM_STATE = {"T": 22.6, "laser": True}
