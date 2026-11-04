
import time
import struct
from datetime import datetime

import board
import busio
import smbus2

import adafruit_sht4x
import adafruit_bmp280

import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

from sensirion_i2c_driver import (
    LinuxI2cTransceiver,
    I2cConnection,
    CrcCalculator
)
from sensirion_driver_adapters.i2c_adapter.i2c_channel import I2cChannel
from sensirion_i2c_sps30.device import Sps30Device
from sensirion_i2c_sps30.commands import OutputFormat


# ==========================================
# KONFIGURASI ADDRESS
# ==========================================

ADDR_SPS30 = 0x69
ADDR_MICS = 0x75
ADDR_ADS1115 = 0x48
ADDR_SHT41 = 0x44
ADDR_BMP280 = 0x76

I2C_PORT = "/dev/i2c-1"

INTERVAL = 2


# ==========================================
# INISIALISASI I2C
# ==========================================

i2c = board.I2C()

smbus = smbus2.SMBus(1)


# ==========================================
# SPS30
# ==========================================

sps30 = None
sps30_connection = None

try:
    sps30_connection = LinuxI2cTransceiver(I2C_PORT)

    channel = I2cChannel(
        I2cConnection(sps30_connection),
        slave_address=ADDR_SPS30,
        crc=CrcCalculator(8, 0x31, 0xFF, 0x00)
    )

    sps30 = Sps30Device(channel)

    try:
        sps30.stop_measurement()
    except Exception:
        pass

    sps30.start_measurement(
        OutputFormat.OUTPUT_FORMAT_FLOAT
    )

    print("[OK] SPS30 initialized")

except Exception as e:
    print("[ERROR] SPS30:", e)


# ==========================================
# MICS-4514 DFRobot SEN0377
# ==========================================

MICS_ADDRESS = 0x75

MICS_OX_REG = 0x04
MICS_POWER_MODE_REG = 0x0A

MICS_SLEEP = 0x00
MICS_WAKEUP = 0x01

MICS_CALIBRATION_TIME = 180

mics_r0_ox = 1.0
mics_r0_red = 1.0


# ==========================================
# I2C COMMUNICATION
# ==========================================

def read_mics_raw():

    data = smbus.read_i2c_block_data(
        MICS_ADDRESS,
        MICS_OX_REG,
        6
    )

    ox_adc = (data[0] << 8) | data[1]
    red_adc = (data[2] << 8) | data[3]
    power = (data[4] << 8) | data[5]

    ox = max(0, power - ox_adc)
    red = max(0, power - red_adc)

    return ox, red, power


# ==========================================
# WAKE UP SENSOR
# ==========================================

def wake_mics():

    mode = smbus.read_byte_data(
        MICS_ADDRESS,
        MICS_POWER_MODE_REG
    )

    if mode == MICS_SLEEP:

        smbus.write_byte_data(
            MICS_ADDRESS,
            MICS_POWER_MODE_REG,
            MICS_WAKEUP
        )

        time.sleep(0.1)
        print("[MICS] Wake-up successful")

    elif mode == MICS_WAKEUP:

        print("[MICS] Sensor already awake")

    else:

        raise RuntimeError(
            f"Unexpected MICS power mode: {mode}"
        )


# ==========================================
# CALIBRATION R0
# ==========================================

def calibrate_mics():

    global mics_r0_ox, mics_r0_red

    print("\n[MICS] Place sensor in clean air.")
    print(
        f"[MICS] Warming up for "
        f"{MICS_CALIBRATION_TIME} seconds..."
    )

    for remaining in range(
        MICS_CALIBRATION_TIME, 0, -1
    ):

        print(
            f"\r[MICS] Remaining: {remaining:3d} seconds",
            end="",
            flush=True
        )

        time.sleep(1)

    print("\n[MICS] Reading calibration baseline...")

    ox_values = []
    red_values = []

    for i in range(10):

        ox, red, power = read_mics_raw()

        if ox <= 0 or red <= 0:
            raise RuntimeError(
                "Invalid MICS baseline. "
                "Check sensor readings."
            )

        ox_values.append(ox)
        red_values.append(red)

        print(
            f"[MICS] Calibration {i+1}/10 | "
            f"OX={ox} RED={red} POWER={power}"
        )

        time.sleep(1)

    mics_r0_ox = sum(ox_values) / len(ox_values)
    mics_r0_red = sum(red_values) / len(red_values)

    print("\n[MICS] Calibration completed!")
    print(f"[MICS] R0 OX  = {mics_r0_ox:.2f}")
    print(f"[MICS] R0 RED = {mics_r0_red:.2f}")


# ==========================================
# GAS CONVERSION
# DFRobot_MICS.cpp
# ==========================================

def get_mics_gas(gas, ratio_red, ratio_ox):

    if gas == "CO":

        if ratio_red > 0.425:
            return 0.0

        value = (0.425 - ratio_red) / 0.000405

        return min(1000.0, value) if value >= 1 else 0.0

    elif gas == "CH4":

        if ratio_red > 0.786:
            return 0.0

        value = (0.786 - ratio_red) / 0.000023

        return min(25000.0, value) if value >= 1000 else 0.0

    elif gas == "ETHANOL":

        if ratio_red > 0.306:
            return 0.0

        value = (0.306 - ratio_red) / 0.00057

        return min(500.0, value) if value >= 10 else 0.0

    elif gas == "H2":

        if ratio_red > 0.279:
            return 0.0

        value = (0.279 - ratio_red) / 0.00026

        return min(1000.0, value) if value >= 1 else 0.0

    elif gas == "NH3":

        if ratio_red > 0.8:
            return 0.0

        value = (0.8 - ratio_red) / 0.0015

        return min(500.0, value) if value >= 10 else 0.0

    elif gas == "NO2":

        if ratio_ox < 1.1:
            return 0.0

        value = (ratio_ox - 0.045) / 6.13

        return min(10.0, value) if value >= 0.1 else 0.0

    return 0.0


# ==========================================
# READ ALL MICS GASES
# ==========================================

def get_mics_data():

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
        "OX_ADC": ox,
        "RED_ADC": red,
        "POWER": power,
        "RATIO_OX": ratio_ox,
        "RATIO_RED": ratio_red,
        "CO": gases["CO"],
        "CH4": gases["CH4"],
        "ETHANOL": gases["ETHANOL"],
        "H2": gases["H2"],
        "NH3": gases["NH3"],
        "NO2": gases["NO2"]
    }


# ==========================================
# INITIALIZE MICS
# ==========================================

def initialize_mics():

    try:

        # Verify I2C communication
        mode = smbus.read_byte_data(
            MICS_ADDRESS,
            MICS_POWER_MODE_REG
        )

        print(
            f"[MICS] Detected at 0x{MICS_ADDRESS:02X}"
        )

        print(f"[MICS] Power mode: 0x{mode:02X}")

        wake_mics()

        calibrate_mics()

        return True

    except Exception as e:

        print(f"[MICS] Initialization failed: {e}")

        return False



# ==========================================
# ADS1115 + MQ136
# ==========================================

ads = None
mq136 = None

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

except Exception as e:
    print("[ERROR] ADS1115:", e)


def read_mq136():

    if mq136 is None:
        return None

    return {
        "RAW": mq136.value,
        "VOLTAGE": mq136.voltage
    }


# ==========================================
# SHT41
# ==========================================

sht41 = None

try:

    sht41 = adafruit_sht4x.SHT4x(i2c)

    print("[OK] SHT41 initialized")

except Exception as e:
    print("[ERROR] SHT41:", e)


def read_sht41():

    if sht41 is None:
        return None

    temperature, humidity = sht41.measurements

    return {
        "TEMPERATURE": temperature,
        "HUMIDITY": humidity
    }


# ==========================================
# BMP280
# ==========================================

bmp280 = None

try:

    bmp280 = adafruit_bmp280.Adafruit_BMP280_I2C(
        i2c,
        address=ADDR_BMP280
    )

    bmp280.sea_level_pressure = 1013.25

    print("[OK] BMP280 initialized")

except Exception as e:
    print("[ERROR] BMP280:", e)


def read_bmp280():

    if bmp280 is None:
        return None

    return {
        "TEMPERATURE": bmp280.temperature,
        "PRESSURE": bmp280.pressure,
        "ALTITUDE": bmp280.altitude
    }


# ==========================================
# PRINT DATA
# ==========================================

def print_data():

    print("\n" + "=" * 65)
    print("       AIR QUALITY & ENVIRONMENT MONITORING")
    print("=" * 65)

    print("TIME:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # SPS30

    print("\n[SPS30 - PARTICULATE MATTER]")

    if sps30:

        try:
            values = sps30.read_measurement_values_float()

            (
                pm1, pm25, pm4, pm10,
                nc05, nc1, nc25, nc4, nc10,
                particle_size
            ) = values

            print(f"PM1.0  : {pm1:.2f} ug/m3")
            print(f"PM2.5  : {pm25:.2f} ug/m3")
            print(f"PM4.0  : {pm4:.2f} ug/m3")
            print(f"PM10   : {pm10:.2f} ug/m3")
            print(f"Particle Size: {particle_size:.2f} um")

        except Exception as e:
            print("SPS30 Read Error:", e)

    # MiCS4514

    print("\n[MiCS-4514 - GAS SENSOR]")

    try:

        data = get_mics_data()

        print(f"OX ADC    : {data['OX_ADC']}")
        print(f"RED ADC   : {data['RED_ADC']}")
        print(f"POWER     : {data['POWER']}")

        print(f"Ratio OX  : {data['RATIO_OX']:.4f}")
        print(f"Ratio RED : {data['RATIO_RED']:.4f}")

        print(f"CO        : {data['CO']:.2f} ppm*")
        print(f"CH4       : {data['CH4']:.2f} ppm*")
        print(f"Ethanol   : {data['ETHANOL']:.2f} ppm*")
        print(f"H2        : {data['H2']:.2f} ppm*")
        print(f"NH3       : {data['NH3']:.2f} ppm*")
        print(f"NO2       : {data['NO2']:.2f} ppm*")

    except Exception as e:
        print("MiCS Error:", e)

    # MQ136

    print("\n[MQ-136 - H2S SENSOR]")

    try:

        data = read_mq136()

        if data:
            print(f"ADC RAW : {data['RAW']}")
            print(f"Voltage : {data['VOLTAGE']:.4f} V")

    except Exception as e:
        print("MQ136 Error:", e)

    # SHT41

    print("\n[SHT41 - TEMPERATURE & HUMIDITY]")

    try:

        data = read_sht41()

        if data:
            print(f"Temperature : {data['TEMPERATURE']:.2f} C")
            print(f"Humidity    : {data['HUMIDITY']:.2f} %")

    except Exception as e:
        print("SHT41 Error:", e)

    # BMP280

    print("\n[BMP280 - PRESSURE & ALTITUDE]")

    try:

        data = read_bmp280()

        if data:
            print(f"Temperature : {data['TEMPERATURE']:.2f} C")
            print(f"Pressure    : {data['PRESSURE']:.2f} hPa")
            print(f"Altitude    : {data['ALTITUDE']:.2f} m")

    except Exception as e:
        print("BMP280 Error:", e)

    print("\n" + "=" * 65)


# ==========================================
# MAIN PROGRAM
# ==========================================

if __name__ == "__main__":

    try:

        # Warm-up sensor MiCS
        time.sleep(2)

        try:
            calibrate_mics(samples=10)
        except Exception as e:
            print("[MICS] Calibration Error:", e)

        print("\nAll sensors monitoring started!")

        while True:

            print_data()

            time.sleep(INTERVAL)

    except KeyboardInterrupt:

        print("\nStopping monitoring...")

    finally:

        if sps30:
            try:
                sps30.stop_measurement()
            except Exception:
                pass

        if sps30_connection:
            try:
                sps30_connection.close()
            except Exception:
                pass

        smbus.close()

        print("Program stopped.")
