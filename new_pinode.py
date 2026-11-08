#!/usr/bin/env python3
"""Raspberry Pi Environment Monitoring + upload ke Enviro Monitor (I2C Bus 3).

Gabungan program pembacaan sensor (SPS30, MiCS-4514, MQ-136, SHT41, BMP280)
dengan pengiriman data ke endpoint /enviro/api/ingest menggunakan /dev/i2c-3.
"""

import csv
import json
import math
import os
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import smbus2
from smbus2 import i2c_msg
from adafruit_extended_bus import ExtendedI2C as I2C

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
# CONFIGURATION (MENGGUNAKAN I2C BUS 3)
# ============================================================

I2C_BUS_NUMBER = 3
I2C_DEVICE = f"/dev/i2c-{I2C_BUS_NUMBER}"

SPS30_ADDRESS = 0x69
MICS_ADDRESS = 0x75
ADS_ADDRESS = 0x48
SHT41_ADDRESS = 0x44
BMP280_ADDRESS = 0x76

LOG_FILE = "environment_monitoring.csv"

INTERVAL = 2

# MiCS-4514 (Ubah ke 180 untuk pemakaian produksi, 15 untuk tes cepat)
MICS_WARMUP_SECONDS = 180
MICS_CALIBRATION_SAMPLES = 10

# ADS1115
ADS_GAIN = 1

# Enviro Monitor upload
ENVIRO_URL = os.getenv(
    "ENVIRO_URL",
    "https://devel-ai.ub.ac.id/enviro/api/ingest"
)
ENVIRO_API_KEY = os.getenv("ENVIRO_API_KEY", "")
ENVIRO_DEVICE_ID = os.getenv("ENVIRO_DEVICE_ID", "node-01")
ENVIRO_QUEUE = os.getenv("ENVIRO_QUEUE", "enviro_queue.jsonl")
ENVIRO_TIMEOUT = float(os.getenv("ENVIRO_TIMEOUT", "5"))
ENVIRO_DEVICE_TZ = os.getenv("ENVIRO_DEVICE_TZ", "Asia/Jakarta")


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

uploader = None


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
# ENVIRO MONITOR UPLOAD
# ============================================================

FIELD_MAP = {
    # SPS30
    "pm1_0": ("sps30", "pm1"),
    "pm2_5": ("sps30", "pm25"),
    "pm4_0": ("sps30", "pm4"),
    "pm10_0": ("sps30", "pm10"),
    # MiCS-4514
    "co_ppm": ("gases", "co"),
    "ch4_ppm": ("gases", "ch4"),
    "ethanol_ppm": ("gases", "c2h5oh"),
    "h2_ppm": ("gases", "h2"),
    "nh3_ppm": ("gases", "nh3"),
    "no2_ppm": ("gases", "no2"),
    # MQ-136
    "mq136_raw": ("extra", "h2s_raw"),
    "mq136_voltage": ("extra", "h2s_voltage"),
    # SHT41
    "sht41_temperature": ("extra", "temp_sht"),
    "sht41_humidity": ("extra", "humidity"),
    # BMP280
    "bmp280_temperature": ("extra", "temp_bmp"),
    "bmp280_pressure": ("extra", "pressure"),
    "bmp280_altitude": ("extra", "altitude"),
}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 2)


def _device_zone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(ENVIRO_DEVICE_TZ)
        except Exception:
            pass
    return timezone(timedelta(hours=7))


def _timestamp(value):
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now()
    else:
        moment = datetime.now()

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_device_zone())
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def build_payload(row, device_id=ENVIRO_DEVICE_ID):
    gases = {}
    sps30_data = {}
    extra = {}

    for source, (group, target) in FIELD_MAP.items():
        value = _number(row.get(source))
        if value is None:
            continue
        if group == "gases":
            gases[target] = value
        elif group == "sps30":
            sps30_data[target] = value
        else:
            extra[target] = value

    if not gases and not sps30_data and not extra:
        return None

    payload = {
        "device_id": device_id,
        "timestamp": _timestamp(row.get("timestamp")),
        "gases": gases,
        "sps30": sps30_data,
    }
    payload.update(extra)
    return payload


class UploadError(RuntimeError):
    def __init__(self, message, drop=False, fatal=False):
        super().__init__(message)
        self.drop = drop
        self.fatal = fatal


class Uploader:
    def __init__(self, url, api_key, device_id, queue_path,
                 timeout=10.0, verify=True, max_queue=5000):
        self.url = url
        self.api_key = api_key
        self.device_id = device_id
        self.queue_path = Path(queue_path)
        self.timeout = timeout
        self.context = None if verify else ssl._create_unverified_context()
        self.max_queue = max_queue
        self.pending = []
        self.backoff = 0.0
        self.retry_at = 0.0
        self._load_queue()

    @property
    def enabled(self):
        return bool(self.api_key)

    def _load_queue(self):
        if not self.queue_path.exists():
            return
        try:
            with self.queue_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self.pending.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[UPLOAD] Gagal membaca antrean: {exc}")
        if self.pending:
            print(f"[UPLOAD] Memuat {len(self.pending)} data tertunda")

    def _save_queue(self):
        try:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.queue_path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                for item in self.pending:
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
            os.replace(temp, self.queue_path)
        except OSError as exc:
            print(f"[UPLOAD] Gagal menyimpan antrean: {exc}")

    def upload(self, row, force=False):
        if not self.enabled:
            return False

        payload = build_payload(row, self.device_id)
        if payload is None:
            print("[UPLOAD] Tidak ada field sensor yang bisa dikirim")
            return False

        self.pending.append(payload)
        if len(self.pending) > self.max_queue:
            dropped = len(self.pending) - self.max_queue
            del self.pending[:dropped]
            print(f"[UPLOAD] Antrean penuh, {dropped} data terlama dibuang")
        self._save_queue()

        return self.flush(force=force)

    def flush(self, force=False):
        if not self.enabled or not self.pending:
            return True
        if not force and time.monotonic() < self.retry_at:
            return False

        try:
            while self.pending:
                batch = self.pending[:200]
                try:
                    self._send(batch)
                except UploadError as exc:
                    if exc.drop:
                        print(f"[UPLOAD] Batch ditolak, dibuang: {exc}")
                        del self.pending[:len(batch)]
                        self._save_queue()
                        continue
                    raise
                del self.pending[:len(batch)]
                self._save_queue()
                print(
                    f"[UPLOAD] Terkirim {len(batch)} data "
                    f"(sisa antrean {len(self.pending)})"
                )

            self.backoff = 0.0
            self.retry_at = 0.0
            return True

        except UploadError as exc:
            if exc.fatal:
                print(f"[UPLOAD] API key ditolak: {exc}")
                print("[UPLOAD] Periksa ENVIRO_API_KEY. Upload dijeda 60 detik.")
                self.retry_at = time.monotonic() + 60
                return False

            self.backoff = min(60.0, max(5.0, self.backoff * 2 or 5.0))
            self.retry_at = time.monotonic() + self.backoff
            print(
                f"[UPLOAD] Gagal: {exc} — antrean {len(self.pending)}, "
                f"coba lagi {self.backoff:.0f} detik"
            )
            return False

    def _send(self, payloads):
        body = json.dumps(
            {"readings": payloads},
            separators=(",", ":")
        ).encode("utf-8")

        req = request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "User-Agent": "enviro-raspi/1.0",
            },
        )

        try:
            with request.urlopen(
                req,
                timeout=self.timeout,
                context=self.context
            ) as resp:
                if not 200 <= resp.status < 300:
                    raise UploadError(f"HTTP {resp.status}")

        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            message = f"HTTP {exc.code}: {detail}"
            if exc.code in (401, 403):
                raise UploadError(message, fatal=True) from exc
            if 400 <= exc.code < 500:
                raise UploadError(message, drop=True) from exc
            raise UploadError(message) from exc

        except error.URLError as exc:
            raise UploadError(f"tidak bisa terhubung: {exc.reason}") from exc

        except TimeoutError as exc:
            raise UploadError("permintaan timeout") from exc


# ============================================================
# INITIALIZE I2C (MENGGUNAKAN EXTENDED I2C BUS 3)
# ============================================================

def initialize_i2c():
    global smbus, i2c

    print(f"\n[INIT] I2C BUS {I2C_BUS_NUMBER} ({I2C_DEVICE})")

    try:
        smbus = smbus2.SMBus(I2C_BUS_NUMBER)
        print(f"[OK] SMBus initialized on bus {I2C_BUS_NUMBER}")
    except Exception as e:
        print(f"[ERROR] SMBus: {e}")
        return False

    try:
        # Menggunakan ExtendedI2C agar Blinka membaca /dev/i2c-3, bukan /dev/i2c-1
        i2c = I2C(I2C_BUS_NUMBER)
        print(f"[OK] Blinka ExtendedI2C initialized on bus {I2C_BUS_NUMBER}")
    except Exception as e:
        print(f"[ERROR] Blinka ExtendedI2C: {e}")
        return False

    return True


# ============================================================
# SPS30 INITIALIZATION & READING
# ============================================================

def initialize_sps30():
    global sps30, sps30_transceiver

    print("\n[INIT] SPS30...")

    try:
        sps30_transceiver = LinuxI2cTransceiver(I2C_DEVICE)
        channel = I2cChannel(
            I2cConnection(sps30_transceiver),
            slave_address=SPS30_ADDRESS,
            crc=CrcCalculator(8, 0x31, 0xFF, 0x00)
        )
        sps30 = Sps30Device(channel)

        try:
            sps30.stop_measurement()
            time.sleep(0.1)
        except Exception:
            pass

        try:
            serial_number = sps30.read_serial_number()
            product_type = sps30.read_product_type()
            print(f"[SPS30] Serial number: {serial_number}")
            print(f"[SPS30] Product type: {product_type}")
        except Exception as e:
            print(f"[SPS30] Info warning: {e}")

        sps30.start_measurement(OutputFormat.OUTPUT_FORMAT_FLOAT)
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


def read_sps30():
    if sps30 is None:
        return {}

    try:
        data_ready = sps30.read_data_ready_flag()
        if not data_ready:
            print("[SPS30] Data not ready")
            return {}

        values = sps30.read_measurement_values_float()
        (pm1, pm25, pm4, pm10, nc05, nc1, nc25, nc4, nc10, particle_size) = values

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
# MiCS-4514 INITIALIZATION, CALIBRATION & READING (RAW I2C)
# ============================================================

def _mics_read_bytes(reg, length):
    write_msg = i2c_msg.write(MICS_ADDRESS, [reg])
    read_msg = i2c_msg.read(MICS_ADDRESS, length)
    smbus.i2c_rdwr(write_msg, read_msg)
    return list(read_msg)


def _mics_write_byte(reg, val):
    write_msg = i2c_msg.write(MICS_ADDRESS, [reg, val])
    smbus.i2c_rdwr(write_msg)


def initialize_mics():
    global mics_ready, mics_r0_ox, mics_r0_red

    print("\n[INIT] MiCS-4514...")

    if smbus is None:
        print("[ERROR] SMBus unavailable")
        return

    try:
        mode = _mics_read_bytes(0x0A, 1)[0]
        print(f"[MiCS] Power mode awal: 0x{mode:02X}")

        if mode == 0x00:
            _mics_write_byte(0x0A, 0x01)
            time.sleep(0.5)
            mode = _mics_read_bytes(0x0A, 1)[0]
            print(f"[MiCS] Power mode setelah wake-up: 0x{mode:02X}")

        print(f"[MiCS] Warmup: {MICS_WARMUP_SECONDS} seconds")
        for remaining in range(MICS_WARMUP_SECONDS, 0, -1):
            print(f"\r[MiCS] Remaining: {remaining:3d}s ", end="", flush=True)
            time.sleep(1)

        print("\n[MiCS] Warmup completed")

        ox_samples = []
        red_samples = []

        print("[MiCS] Starting R0 calibration...")
        for i in range(MICS_CALIBRATION_SAMPLES):
            data = _mics_read_bytes(0x04, 6)

            ox_adc = (data[0] << 8) | data[1]
            red_adc = (data[2] << 8) | data[3]
            power = (data[4] << 8) | data[5]

            ox = max(0, power - ox_adc)
            red = max(0, power - red_adc)

            ox_samples.append(ox)
            red_samples.append(red)

            print(
                f"[MiCS CAL] {i + 1}/{MICS_CALIBRATION_SAMPLES} "
                f"OX={ox} RED={red} POWER={power} (raw={data})"
            )
            time.sleep(1)

        mics_r0_ox = sum(ox_samples) / len(ox_samples)
        mics_r0_red = sum(red_samples) / len(red_samples)

        if mics_r0_ox <= 0 or mics_r0_red <= 0:
            raise ValueError("Invalid MiCS R0 calibration (POWER=0)")

        mics_ready = True
        print("[OK] MiCS-4514 initialized")
        print(f"[MiCS] R0 OX  = {mics_r0_ox:.2f}")
        print(f"[MiCS] R0 RED = {mics_r0_red:.2f}")

    except Exception as e:
        print(f"\n[ERROR] MiCS-4514: {e}")
        mics_ready = False


def calculate_gas(ratio_ox, ratio_red):
    co = 0
    if ratio_red <= 0.425:
        co = (0.425 - ratio_red) / 0.000405
        co = min(co, 1000)
        if co < 1:
            co = 0

    ch4 = 0
    if ratio_red <= 0.786:
        ch4 = (0.786 - ratio_red) / 0.000023
        ch4 = min(ch4, 25000)
        if ch4 < 1000:
            ch4 = 0

    ethanol = 0
    if ratio_red <= 0.306:
        ethanol = (0.306 - ratio_red) / 0.00057
        ethanol = min(ethanol, 500)
        if ethanol < 10:
            ethanol = 0

    h2 = 0
    if ratio_red <= 0.279:
        h2 = (0.279 - ratio_red) / 0.00026
        h2 = min(h2, 1000)
        if h2 < 1:
            h2 = 0

    nh3 = 0
    if ratio_red <= 0.8:
        nh3 = (0.8 - ratio_red) / 0.0015
        nh3 = min(nh3, 500)
        if nh3 < 1:
            nh3 = 0

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


def read_mics():
    if not mics_ready:
        return {}

    try:
        data = _mics_read_bytes(0x04, 6)

        ox_adc = (data[0] << 8) | data[1]
        red_adc = (data[2] << 8) | data[3]
        power = (data[4] << 8) | data[5]

        ox = max(0, power - ox_adc)
        red = max(0, power - red_adc)

        ratio_ox = ox / mics_r0_ox
        ratio_red = red / mics_r0_red

        gases = calculate_gas(ratio_ox, ratio_red)

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
# ADS1115 + MQ-136 INITIALIZATION & READING
# ============================================================

def initialize_ads():
    global ads, mq136

    print("\n[INIT] ADS1115 + MQ-136...")

    try:
        ads = ADS.ADS1115(i2c, address=ADS_ADDRESS)
        ads.gain = ADS_GAIN
        mq136 = AnalogIn(ads, ads1x15.Pin.A0)
        print("[OK] ADS1115 initialized")
        print("[OK] MQ-136 connected to A0")
    except Exception as e:
        print(f"[ERROR] ADS1115: {e}")
        ads = None
        mq136 = None


def read_mq136():
    if mq136 is None:
        return {}

    try:
        return {
            "mq136_raw": mq136.value,
            "mq136_voltage": mq136.voltage
        }
    except Exception as e:
        print(f"[ERROR] MQ-136 reading: {e}")
        return {}


# ============================================================
# SHT41 INITIALIZATION & READING
# ============================================================

def initialize_sht41():
    global sht41

    print("\n[INIT] SHT41...")

    try:
        sht41 = adafruit_sht4x.SHT4x(i2c, address=SHT41_ADDRESS)
        _ = sht41.serial_number  # Verifikasi nyata komunikasi I2C SHT41
        print("[OK] SHT41 initialized")
    except Exception as e:
        print(f"[ERROR] SHT41: {e}")
        sht41 = None


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
# BMP280 INITIALIZATION & READING
# ============================================================

def initialize_bmp280():
    global bmp280

    print("\n[INIT] BMP280...")

    try:
        bmp280 = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=BMP280_ADDRESS)
        bmp280.sea_level_pressure = 1013.25
        print("[OK] BMP280 initialized")
    except Exception as e:
        print(f"[ERROR] BMP280: {e}")
        bmp280 = None


def read_bmp280():
    if bmp280 is None:
        return {}

    try:
        return {
            "bmp280_temperature": bmp280.temperature,
            "bmp280_pressure": bmp280.pressure,
            "bmp280_altitude": bmp280.altitude
        }
    except Exception as e:
        print(f"[ERROR] BMP280 reading: {e}")
        return {}


# ============================================================
# CSV & DISPLAY
# ============================================================

def initialize_csv():
    exists = os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0
    if not exists:
        with open(LOG_FILE, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_HEADER)
            writer.writeheader()
        print(f"[CSV] Created: {LOG_FILE}")
    else:
        print(f"[CSV] Append mode: {LOG_FILE}")


def save_csv(data):
    try:
        with open(LOG_FILE, "a", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_HEADER)
            writer.writerow(data)
    except Exception as e:
        print(f"[ERROR] CSV: {e}")


def print_data():
    data = {key: "" for key in CSV_HEADER}
    data["timestamp"] = datetime.now().isoformat(timespec="seconds")

    data.update(read_sps30())
    data.update(read_mics())
    data.update(read_mq136())
    data.update(read_sht41())
    data.update(read_bmp280())

    print("\n" + "=" * 65)
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
    print(f"Particle size: {data['typical_particle_size']}")

    print("\n[MiCS-4514] GAS SENSOR")
    print(f"OX ADC    : {data['mics_ox_adc']}")
    print(f"RED ADC   : {data['mics_red_adc']}")
    print(f"POWER     : {data['mics_power']}")
    print(f"Ratio OX  : {data['mics_ratio_ox']}")
    print(f"Ratio RED : {data['mics_ratio_red']}")
    print(f"CO        : {data['co_ppm']} ppm")
    print(f"CH4       : {data['ch4_ppm']} ppm")
    print(f"Ethanol   : {data['ethanol_ppm']} ppm")
    print(f"H2        : {data['h2_ppm']} ppm")
    print(f"NH3       : {data['nh3_ppm']} ppm")
    print(f"NO2       : {data['no2_ppm']} ppm")

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
    return data


def cleanup():
    global smbus, sps30_transceiver

    print("\n[SYSTEM] Cleaning up...")
    if uploader is not None and uploader.enabled:
        uploader.flush(force=True)

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


def main():
    global uploader

    print("\n" + "=" * 65)
    print("       RASPBERRY PI ENVIRONMENT MONITORING (I2C BUS 3)")
    print("=" * 65)

    if not initialize_i2c():
        return

    initialize_sps30()
    initialize_ads()
    initialize_sht41()
    initialize_bmp280()
    initialize_mics()
    initialize_csv()

    uploader = Uploader(
        ENVIRO_URL,
        ENVIRO_API_KEY,
        ENVIRO_DEVICE_ID,
        ENVIRO_QUEUE,
        timeout=ENVIRO_TIMEOUT,
    )

    if uploader.enabled:
        print(f"[UPLOAD] Tujuan: {ENVIRO_URL}")
        print(f"[UPLOAD] Device: {ENVIRO_DEVICE_ID}")
        uploader.flush(force=True)
    else:
        print("[UPLOAD] ENVIRO_API_KEY belum diisi, mode CSV saja")

    print("\n[SYSTEM] Initialization completed")
    print(f"[SYSTEM] Interval: {INTERVAL} seconds")
    print(f"[SYSTEM] Output: {LOG_FILE}")
    print("[SYSTEM] Press CTRL+C to stop")

    try:
        while True:
            start_time = time.time()
            data = print_data()

            if uploader is not None and uploader.enabled:
                uploader.upload(data)

            elapsed = time.time() - start_time
            sleep_time = max(0, INTERVAL - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[SYSTEM] Monitoring stopped by user")
    except Exception as e:
        print(f"\n[SYSTEM] Unexpected error: {e}")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
