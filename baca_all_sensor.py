
import time
import csv
import os
from datetime import datetime

import smbus2
import board

# ADS1115
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15 import ads1x15
from adafruit_ads1x15.analog_in import AnalogIn

# SHT41 and BMP280
import adafruit_sht4x
import adafruit_bmp280

# SPS30
from sensirion_i2c_driver import (
    LinuxI2cTransceiver,
    I2cConnection,
    CrcCalculator
)

from sensirion_driver_adapters.i2c_adapter.i2c_channel import I2cChannel
from sensirion_i2c_sps30.device import Sps30Device
from sensirion_i2c_sps30.commands import OutputFormat


# ============================================================
# CONFIGURATION
# ============================================================

I2C_BUS_NUMBER = 1
I2C_DEVICE = "/dev/i2c-1"

SPS30_ADDRESS = 0x69
MICS_ADDRESS = 0x75
ADS_ADDRESS = 0x48
SHT41_ADDRESS = 0x44
BMP280_ADDRESS = 0x76

LOG_FILE = "environment_monitoring.csv"

INTERVAL = 2

# MiCS-4514
MICS_WARMUP_SECONDS = 180
MICS_CALIBRATION_SAMPLES = 10

# ADS1115
ADS_GAIN = 1


# ============================================================
# GLOBAL OBJECTS
# ============================================================

smbus = None
i2c = None

sps30 = None
sps30_transceiver = None

ads = None
mq136 = None

sht41 = None
bmp280 = None

mics_ready = False
mics_r0_ox = None
mics_r0_red = None


# ============================================================
# CSV HEADER
# ============================================================

CSV_HEADER = [
    "timestamp",

    # SPS30
    "pm1_0",
    "pm2_5",
    "pm4_0",
    "pm10_0",
    "nc0_5",
    "nc1_0",
    "nc2_5",
    "nc4_0",
    "nc10_0",
    "typical_particle_size",

    # MiCS-4514
    "mics_ox_adc",
    "mics_red_adc",
    "mics_power",
    "mics_ratio_ox",
    "mics_ratio_red",
    "co_ppm",
    "ch4_ppm",
    "ethanol_ppm",
    "h2_ppm",
    "nh3_ppm",
    "no2_ppm",

    # MQ-136
    "mq136_raw",
    "mq136_voltage",

    # SHT41
    "sht41_temperature",
    "sht41_humidity",

    # BMP280
    "bmp280_temperature",
    "bmp280_pressure",
    "bmp280_altitude"
]


# ============================================================
# INITIALIZE I2C
# ============================================================

def initialize_i2c():

    global smbus, i2c

    print("\n[INIT] I2C BUS")

    try:
        smbus = smbus2.SMBus(I2C_BUS_NUMBER)
        print("[OK] SMBus initialized")

    except Exception as e:
        print(f"[ERROR] SMBus: {e}")
        return False

    try:
        i2c = board.I2C()
        print("[OK] Blinka I2C initialized")

    except Exception as e:
        print(f"[ERROR] Blinka I2C: {e}")
        return False

    return True


# ============================================================
# SPS30 INITIALIZATION
# ============================================================

def initialize_sps30():

    global sps30, sps30_transceiver

    print("\n[INIT] SPS30...")

    try:

        sps30_transceiver = LinuxI2cTransceiver(
            I2C_DEVICE
        )

        channel = I2cChannel(
            I2cConnection(sps30_transceiver),
            slave_address=SPS30_ADDRESS,
            crc=CrcCalculator(
                8,
                0x31,
                0xFF,
                0x00
            )
        )

        sps30 = Sps30Device(channel)

        # Stop any previous measurement
        try:
            sps30.stop_measurement()
            time.sleep(0.1)
        except Exception:
            pass

        # Read sensor information
        try:
            serial_number = sps30.read_serial_number()
            product_type = sps30.read_product_type()

            print(f"[SPS30] Serial number: {serial_number}")
            print(f"[SPS30] Product type: {product_type}")

        except Exception as e:
            print(f"[SPS30] Info warning: {e}")

        # Start measurement
        sps30.start_measurement(
            OutputFormat.OUTPUT_FORMAT_FLOAT
        )

        time.sleep(0.1)

        print("[OK] SPS30 initialized")

    except Exception as e:

        print(f"[ERROR] SPS30: {e}")

        sps30 = None

        if sps30_transceiver is not None:
            try:
                sps30_transceiver.close()
            except Exception:
                pass

        sps30_transceiver = None


# ============================================================
# SPS30 READING
# ============================================================

def read_sps30():

    if sps30 is None:
        return {}

    try:

        data_ready = sps30.read_data_ready_flag()

        if not data_ready:
            print("[SPS30] Data not ready")
            return {}

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
            "pm1_0": pm1,
            "pm2_5": pm25,
            "pm4_0": pm4,
            "pm10_0": pm10,

            "nc0_5": nc05,
            "nc1_0": nc1,
            "nc2_5": nc25,
            "nc4_0": nc4,
            "nc10_0": nc10,

            "typical_particle_size": particle_size
        }

    except Exception as e:

        print(f"[ERROR] SPS30 reading: {e}")
        return {}


# ============================================================
# MiCS-4514 INITIALIZATION AND CALIBRATION
# ============================================================

def initialize_mics():

    global mics_ready
    global mics_r0_ox
    global mics_r0_red

    print("\n[INIT] MiCS-4514...")

    if smbus is None:
        print("[ERROR] SMBus unavailable")
        return

    try:

        # Read power mode
        mode = smbus.read_byte_data(
            MICS_ADDRESS,
            0x0A
        )

        print(f"[MiCS] Power mode: 0x{mode:02X}")

        # Wake sensor if sleeping
        if mode == 0x00:

            smbus.write_byte_data(
                MICS_ADDRESS,
                0x0A,
                0x01
            )

            print("[MiCS] Sensor awakened")

        # Warmup
        print(
            f"[MiCS] Warmup: "
            f"{MICS_WARMUP_SECONDS} seconds"
        )

        for remaining in range(
            MICS_WARMUP_SECONDS,
            0,
            -1
        ):

            print(
                f"\r[MiCS] Remaining: {remaining}s ",
                end="",
                flush=True
            )

            time.sleep(1)

        print("\n[MiCS] Warmup completed")

        # Calibration
        ox_samples = []
        red_samples = []

        print("[MiCS] Starting R0 calibration...")

        for i in range(MICS_CALIBRATION_SAMPLES):

            data = smbus.read_i2c_block_data(
                MICS_ADDRESS,
                0x04,
                6
            )

            ox_adc = (data[0] << 8) | data[1]
            red_adc = (data[2] << 8) | data[3]
            power = (data[4] << 8) | data[5]

            ox = max(0, power - ox_adc)
            red = max(0, power - red_adc)

            ox_samples.append(ox)
            red_samples.append(red)

            print(
                f"[MiCS CAL] "
                f"{i + 1}/{MICS_CALIBRATION_SAMPLES} "
                f"OX={ox} "
                f"RED={red} "
                f"POWER={power}"
            )

            time.sleep(1)

        mics_r0_ox = sum(ox_samples) / len(ox_samples)
        mics_r0_red = sum(red_samples) / len(red_samples)

        if mics_r0_ox <= 0 or mics_r0_red <= 0:
            raise ValueError("Invalid MiCS R0 calibration")

        mics_ready = True

        print("[OK] MiCS-4514 initialized")
        print(f"[MiCS] R0 OX  = {mics_r0_ox:.2f}")
        print(f"[MiCS] R0 RED = {mics_r0_red:.2f}")

    except Exception as e:

        print(f"\n[ERROR] MiCS-4514: {e}")
        mics_ready = False


# ============================================================
# MiCS-4514 GAS CONVERSION
# DFRobot conversion equations
# ============================================================

def calculate_gas(ratio_ox, ratio_red):

    # CO
    co = 0

    if ratio_red <= 0.425:
        co = (0.425 - ratio_red) / 0.000405
        co = min(co, 1000)

        if co < 1:
            co = 0

    # CH4
    ch4 = 0

    if ratio_red <= 0.786:
        ch4 = (0.786 - ratio_red) / 0.000023
        ch4 = min(ch4, 25000)

        if ch4 < 1000:
            ch4 = 0

    # Ethanol
    ethanol = 0

    if ratio_red <= 0.306:
        ethanol = (0.306 - ratio_red) / 0.00057
        ethanol = min(ethanol, 500)

        if ethanol < 10:
            ethanol = 0

    # H2
    h2 = 0

    if ratio_red <= 0.279:
        h2 = (0.279 - ratio_red) / 0.00026
        h2 = min(h2, 1000)

        if h2 < 1:
            h2 = 0

    # NH3
    nh3 = 0

    if ratio_red <= 0.8:
        nh3 = (0.8 - ratio_red) / 0.0015
        nh3 = min(nh3, 500)

        if nh3 < 10:
            nh3 = 0

    # NO2
    no2 = 0

    if ratio_ox >= 1.1:
        no2 = (ratio_ox - 0.045) / 6.13
        no2 = min(no2, 10)

        if no2 < 0.1:
            no2 = 0

    return {
        "co_ppm": co,
        "ch4_ppm": ch4,
        "ethanol_ppm": ethanol,
        "h2_ppm": h2,
        "nh3_ppm": nh3,
        "no2_ppm": no2
    }


# ============================================================
# MiCS-4514 READING
# ============================================================

def read_mics():

    if not mics_ready:
        return {}

    try:

        data = smbus.read_i2c_block_data(
            MICS_ADDRESS,
            0x04,
            6
        )

        ox_adc = (data[0] << 8) | data[1]
        red_adc = (data[2] << 8) | data[3]
        power = (data[4] << 8) | data[5]

        ox = max(0, power - ox_adc)
        red = max(0, power - red_adc)

        ratio_ox = ox / mics_r0_ox
        ratio_red = red / mics_r0_red

        gases = calculate_gas(
            ratio_ox,
            ratio_red
        )

        return {
            "mics_ox_adc": ox_adc,
            "mics_red_adc": red_adc,
            "mics_power": power,

            "mics_ratio_ox": ratio_ox,
            "mics_ratio_red": ratio_red,

            **gases
        }

    except Exception as e:

        print(f"[ERROR] MiCS reading: {e}")
        return {}


# ============================================================
# ADS1115 + MQ-136 INITIALIZATION
# ============================================================

def initialize_ads():

    global ads, mq136

    print("\n[INIT] ADS1115 + MQ-136...")

    try:

        ads = ADS.ADS1115(
            i2c,
            address=ADS_ADDRESS
        )

        ads.gain = ADS_GAIN

        # ADS1115 channel A0
        mq136 = AnalogIn(
            ads,
            ads1x15.Pin.A0
        )

        print("[OK] ADS1115 initialized")
        print("[OK] MQ-136 connected to A0")

    except Exception as e:

        print(f"[ERROR] ADS1115: {e}")

        ads = None
        mq136 = None


# ============================================================
# MQ-136 READING
# ============================================================

def read_mq136():

    if mq136 is None:
        return {}

    try:

        raw = mq136.value
        voltage = mq136.voltage

        return {
            "mq136_raw": raw,
            "mq136_voltage": voltage
        }

    except Exception as e:

        print(f"[ERROR] MQ-136 reading: {e}")
        return {}


# ============================================================
# SHT41 INITIALIZATION
# ============================================================

def initialize_sht41():

    global sht41

    print("\n[INIT] SHT41...")

    try:

        sht41 = adafruit_sht4x.SHT4x(
            i2c,
            address=SHT41_ADDRESS
        )

        print("[OK] SHT41 initialized")

    except Exception as e:

        print(f"[ERROR] SHT41: {e}")
        sht41 = None


# ============================================================
# SHT41 READING
# ============================================================

def read_sht41():

    if sht41 is None:
        return {}

    try:

        temperature, humidity = sht41.measurements

        return {
            "sht41_temperature": temperature,
            "sht41_humidity": humidity
        }

    except Exception as e:

        print(f"[ERROR] SHT41 reading: {e}")
        return {}


# ============================================================
# BMP280 INITIALIZATION
# ============================================================

def initialize_bmp280():

    global bmp280

    print("\n[INIT] BMP280...")

    try:

        bmp280 = adafruit_bmp280.Adafruit_BMP280_I2C(
            i2c,
            address=BMP280_ADDRESS
        )

        bmp280.sea_level_pressure = 1013.25

        print("[OK] BMP280 initialized")

    except Exception as e:

        print(f"[ERROR] BMP280: {e}")
        bmp280 = None


# ============================================================
# BMP280 READING
# ============================================================

def read_bmp280():

    if bmp280 is None:
        return {}

    try:

        temperature = bmp280.temperature
        pressure = bmp280.pressure
        altitude = bmp280.altitude

        return {
            "bmp280_temperature": temperature,
            "bmp280_pressure": pressure,
            "bmp280_altitude": altitude
        }

    except Exception as e:

        print(f"[ERROR] BMP280 reading: {e}")
        return {}


# ============================================================
# CSV INITIALIZATION
# ============================================================

def initialize_csv():

    exists = (
        os.path.isfile(LOG_FILE)
        and os.path.getsize(LOG_FILE) > 0
    )

    if not exists:

        with open(
            LOG_FILE,
            "w",
            newline=""
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=CSV_HEADER
            )

            writer.writeheader()

        print(f"[CSV] Created: {LOG_FILE}")

    else:

        print(f"[CSV] Append mode: {LOG_FILE}")


# ============================================================
# CSV SAVE
# ============================================================

def save_csv(data):

    try:

        with open(
            LOG_FILE,
            "a",
            newline=""
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=CSV_HEADER
            )

            writer.writerow(data)

    except Exception as e:

        print(f"[ERROR] CSV: {e}")


# ============================================================
# DISPLAY DATA
# ============================================================

def print_data():

    data = {
        key: ""
        for key in CSV_HEADER
    }

    data["timestamp"] = datetime.now().isoformat(
        timespec="seconds"
    )

    # Read all sensors
    data.update(read_sps30())
    data.update(read_mics())
    data.update(read_mq136())
    data.update(read_sht41())
    data.update(read_bmp280())

    # Display
    print("\n")
    print("=" * 65)
    print("          ENVIRONMENT MONITORING SYSTEM")
    print("=" * 65)

    print(f"Timestamp: {data['timestamp']}")

    print("\n[SPS30] PARTICULATE MATTER")

    print(f"PM1.0  : {data['pm1_0']}")
    print(f"PM2.5  : {data['pm2_5']}")
    print(f"PM4.0  : {data['pm4_0']}")
    print(f"PM10.0 : {data['pm10_0']}")

    print(f"NC0.5  : {data['nc0_5']}")
    print(f"NC1.0  : {data['nc1_0']}")
    print(f"NC2.5  : {data['nc2_5']}")
    print(f"NC4.0  : {data['nc4_0']}")
    print(f"NC10.0 : {data['nc10_0']}")

    print(
        f"Particle size: "
        f"{data['typical_particle_size']}"
    )

    print("\n[MiCS-4514] GAS SENSOR")

    print(f"OX ADC  : {data['mics_ox_adc']}")
    print(f"RED ADC : {data['mics_red_adc']}")
    print(f"POWER   : {data['mics_power']}")

    print(f"Ratio OX  : {data['mics_ratio_ox']}")
    print(f"Ratio RED : {data['mics_ratio_red']}")

    print(f"CO      : {data['co_ppm']} ppm")
    print(f"CH4     : {data['ch4_ppm']} ppm")
    print(f"Ethanol : {data['ethanol_ppm']} ppm")
    print(f"H2      : {data['h2_ppm']} ppm")
    print(f"NH3     : {data['nh3_ppm']} ppm")
    print(f"NO2     : {data['no2_ppm']} ppm")

    print("\n[MQ-136] GAS SENSOR")

    print(f"RAW     : {data['mq136_raw']}")
    print(f"Voltage : {data['mq136_voltage']} V")

    print("\n[SHT41] TEMPERATURE & HUMIDITY")

    print(f"Temperature : {data['sht41_temperature']} °C")
    print(f"Humidity    : {data['sht41_humidity']} %")

    print("\n[BMP280] ATMOSPHERIC")

    print(f"Temperature : {data['bmp280_temperature']} °C")
    print(f"Pressure    : {data['bmp280_pressure']} hPa")
    print(f"Altitude    : {data['bmp280_altitude']} m")

    print("=" * 65)

    save_csv(data)


# ============================================================
# CLEANUP
# ============================================================

def cleanup():

    global smbus, sps30_transceiver

    print("\n[SYSTEM] Cleaning up...")

    if sps30 is not None:

        try:
            sps30.stop_measurement()
            print("[SPS30] Measurement stopped")

        except Exception as e:
            print(f"[SPS30] Stop error: {e}")

    if sps30_transceiver is not None:

        try:
            sps30_transceiver.close()
            print("[SPS30] I2C transceiver closed")

        except Exception:
            pass

    if smbus is not None:

        try:
            smbus.close()
            print("[I2C] SMBus closed")

        except Exception:
            pass

    print("[SYSTEM] Cleanup completed")


# ============================================================
# MAIN
# ============================================================

def main():

    print("\n")
    print("=" * 65)
    print("       RASPBERRY PI ENVIRONMENT MONITORING")
    print("=" * 65)

    # Initialize I2C
    if not initialize_i2c():
        return

    # Initialize sensors
    initialize_sps30()
    initialize_ads()
    initialize_sht41()
    initialize_bmp280()

    # MiCS requires warmup and calibration
    initialize_mics()

    # CSV
    initialize_csv()

    print("\n[SYSTEM] Initialization completed")
    print(f"[SYSTEM] Interval: {INTERVAL} seconds")
    print(f"[SYSTEM] Output: {LOG_FILE}")
    print("[SYSTEM] Press CTRL+C to stop")

    try:

        while True:

            start_time = time.time()

            print_data()

            elapsed = time.time() - start_time

            sleep_time = max(
                0,
                INTERVAL - elapsed
            )

            time.sleep(sleep_time)

    except KeyboardInterrupt:

        print("\n[SYSTEM] Monitoring stopped by user")

    except Exception as e:

        print(f"\n[SYSTEM] Unexpected error: {e}")

    finally:

        cleanup()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
