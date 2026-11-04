
#!/usr/bin/env python3

"""
=========================================================
 AIR QUALITY & ENVIRONMENT MONITORING
 Raspberry Pi + Python 3

 Sensors:
 1. Sensirion SPS30       I2C 0x69
 2. DFRobot MiCS-4514     I2C 0x75
 3. ADS1115 + MQ-136      I2C 0x48
 4. GY-SHT41              I2C 0x44
 5. GY-BMP280             I2C 0x76

 Output:
 - Terminal monitoring
 - CSV data logging

 MiCS-4514:
 - DFRobot SEN0377 logic
 - Wake-up
 - 180 seconds warm-up
 - 10 samples R0 calibration
 - Gas conversion from DFRobot library
=========================================================
"""

import time
import csv
import os
import sys

from datetime import datetime

import smbus2

import board

import adafruit_sht4x
import adafruit_bmp280

import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

from sensirion_i2c_driver import (
    LinuxI2cTransceiver,
    I2cConnection,
    CrcCalculator
)

from sensirion_driver_adapters.i2c_adapter.i2c_channel import (
    I2cChannel
)

from sensirion_i2c_sps30.device import Sps30Device
from sensirion_i2c_sps30.commands import OutputFormat


# =========================================================
# CONFIGURATION
# =========================================================

I2C_BUS = 1
I2C_PORT = "/dev/i2c-1"

ADDR_SPS30 = 0x69
ADDR_MICS = 0x75
ADDR_ADS1115 = 0x48
ADDR_SHT41 = 0x44
ADDR_BMP280 = 0x76

SAMPLE_INTERVAL = 2.0

MICS_CALIBRATION_TIME = 180
MICS_CALIBRATION_SAMPLES = 10

CSV_FILE = "environment_monitoring.csv"


# =========================================================
# GLOBAL VARIABLES
# =========================================================

bus = None
i2c = None

sps30 = None
sps30_connection = None

mics_ready = False

ads = None
mq136 = None

sht41 = None
bmp280 = None

mics_r0_ox = 1.0
mics_r0_red = 1.0


# =========================================================
# CSV CONFIGURATION
# =========================================================

CSV_HEADER = [
    "timestamp",

    # SPS30
    "PM1_0",
    "PM2_5",
    "PM4_0",
    "PM10",

    "NC0_5",
    "NC1_0",
    "NC2_5",
    "NC4_0",
    "NC10",

    "TypicalParticleSize",

    # MiCS4514
    "MICS_OX_ADC",
    "MICS_RED_ADC",
    "MICS_POWER",

    "MICS_RATIO_OX",
    "MICS_RATIO_RED",

    "CO",
    "CH4",
    "ETHANOL",
    "H2",
    "NH3",
    "NO2",

    # MQ136
    "MQ136_RAW",
    "MQ136_VOLTAGE",

    # SHT41
    "SHT41_TEMPERATURE",
    "SHT41_HUMIDITY",

    # BMP280
    "BMP280_TEMPERATURE",
    "BMP280_PRESSURE",
    "BMP280_ALTITUDE"
]


def create_csv():

    if not os.path.exists(CSV_FILE):

        with open(
            CSV_FILE,
            "w",
            newline=""
        ) as file:

            writer = csv.writer(file)
            writer.writerow(CSV_HEADER)

        print("[CSV] File created:", CSV_FILE)


def save_csv(data):

    row = [
        data.get(key, "")
        for key in CSV_HEADER
    ]

    with open(
        CSV_FILE,
        "a",
        newline=""
    ) as file:

        writer = csv.writer(file)
        writer.writerow(row)


# =========================================================
# INITIALIZE I2C
# =========================================================

def initialize_i2c():

    global bus, i2c

    print("\n[INIT] Initializing I2C...")

    bus = smbus2.SMBus(I2C_BUS)

    i2c = board.I2C()

    print("[OK] I2C initialized")


# =========================================================
# SPS30
# =========================================================

def initialize_sps30():

    global sps30, sps30_connection

    print("\n[INIT] SPS30...")

    try:

        sps30_connection = LinuxI2cTransceiver(
            I2C_PORT
        )

        channel = I2cChannel(
            I2cConnection(sps30_connection),
            slave_address=ADDR_SPS30,
            crc=CrcCalculator(
                8, 0x31, 0xFF, 0x00
            )
        )

        sps30 = Sps30Device(channel)

        try:
            sps30.stop_measurement()
        except Exception:
            pass

        sps30.start_measurement(
            OutputFormat.OUTPUT_FORMAT_FLOAT
        )

        time.sleep(0.1)

        print("[OK] SPS30 initialized")

        try:

            print(
                "[SPS30] Serial:",
                sps30.read_serial_number()
            )

        except Exception:
            pass

        return True

    except Exception as e:

        print("[ERROR] SPS30:", e)

        sps30 = None

        return False


def read_sps30():

    if sps30 is None:
        return None

    values = sps30.read_measurement_values_float()

    (
        pm1,
        pm25,
        pm4,
        pm10,
        nc05,
        nc1,
        nc25,
        nc4,
        nc10,
        particle_size
    ) = values

    return {

        "PM1_0": pm1,
        "PM2_5": pm25,
        "PM4_0": pm4,
        "PM10": pm10,

        "NC0_5": nc05,
        "NC1_0": nc1,
        "NC2_5": nc25,
        "NC4_0": nc4,
        "NC10": nc10,

        "TypicalParticleSize": particle_size

    }


# =========================================================
# MiCS-4514 DFRobot SEN0377
# =========================================================

MICS_OX_REG = 0x04
MICS_POWER_MODE_REG = 0x0A

MICS_SLEEP = 0x00
MICS_WAKEUP = 0x01


def read_mics_raw():

    data = bus.read_i2c_block_data(
        ADDR_MICS,
        MICS_OX_REG,
        6
    )

    ox_adc = (data[0] << 8) | data[1]

    red_adc = (data[2] << 8) | data[3]

    power = (data[4] << 8) | data[5]

    ox = max(0, power - ox_adc)

    red = max(0, power - red_adc)

    return ox, red, power


def wake_mics():

    mode = bus.read_byte_data(
        ADDR_MICS,
        MICS_POWER_MODE_REG
    )

    print(
        f"[MICS] Power mode: 0x{mode:02X}"
    )

    if mode == MICS_SLEEP:

        bus.write_byte_data(
            ADDR_MICS,
            MICS_POWER_MODE_REG,
            MICS_WAKEUP
        )

        time.sleep(0.1)

        print("[MICS] Wake-up successful")

    elif mode == MICS_WAKEUP:

        print("[MICS] Sensor already awake")

    else:

        raise RuntimeError(
            f"Unexpected power mode: {mode}"
        )


def calibrate_mics():

    global mics_r0_ox
    global mics_r0_red

    ox_values = []
    red_values = []

    print("\n[MICS] Place sensor in clean air.")

    print(
        f"[MICS] Warm-up: "
        f"{MICS_CALIBRATION_TIME} seconds"
    )

    for remaining in range(
        MICS_CALIBRATION_TIME,
        0,
        -1
    ):

        print(
            f"\r[MICS] Remaining: {remaining:3d}s",
            end="",
            flush=True
        )

        time.sleep(1)

    print("\n[MICS] Collecting R0 baseline...")

    for index in range(
        MICS_CALIBRATION_SAMPLES
    ):

        ox, red, power = read_mics_raw()

        if ox <= 0 or red <= 0:

            raise RuntimeError(
                "Invalid MICS baseline. "
                "Check sensor connection."
            )

        ox_values.append(ox)

        red_values.append(red)

        print(
            f"[MICS] Calibration "
            f"{index+1}/{MICS_CALIBRATION_SAMPLES} "
            f"| OX={ox} RED={red} POWER={power}"
        )

        time.sleep(1)

    mics_r0_ox = (
        sum(ox_values) / len(ox_values)
    )

    mics_r0_red = (
        sum(red_values) / len(red_values)
    )

    print("\n[MICS] Calibration completed!")

    print(
        f"[MICS] R0 OX  = {mics_r0_ox:.2f}"
    )

    print(
        f"[MICS] R0 RED = {mics_r0_red:.2f}"
    )


def initialize_mics():

    global mics_ready

    print("\n[INIT] MiCS-4514...")

    try:

        # Verify I2C communication
        mode = bus.read_byte_data(
            ADDR_MICS,
            MICS_POWER_MODE_REG
        )

        print(
            f"[MICS] Detected at 0x{ADDR_MICS:02X}"
        )

        wake_mics()

        calibrate_mics()

        mics_ready = True

        print("[OK] MiCS-4514 initialized")

        return True

    except Exception as e:

        print("[ERROR] MiCS-4514:", e)

        mics_ready = False

        return False


# =========================================================
# MiCS GAS CONVERSION
# DFRobot_MICS.cpp equations
# =========================================================

def get_mics_gas(
    gas,
    ratio_red,
    ratio_ox
):

    if gas == "CO":

        if ratio_red > 0.425:
            return 0.0

        value = (
            (0.425 - ratio_red) / 0.000405
        )

        return (
            min(1000.0, value)
            if value >= 1
            else 0.0
        )

    elif gas == "CH4":

        if ratio_red > 0.786:
            return 0.0

        value = (
            (0.786 - ratio_red) / 0.000023
        )

        return (
            min(25000.0, value)
            if value >= 1000
            else 0.0
        )

    elif gas == "ETHANOL":

        if ratio_red > 0.306:
            return 0.0

        value = (
            (0.306 - ratio_red) / 0.00057
        )

        return (
            min(500.0, value)
            if value >= 10
            else 0.0
        )

    elif gas == "H2":

        if ratio_red > 0.279:
            return 0.0

        value = (
            (0.279 - ratio_red) / 0.00026
        )

        return (
            min(1000.0, value)
            if value >= 1
            else 0.0
        )

    elif gas == "NH3":

        if ratio_red > 0.8:
            return 0.0

        value = (
            (0.8 - ratio_red) / 0.0015
        )

        return (
            min(500.0, value)
            if value >= 10
            else 0.0
        )

    elif gas == "NO2":

        if ratio_ox < 1.1:
            return 0.0

        value = (
            (ratio_ox - 0.045) / 6.13
        )

        return (
            min(10.0, value)
            if value >= 0.1
            else 0.0
        )

    return 0.0


def get_mics_data():

    if not mics_ready:
        return None

    ox, red, power = read_mics_raw()

    ratio_ox = ox / mics_r0_ox

    ratio_red = red / mics_r0_red

    gases = {

        "CO": get_mics_gas(
            "CO", ratio_red, ratio_ox
        ),

        "CH4": get_mics_gas(
            "CH4", ratio_red, ratio_ox
        ),

        "ETHANOL": get_mics_gas(
            "ETHANOL", ratio_red, ratio_ox
        ),

        "H2": get_mics_gas(
            "H2", ratio_red, ratio_ox
        ),

        "NH3": get_mics_gas(
            "NH3", ratio_red, ratio_ox
        ),

        "NO2": get_mics_gas(
            "NO2", ratio_red, ratio_ox
        )

    }

    return {

        "MICS_OX_ADC": ox,
        "MICS_RED_ADC": red,
        "MICS_POWER": power,

        "MICS_RATIO_OX": ratio_ox,
        "MICS_RATIO_RED": ratio_red,

        "CO": gases["CO"],
        "CH4": gases["CH4"],
        "ETHANOL": gases["ETHANOL"],
        "H2": gases["H2"],
        "NH3": gases["NH3"],
        "NO2": gases["NO2"]

    }


# =========================================================
# ADS1115 + MQ-136
# =========================================================

def initialize_ads1115():

    global ads, mq136

    print("\n[INIT] ADS1115 + MQ-136...")

    try:

        ads = ADS.ADS1115(
            i2c,
            address=ADDR_ADS1115
        )

        ads.gain = 1

        mq136 = AnalogIn(
            ads,
            ADS.P0
        )

        print("[OK] ADS1115 initialized")

        return True

    except Exception as e:

        print("[ERROR] ADS1115:", e)

        ads = None
        mq136 = None

        return False


def read_mq136():

    if mq136 is None:
        return None

    return {

        "MQ136_RAW": mq136.value,

        "MQ136_VOLTAGE": mq136.voltage

    }


# =========================================================
# SHT41
# =========================================================

def initialize_sht41():

    global sht41

    print("\n[INIT] SHT41...")

    try:

        sht41 = adafruit_sht4x.SHT4x(
            i2c,
            address=ADDR_SHT41
        )

        print("[OK] SHT41 initialized")

        return True

    except Exception as e:

        print("[ERROR] SHT41:", e)

        sht41 = None

        return False


def read_sht41():

    if sht41 is None:
        return None

    temperature, humidity = sht41.measurements

    return {

        "SHT41_TEMPERATURE": temperature,

        "SHT41_HUMIDITY": humidity

    }


# =========================================================
# BMP280
# =========================================================

def initialize_bmp280():

    global bmp280

    print("\n[INIT] BMP280...")

    try:

        bmp280 = adafruit_bmp280.Adafruit_BMP280_I2C(
            i2c,
            address=ADDR_BMP280
        )

        bmp280.sea_level_pressure = 1013.25

        print("[OK] BMP280 initialized")

        return True

    except Exception as e:

        print("[ERROR] BMP280:", e)

        bmp280 = None

        return False


def read_bmp280():

    if bmp280 is None:
        return None

    return {

        "BMP280_TEMPERATURE": bmp280.temperature,

        "BMP280_PRESSURE": bmp280.pressure,

        "BMP280_ALTITUDE": bmp280.altitude

    }


# =========================================================
# DISPLAY DATA
# =========================================================

def print_section(title):

    print("\n" + "-" * 65)

    print(title)

    print("-" * 65)


def print_data():

    data = {

        key: ""
        for key in CSV_HEADER
    }

    timestamp = datetime.now().isoformat(
        timespec="seconds"
    )

    data["timestamp"] = timestamp

    print("\n")
    print("=" * 65)

    print("       AIR QUALITY & ENVIRONMENT MONITORING")

    print("=" * 65)

    print("TIME:", timestamp)

    # -----------------------------------------------------
    # SPS30
    # -----------------------------------------------------

    print_section("SPS30 - PARTICULATE MATTER")

    try:

        result = read_sps30()

        if result:

            data.update(result)

            print(
                f"PM1.0  : {result['PM1_0']:.2f} ug/m3"
            )

            print(
                f"PM2.5  : {result['PM2_5']:.2f} ug/m3"
            )

            print(
                f"PM4.0  : {result['PM4_0']:.2f} ug/m3"
            )

            print(
                f"PM10   : {result['PM10']:.2f} ug/m3"
            )

            print(
                f"NC0.5  : {result['NC0_5']:.2f} #/cm3"
            )

            print(
                f"NC1.0  : {result['NC1_0']:.2f} #/cm3"
            )

            print(
                f"NC2.5  : {result['NC2_5']:.2f} #/cm3"
            )

            print(
                f"NC4.0  : {result['NC4_0']:.2f} #/cm3"
            )

            print(
                f"NC10   : {result['NC10']:.2f} #/cm3"
            )

            print(
                f"Particle Size: "
                f"{result['TypicalParticleSize']:.2f} um"
            )

    except Exception as e:

        print("[SPS30 ERROR]", e)

    # -----------------------------------------------------
    # MiCS-4514
    # -----------------------------------------------------

    print_section("MiCS-4514 - GAS SENSOR")

    try:

        result = get_mics_data()

        if result:

            data.update(result)

            print(
                f"OX={result['MICS_OX_ADC']:4d} "
                f"RED={result['MICS_RED_ADC']:4d} "
                f"POWER={result['MICS_POWER']:4d}"
            )

            print(
                f"R0_OX={mics_r0_ox:.1f} "
                f"R0_RED={mics_r0_red:.1f}"
            )

            print(
                f"Ratio_OX={result['MICS_RATIO_OX']:.4f} "
                f"Ratio_RED={result['MICS_RATIO_RED']:.4f}"
            )

            print(
                f"CO      : {result['CO']:.2f} ppm"
            )

            print(
                f"CH4     : {result['CH4']:.2f} ppm"
            )

            print(
                f"Ethanol : {result['ETHANOL']:.2f} ppm"
            )

            print(
                f"H2      : {result['H2']:.2f} ppm"
            )

            print(
                f"NH3     : {result['NH3']:.2f} ppm"
            )

            print(
                f"NO2     : {result['NO2']:.2f} ppm"
            )

        else:

            print("MiCS-4514 unavailable")

    except Exception as e:

        print("[MICS ERROR]", e)

    # -----------------------------------------------------
    # MQ136
    # -----------------------------------------------------

    print_section("MQ-136 - H2S SENSOR")

    try:

        result = read_mq136()

        if result:

            data.update(result)

            print(
                f"ADC RAW : {result['MQ136_RAW']}"
            )

            print(
                f"Voltage : {result['MQ136_VOLTAGE']:.4f} V"
            )

        else:

            print("ADS1115 unavailable")

    except Exception as e:

        print("[MQ136 ERROR]", e)

    # -----------------------------------------------------
    # SHT41
    # -----------------------------------------------------

    print_section("SHT41 - TEMPERATURE & HUMIDITY")

    try:

        result = read_sht41()

        if result:

            data.update(result)

            print(
                f"Temperature : "
                f"{result['SHT41_TEMPERATURE']:.2f} C"
            )

            print(
                f"Humidity    : "
                f"{result['SHT41_HUMIDITY']:.2f} %"
            )

        else:

            print("SHT41 unavailable")

    except Exception as e:

        print("[SHT41 ERROR]", e)

    # -----------------------------------------------------
    # BMP280
    # -----------------------------------------------------

    print_section("BMP280 - PRESSURE & ALTITUDE")

    try:

        result = read_bmp280()

        if result:

            data.update(result)

            print(
                f"Temperature : "
                f"{result['BMP280_TEMPERATURE']:.2f} C"
            )

            print(
                f"Pressure    : "
                f"{result['BMP280_PRESSURE']:.2f} hPa"
            )

            print(
                f"Altitude    : "
                f"{result['BMP280_ALTITUDE']:.2f} m"
            )

        else:

            print("BMP280 unavailable")

    except Exception as e:

        print("[BMP280 ERROR]", e)

    # -----------------------------------------------------
    # SAVE CSV
    # -----------------------------------------------------

    try:

        save_csv(data)

        print_section("DATA LOGGING")

        print("[CSV] Saved:", CSV_FILE)

    except Exception as e:

        print("[CSV ERROR]", e)

    print("\n" + "=" * 65)


# =========================================================
# MAIN PROGRAM
# =========================================================

def main():

    print("\n")
    print("=" * 65)

    print("       RASPBERRY PI SENSOR MONITORING SYSTEM")

    print("=" * 65)

    try:

        # Initialize I2C
        initialize_i2c()

        # Initialize independent sensors
        initialize_sps30()

        initialize_ads1115()

        initialize_sht41()

        initialize_bmp280()

        # Initialize and calibrate MiCS
        initialize_mics()

        # Create CSV
        create_csv()

        print("\n")
        print("=" * 65)

        print("ALL SENSOR INITIALIZATION COMPLETED")

        print(
            f"Sampling interval: {SAMPLE_INTERVAL} seconds"
        )

        print("Press CTRL+C to stop monitoring.")

        print("=" * 65)

        # Main monitoring loop
        while True:

            start_time = time.time()

            print_data()

            elapsed = time.time() - start_time

            time.sleep(
                max(0, SAMPLE_INTERVAL - elapsed)
            )

    except KeyboardInterrupt:

        print("\n[STOP] Monitoring stopped by user.")

    except Exception as e:

        print("\n[FATAL ERROR]", e)

    finally:

        print("\n[EXIT] Closing sensor connections...")

        if sps30 is not None:

            try:
                sps30.stop_measurement()
            except Exception:
                pass

        if sps30_connection is not None:

            try:
                sps30_connection.close()
            except Exception:
                pass

        if bus is not None:

            try:
                bus.close()
            except Exception:
                pass

        if i2c is not None:

            try:
                i2c.deinit()
            except Exception:
                pass

        print("[EXIT] Program terminated.")


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    main()
