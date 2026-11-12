import os
import time
import datetime
import csv

import numpy as np
import pyvisa


# -----------------------------
# User-editable parameters
# -----------------------------
device_name = "Autumn_2D_A8"  # Added to the output filename
waveform_type = "triangle"  # Options: sine, square, triangle, ramp
# frequencies_khz = [1,2,3,5,7,10]  # Frequencies in kHz
# amplitudes_v = [0.1,0.24,0.3,0.5,0.8,1]  # Sample peak amplitudes in volts
frequencies_khz = [0.5,1,2]
# Requested PEAK voltage at the high-impedance sample (not the generator or
# monitor voltage). For example, 0.8 means +/-0.8 V at the sample.
amplitudes_v = [30]
number_of_periods = 3  # Number of waveform periods captured per measurement
use_current_amp = True  # Set to True if using a current amplifier with a gain of 2x
current_preamplifier_gain_a_per_v = 1e-5  # Conversion from scope channel 1 voltage to current (A/V)
electrode_area_m2 = (10e-6)**2  # Active electrode area in square metres
voltage_amplifier_gain = 20.0  # Magnitude only; X-20 output is inverted relative to its input
generator_high_z_voltage_factor = 2.0  # 50-ohm-calibrated generator driving the high-Z X-20 input
# The output-monitor waveform is already inverted relative to the generator,
# just like the sample waveform. Use +40 here to preserve its measured sign.
monitor_to_sample_voltage_gain = 40.0
# Safety limit for the PEAK voltage actually applied to the sample.
max_allowed_amplitude_v = 100

target_time_step_s = 1e-7

function_generator_address = "USB0::0x0957::0x2707::MY57301577::INSTR"
oscope_address = "USB0::0x0957::0x9005::MY50140103::INSTR"
SAVE_PATH = r"D:\Ferroelectric_BTO_optimization\20261102\Autumn_2D_A8"


def setup_function_generator(waveform_type, frequency_khz, sample_amplitude_v):
    rm = pyvisa.ResourceManager()
    fg = rm.open_resource(function_generator_address)
    fg.write_termination = "\n"
    fg.read_termination = "\n"
    fg.write("*RST")
    fg.write("*CLS")

    waveform_map = {
        "sine": "SIN",
        "square": "SQU",
        "triangle": "TRI",
        "ramp": "RAMP",
        "pulse": "PULS",
    }

    if waveform_type.lower() not in waveform_map:
        raise ValueError(f"Unsupported waveform type: {waveform_type}. Choose from {list(waveform_map.keys())}")

    freq_hz = float(frequency_khz) * 1e3
    sample_amplitude_v = float(sample_amplitude_v)
    # The generator amplitude is specified for a matched 50-ohm load. The X-20
    # input is high impedance, so its input sees twice the generator setting.
    generator_amplitude_v = sample_amplitude_v / (
        generator_high_z_voltage_factor * voltage_amplifier_gain
    )
    generator_amplitude_vpp = 2.0 * generator_amplitude_v

    fg.write(f"FUNC {waveform_map[waveform_type.lower()]}")
    fg.write(f"FREQ {freq_hz}")
    # Keep the generator amplitude calibrated as a 50-ohm-load value. Because
    # the physical X-20 input is high impedance, it receives twice this value.
    fg.write("OUTP:LOAD 50")
    fg.write(f"VOLT {generator_amplitude_vpp}")
    fg.write("VOLT:OFFS 0")
    fg.write("OUTP ON")
    time.sleep(0.5)

    print(
        f"Uploaded {waveform_type} at {frequency_khz} kHz: generator "
        f"50-ohm setting {generator_amplitude_v:g} V peak "
        f"({generator_amplitude_vpp:g} Vpp), "
        f"for a requested sample voltage of {sample_amplitude_v:g} V peak."
    )

    fg.close()
    rm.close()


def turn_off_function_generator():
    rm = pyvisa.ResourceManager()
    fg = rm.open_resource(function_generator_address)
    try:
        fg.write("OUTP OFF")
        print("Function generator output turned OFF.")
    finally:
        fg.close()
        rm.close()


def capture_waveform(scope, channel, capture_duration_s):
    scope.write(f":WAVEFORM:SOURCE CHANNEL{channel}")
    scope.write(":WAVEFORM:FORMAT ASCII")

    x_increment = float(scope.query(":WAVeform:XINCrement?"))
    if not np.isclose(x_increment, target_time_step_s, rtol=0.02, atol=1e-12):
        print(f"Warning: scope returned {x_increment:g} s/point, requested {target_time_step_s:g}")
    x_origin = float(scope.query(":WAVeform:XORigin?"))
    raw_data = scope.query(":WAVEFORM:DATA?").strip()

    try:
        raw_values = np.array([float(i) for i in raw_data.split(",") if i.strip()], dtype=np.float64)
    except ValueError as exc:
        print(f"Error converting waveform data for Channel {channel}: {exc}")
        return None, None

    # Infiniium ASCII waveform samples are already returned in calibrated
    # engineering units. YINCREMENT/YORIGIN conversion is only required when
    # reading integer BYTE/WORD waveform data.
    voltage_values = raw_values
    time_values = np.arange(len(voltage_values)) * x_increment + x_origin
    print(f"Channel {channel}: received {len(voltage_values)} points at {x_increment:.3g} s/point")
    if len(time_values) == 0:
        return None, None

    end_time = time_values[0] + capture_duration_s
    mask = (time_values >= time_values[0]) & (time_values <= end_time)
    if np.count_nonzero(mask) < 10:
        return time_values, voltage_values

    return time_values[mask], voltage_values[mask]


def save_data(device_name, waveform_type, frequency_khz, sample_amplitude_v, time_values, voltage_values, current_values):
    os.makedirs(SAVE_PATH, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def sanitize(value):
        return str(value).replace("-", "m")

    filename = (
        f"{device_name}_{waveform_type}_f{sanitize(frequency_khz)}kHz_"
        f"sample_amp{sanitize(sample_amplitude_v)}Vpeak_"
        f"area{electrode_area_m2:.6g}m2_"
        f"gain{sanitize(current_preamplifier_gain_a_per_v)}AperV_{timestamp}.csv"
    )
    csv_path = os.path.join(SAVE_PATH, filename)

    with open(csv_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Time (s)", "Current (A)", "Voltage (V)"])
        writer.writerows(zip(time_values, current_values, voltage_values))

    print(f"Data saved to {csv_path} successfully!")


def configure_scope(scope, frequency_hz, sample_amplitude_v):
    period_s = 1.0 / frequency_hz
    if number_of_periods <= 0:
        raise ValueError("number_of_periods must be greater than zero")
    capture_duration_s = float(number_of_periods) * period_s
    requested_points = int(round(capture_duration_s / target_time_step_s)) + 1
    time_div_s = capture_duration_s / 10.0
    time_div_s = max(time_div_s, 1e-6)

    trigger_level = 0.2
    expected_monitor_peak_v = sample_amplitude_v / monitor_to_sample_voltage_gain
    monitor_scale_v_per_div = max(expected_monitor_peak_v / 3.0, 0.001)

    scope.write("*RST")
    scope.write("*CLS")
    scope.write(":CHANNEL1:DISPLAY ON")
    scope.write(":CHANNEL2:DISPLAY ON")
    scope.write(":CHANNEL3:DISPLAY ON")
    # The X-20 monitor conversion factor of 40 is specified for a 50-ohm load.
    scope.write(":CHANNEL2:INPUT DC50")
    scope.write(f":TIMEBASE:SCALE {time_div_s}")
    scope.write(":TIMEBASE:POSITION 0")
    scope.write(":CHANNEL1:SCALE 0.5")
    scope.write(f":CHANNEL2:SCALE {monitor_scale_v_per_div}")
    scope.write(":CHANNEL3:SCALE 1")
    scope.write(":CHANNEL1:OFFSET 0")
    scope.write(":CHANNEL2:OFFSET 0")
    scope.write(":CHANNEL3:OFFSET 0")
    scope.write(":WAVEFORM:FORMAT ASCII")
    scope.write(":ACQUIRE:SRATE:AUTO OFF")
    scope.write(f":ACQUIRE:SRATE {1.0 / target_time_step_s}")
    scope.write(f":ACQUIRE:POINTS {requested_points}")
    scope.write(f":WAVEFORM:POINTS {requested_points}")

    scope.write(":ACQUIRE:MODE HRESOLUTION")
    scope.write(":ACQuire:AVERage ON")
    scope.write(":ACQuire:AVERage:COUNt 256") 
    scope.write(":TRIGGER:MODE EDGE")
    scope.write(":TRIGGER:EDGE:SOURCE CHANNEL3")
    scope.write(":TRIGGER:EDGE:SLOPE POSITIVE")
    scope.write(":TRIGGER:HYSTERESIS NREJECT")
    scope.write(":TRIGGER:SWEEP TRIGGERED")
    scope.write(f":TRIGger:LEVel CHANnel3,{trigger_level}")
    print(f"Single acquisition; requesting {requested_points} points at {target_time_step_s:g} s/point")

    return capture_duration_s


def main():
    if electrode_area_m2 <= 0 or target_time_step_s <= 0:
        raise ValueError("electrode_area_m2 and target_time_step_s must be greater than zero")
    if (
        voltage_amplifier_gain <= 0
        or generator_high_z_voltage_factor <= 0
        or monitor_to_sample_voltage_gain <= 0
    ):
        raise ValueError("Voltage conversion factors must be greater than zero")
    if max_allowed_amplitude_v <= 0:
        raise ValueError("max_allowed_amplitude_v must be greater than zero")
    for frequency_khz in frequencies_khz:
        for requested_amplitude_v in amplitudes_v:
            sample_amplitude_v = float(requested_amplitude_v)
            if sample_amplitude_v < 0:
                raise ValueError("amplitudes_v entries must be non-negative peak voltages")
            if sample_amplitude_v > max_allowed_amplitude_v:
                print(
                    f"Skipping measurement: requested sample amplitude {sample_amplitude_v:g} V peak "
                    f"exceeds the sample limit of {max_allowed_amplitude_v:g} V peak."
                )
                continue

            print(
                f"\nRunning measurement: {waveform_type} @ {frequency_khz} kHz, "
                f"requested sample amplitude {sample_amplitude_v:g} V peak"
            )
            setup_function_generator(waveform_type, frequency_khz, sample_amplitude_v)
            time.sleep(2)  # Wait for the system to stabilize after uploading a new waveform

            rm = pyvisa.ResourceManager()
            scope = rm.open_resource(oscope_address)
            scope.write_termination = "\n"
            scope.read_termination = "\n"
            scope.timeout = 100000
            print("Oscilloscope ID:", scope.query("*IDN?"))

            frequency_hz = float(frequency_khz) * 1e3
            capture_duration_s = configure_scope(scope, frequency_hz, sample_amplitude_v)

            # Perform one acquisition and explicitly read the completion reply.
            # This prevents a pending/unterminated query in the scope I/O queue.
            scope.query(":DIGITIZE CHANNEL1,CHANNEL2;*OPC?")

            time_ch1, current_raw = capture_waveform(scope, 1, capture_duration_s)
            time_ch2, voltage_raw = capture_waveform(scope, 2, capture_duration_s)

            if time_ch1 is None or time_ch2 is None or current_raw is None or voltage_raw is None:
                print("Failed to capture waveform data. Skipping save.")
                scope.write(":RUN")
                scope.close()
                rm.close()
                continue

            if len(time_ch1) != len(current_raw) or len(time_ch2) != len(voltage_raw):
                print("Captured waveform lengths do not match. Skipping save.")
                scope.write(":RUN")
                scope.close()
                rm.close()
                continue
            if use_current_amp==True:
                current_values = 2*current_raw * current_preamplifier_gain_a_per_v
            else:
                current_values = current_raw * current_preamplifier_gain_a_per_v
            # Channel 2 is the X-20 output monitor terminated into 50 ohms.
            # It already contains the amplifier inversion, so multiplying by a
            # positive 40 preserves the polarity actually present at the sample.
            voltage_values = voltage_raw * monitor_to_sample_voltage_gain

            save_data(
                device_name=device_name,
                waveform_type=waveform_type,
                frequency_khz=frequency_khz,
                sample_amplitude_v=sample_amplitude_v,
                time_values=time_ch1,
                voltage_values=voltage_values,
                current_values=current_values,
            )

            scope.write(":RUN")
            scope.close()
            rm.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        turn_off_function_generator()
