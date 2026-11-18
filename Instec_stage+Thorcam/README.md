# Instec heating stage + Thorlabs camera: temperature-dependent fluorescence

Control code for an **Instec mK2000B** temperature controller (heating stage) and a
**Thorlabs scientific camera** (tested with the CS135MU). It measures fluorescence
intensity as a function of temperature and has a live GUI.

| File | What it does |
|---|---|
| `temp_fluorescence_scan.py` | Main program. Starts the GUI (`--simulate` runs it without hardware). |
| `run_scan.bat` | Double-click launcher on Windows (uses Anaconda Python). |
| `fluoscan/` | The package behind the GUI (see [Code layout](#code-layout)). |
| `instec_temperature_step_ramp.py` | Stand-alone command-line script: step the stage from 23 °C to 50 °C in 2 °C steps, equilibrating and holding at each step. No camera. |

## Features

- **Two temperature modes**, chosen in the GUI:
  - **Step:** go to each set point, wait until equilibrated (|PV − SP| ≤ tolerance for N s),
    hold a few seconds, then take a photo.
  - **Continuous ramp:** equilibrate at the start temperature, then the controller ramps at a
    constant rate (°C/min, up to 30 °C/min on the mK2000B) and a photo is taken every N seconds.
- **Repeated sweeps** (start → end → start → …) so heating and cooling curves can be
  compared. If they overlap, the change is reversible (no photobleaching).
- **Live view** with adjustable gain and exposure, frame averaging, and an ROI you drag on the image.
- **Per-pixel dark frame** (laser off), measured before the run and subtracted from every photo.
- **Live plots** of temperature vs time and ROI intensity vs temperature (one colour per sweep,
  ▲ heating / ▼ cooling), saved as a PNG at the end.
- **Remembers your last settings** (`last_settings.json`, next to the script).
- **Camera watchdog:** if frames stop arriving, it re-arms and then reopens the camera, and shows a
  warning on the image.

## Safety

The heater is switched **off** (`stop()`, then the status is read back to confirm it is STOP) when:

- the scan finishes, or you press **Abort** or **HEATER OFF NOW**
- any error happens (camera, stage, file writing, GUI)
- the stage reads above the **safety cutoff** temperature
- the stage temperature cannot be read 3 times in a row
- the window is closed, the program exits, Ctrl+C is pressed, or the console window is closed

If the controller cannot confirm it stopped, the GUI tells you to press STOP on the front panel.
A hard kill of the Python process (e.g. Task Manager) or a PC power loss cannot be caught
by software. After that, stop the stage manually.

## Installation

Windows, Python ≥ 3.10 (developed with Anaconda Python 3.10).

```bash
pip install -r requirements.txt
```

- **Instec:** [`instec`](https://pypi.org/project/instec/) (pyinstec). The controller appears as
  a USB serial port (default `COM4`, 38400 baud). The port can be changed in the GUI.
- **Thorlabs camera:** install ThorCam / *Scientific Camera Interfaces*, then
  - `pip install` the `thorlabs_tsi_sdk` wheel from its `SDK/Python Toolkit` folder;
  - point the program to the native DLL folder (`thorlabs_tsi_camera_sdk.dll`), either by
    editing `DLL_PATH` in `fluoscan/config.py` or by setting an environment variable:
    ```bat
    setx THORLABS_TSI_DLL_PATH "C:\path\to\Native_64_lib"
    ```

## Usage

```bash
python temp_fluorescence_scan.py              # real hardware
python temp_fluorescence_scan.py --simulate   # fake stage + camera, for trying the GUI
```

1. Pick the stage port and the camera. Set gain and exposure, then drag an ROI on the image.
2. Choose **Step** or **Continuous ramp** and fill in the protocol fields (fields the
   chosen mode does not use are greyed out).
3. Press **Start scan**. The program offers to measure a dark frame: turn the laser off,
   press OK, then turn it back on. It checks that the laser really is back on before heating.
4. Confirm the summary dialog. It shows the set points or ramp, the estimated duration, the
   number of photos and the disk space needed.

### Output (one folder per run: `<save folder>/<date>_<time>_<sample>/`)

| File | Content |
|---|---|
| `results.csv` | One row per photo: temperatures (set point, PV), ROI mean (dark-subtracted and raw), frame-to-frame std, spatial std, median, saturation fraction, gain, exposure, image file. |
| `temperature_log.csv` | Every temperature reading (time, phase, set point, PV). |
| `images/*.tif` | Averaged **raw** images (float32). Dark-subtracted image = image − `dark_frame.tif`. In ramp mode only every N-th photo is saved (setting), plus the start and end of each sweep. |
| `dark_frame.tif` | The averaged dark frame (if used). |
| `run_info.json` | All parameters, ROI, camera info, dark-frame statistics. |
| `intensity_vs_temperature.png` | Summary plot. |
| `run_log.txt` | The GUI log. |

## Code layout

```
temp_fluorescence_scan.py   entry point (argument parsing, start GUI, final safety shutdown)
fluoscan/
    config.py   settings (ports, paths, limits) and the protocol fields shown in the GUI
    stage.py    Instec mK2000B wrapper (hold / ramp / safe_off), simulator, shutdown hooks
    camera.py   camera thread: streaming, gain/exposure changes, frame averaging, watchdog
    scan.py     temperature schedules and the scan worker (step and ramp modes)
    gui.py      Tkinter + matplotlib user interface
```

Threads: the camera thread owns the camera, the scan worker owns the stage during a run, and
the GUI talks to both through a queue polled every 100 ms.
