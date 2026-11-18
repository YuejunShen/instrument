"""
Step the Instec mK2000B heating stage from 23 C to 50 C in 2 C steps.

At each set point the script:
  1. sends a HOLD command to the new set point,
  2. waits until the stage has equilibrated
     (|PV - TSP| <= STABLE_TOL for STABLE_TIME seconds in a row),
  3. holds there for HOLD_TIME seconds (10 s),
then moves on to the next set point.

Set points: 23, 25, 27, ..., 49, 50  (the last step is 1 C so it ends exactly at 50 C).

Every reading is logged to a CSV file (time, phase, set point, PV).

Requires:  pip install pyserial instec      (Python >= 3.10)
Usage:     python instec_temperature_step_ramp.py              # uses PORT below
           python instec_temperature_step_ramp.py --port COM5
           python instec_temperature_step_ramp.py --list       # list serial ports
"""

import argparse
import csv
import sys
import time
from datetime import datetime

import instec

# ---------------- user settings ----------------
PORT = "COM4"          # USB serial port of the mK2000B (check Device Manager)
BAUDRATE = 38400       # default for the mK2000B

T_START = 23.0         # C
T_END = 50.0           # C
T_STEP = 2.0           # C

STABLE_TOL = 0.1       # C, allowed |PV - TSP| to count as equilibrated
STABLE_TIME = 30.0     # s, PV must stay within tolerance this long
EQUIL_TIMEOUT = 900.0  # s, give up waiting for equilibrium after this
HOLD_TIME = 10.0       # s, hold at each set point after equilibrating
POLL_INTERVAL = 0.5    # s, how often to read the temperature

STOP_AT_END = True     # True: stop temperature control when finished
# ------------------------------------------------


def build_setpoints(t_start, t_end, step):
    """23, 25, ..., 49, then 50 so the sequence always ends on t_end."""
    pts = []
    t = t_start
    while t < t_end - 1e-9:
        pts.append(round(t, 3))
        t += step
    pts.append(round(t_end, 3))
    return pts


def log_row(writer, f, t0, phase, tsp, pv):
    writer.writerow([f"{time.time() - t0:.2f}", phase, f"{tsp:.3f}", f"{pv:.3f}"])
    f.flush()


def wait_for_equilibrium(ctrl, tsp, writer, f, t0):
    """Block until PV stays within STABLE_TOL of tsp for STABLE_TIME seconds."""
    start = time.time()
    stable_since = None
    while True:
        pv = ctrl.get_process_variable()
        log_row(writer, f, t0, "equilibrating", tsp, pv)
        now = time.time()

        if abs(pv - tsp) <= STABLE_TOL:
            if stable_since is None:
                stable_since = now
            stable_for = now - stable_since
        else:
            stable_since = None
            stable_for = 0.0

        print(f"\r  [equilibrating] TSP={tsp:6.2f}  PV={pv:7.3f}  "
              f"dT={pv - tsp:+.3f}  stable {stable_for:5.1f}/{STABLE_TIME:.0f}s",
              end="", flush=True)

        if stable_for >= STABLE_TIME:
            print()
            return True
        if now - start > EQUIL_TIMEOUT:
            print(f"\n  WARNING: did not equilibrate within {EQUIL_TIMEOUT:.0f}s, continuing.")
            return False
        time.sleep(POLL_INTERVAL)


def hold_for(ctrl, tsp, duration, writer, f, t0):
    """Keep holding at tsp for `duration` seconds while logging PV."""
    end = time.time() + duration
    while True:
        pv = ctrl.get_process_variable()
        log_row(writer, f, t0, "holding", tsp, pv)
        remaining = end - time.time()
        print(f"\r  [holding]       TSP={tsp:6.2f}  PV={pv:7.3f}  "
              f"remaining {max(remaining, 0):5.1f}s   ", end="", flush=True)
        if remaining <= 0:
            print()
            return
        time.sleep(min(POLL_INTERVAL, remaining))


def list_ports():
    from serial.tools import list_ports as lp
    ports = list(lp.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        print(f"{p.device:8s}  {p.description}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=PORT, help=f"serial port (default {PORT})")
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = parser.parse_args()

    if args.list:
        list_ports()
        return

    setpoints = build_setpoints(T_START, T_END, T_STEP)
    print("Set points (C):", ", ".join(f"{t:g}" for t in setpoints))

    ctrl = instec.MK2000B(instec.mode.USB, BAUDRATE, args.port)
    try:
        ctrl.connect()
    except Exception as e:
        print(f"Could not connect on {args.port}: {e}")
        print("Available ports:")
        list_ports()
        sys.exit(1)

    print("Connected:", ctrl.get_system_information())

    log_name = f"temp_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
    t0 = time.time()
    with open(log_name, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "phase", "tsp_C", "pv_C"])
        print(f"Logging to {log_name}")

        try:
            for i, tsp in enumerate(setpoints, 1):
                print(f"\nStep {i}/{len(setpoints)}: set point {tsp:g} C")
                # hold_check raises ValueError if tsp is outside the stage's operation range
                ctrl.hold_check(tsp)
                wait_for_equilibrium(ctrl, tsp, writer, f, t0)
                hold_for(ctrl, tsp, HOLD_TIME, writer, f, t0)

            print(f"\nDone. Total time {(time.time() - t0) / 60:.1f} min.")
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            if STOP_AT_END:
                ctrl.stop()
                print("Temperature control stopped.")
            else:
                print(f"Leaving controller holding at {ctrl.get_set_point_temperature():g} C.")
            ctrl.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
