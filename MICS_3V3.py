#!/usr/bin/env python3
"""Program Pembacaan DFRobot SEN0377 (MiCS-4514) untuk Raspberry Pi 5.

Menggunakan I2C Bus 3 (/dev/i2c-3) sesuai konfigurasi dtoverlay dan
mengikuti spesifikasi protokol serta rumus konversi resmi DFRobot_MICS.
"""

import time
import smbus2

# ============================================================
# KONFIGURASI BUS & ALAMAT I2C (DFRobot SEN0377)
# ============================================================
I2C_BUS_NUMBER = 3  # Menggunakan /dev/i2c-3 pada Raspberry Pi 5

# Pilihan alamat sesuai Dip Switch (A0, A1) pada datasheet DFRobot:
# ADDRESS_0 = 0x75 (A0=0, A1=0)
# ADDRESS_1 = 0x76 (A0=1, A1=0)
# ADDRESS_2 = 0x77 (A0=0, A1=1)
# ADDRESS_3 = 0x78 (A0=1, A1=1)
CANDIDATE_ADDRESSES = [0x75, 0x78, 0x76, 0x77]

# Register internal MCU DFRobot MiCS-4514
REG_DATA_START = 0x04  # 6 byte: OX_H, OX_L, RED_H, RED_L, PWR_H, PWR_L
REG_POWER_MODE = 0x0A  # 0x00 = SLEEP_MODE, 0x01 = WAKE_UP_MODE

SLEEP_MODE = 0x00
WAKE_UP_MODE = 0x01

# Waktu kalibrasi/warmup standar datasheet adalah 3 menit (180 detik).
# Ubah ke 15-30 detik jika hanya ingin menguji respons perangkat keras.
CALIBRATION_TIME_SECONDS = 180
READ_INTERVAL_SECONDS = 1.0


class DFRobotMICS:
    """Implementasi Python untuk library resmi DFRobot_MICS_I2C."""

    def __init__(self, bus_number=I2C_BUS_NUMBER, address=None):
        self.bus = smbus2.SMBus(bus_number)
        self.address = address or self._auto_detect_address()
        self.r0_ox = 0.0
        self.r0_red = 0.0

    def _auto_detect_address(self):
        """Cari alamat I2C MiCS-4514 yang aktif di Bus 3."""
        for addr in CANDIDATE_ADDRESSES:
            try:
                self.bus.read_byte_data(addr, REG_POWER_MODE)
                print(f"[OK] Sensor MiCS-4514 terdeteksi pada alamat 0x{addr:02X} di i2c-{I2C_BUS_NUMBER}")
                return addr
            except OSError:
                continue
        raise RuntimeError(
            f"Sensor MiCS-4514 tidak ditemukan pada i2c-{I2C_BUS_NUMBER} "
            f"(alamat yang dicek: {[hex(a) for a in CANDIDATE_ADDRESSES]})."
        )

    def get_power_state(self):
        """Membaca status mode daya sensor (SLEEP_MODE=0x00, WAKE_UP_MODE=0x01)."""
        return self.bus.read_byte_data(self.address, REG_POWER_MODE)

    def wake_up_mode(self):
        """Mengaktifkan sensor dan boost converter 5V internal (SP_EN)."""
        self.bus.write_i2c_block_data(self.address, REG_POWER_MODE, [WAKE_UP_MODE])
        time.sleep(0.5)

    def sleep_mode(self):
        """Mematikan pemanas sensor (masuk ke Sleep Mode)."""
        self.bus.write_i2c_block_data(self.address, REG_POWER_MODE, [SLEEP_MODE])
        time.sleep(0.2)

    def read_sensor_raw(self):
        """Membaca 6 byte ADC dari register 0x04..0x09 sesuai library DFRobot.

        Mengembalikan:
            tuple: (ox_data, red_data, power_adc, ox_adc, red_adc)
            Di mana sesuai datasheet sample 3 (getADCData):
            ox_data  = power_adc - ox_adc
            red_data = power_adc - red_adc
        """
        data = self.bus.read_i2c_block_data(self.address, REG_DATA_START, 6)

        ox_adc = (data[0] << 8) | data[1]
        red_adc = (data[2] << 8) | data[3]
        power_adc = (data[4] << 8) | data[5]

        ox_data = max(0, power_adc - ox_adc)
        red_data = max(0, power_adc - red_adc)

        return ox_data, red_data, power_adc, ox_adc, red_adc

    def warm_up_and_calibrate(self, warmup_seconds=CALIBRATION_TIME_SECONDS, samples=10):
        """Menjalankan pemanasan sensor (warmUpTime) dan mencatat baseline R0."""
        print(f"[INFO] Memulai pemanasan (warm-up) selama {warmup_seconds} detik di udara bersih...")
        for remaining in range(warmup_seconds, 0, -1):
            # Setiap 5 detik tampilkan nilai ADC saat ini untuk memantau boost converter 5V
            if remaining % 5 == 0 or remaining == warmup_seconds or remaining == 1:
                try:
                    ox_d, red_d, pwr, ox_raw, red_raw = self.read_sensor_raw()
                    print(
                        f"\r[WARMUP {remaining:3d}s] "
                        f"ox_data={ox_d:4d} | red_data={red_d:4d} | "
                        f"PWR_ADC={pwr:4d} (raw OX={ox_raw:4d}, RED={red_raw:4d})   ",
                        end="",
                        flush=True,
                    )
                except OSError as err:
                    print(f"\r[WARMUP {remaining:3d}s] I2C Read Retry ({err})   ", end="", flush=True)
            time.sleep(1)
        print("\n[OK] Waktu pemanasan selesai.")

        # Ambil sampel rata-rata untuk nilai dasar R0 (udara bersih)
        ox_list = []
        red_list = []
        print(f"[INFO] Mengambil {samples} sampel kalibrasi R0...")

        for i in range(samples):
            ox_d, red_d, pwr, ox_raw, red_raw = self.read_sensor_raw()
            ox_list.append(ox_d)
            red_list.append(red_d)
            print(
                f"  Sampel {i + 1:2d}/{samples} -> "
                f"ox_data={ox_d:4d}, red_data={red_d:4d}, PWR_ADC={pwr:4d}"
            )
            time.sleep(1)

        self.r0_ox = sum(ox_list) / len(ox_list)
        self.r0_red = sum(red_list) / len(red_list)

        if self.r0_ox <= 0 or self.r0_red <= 0:
            raise RuntimeError(
                f"Kalibrasi R0 gagal (R0_OX={self.r0_ox:.1f}, R0_RED={self.r0_red:.1f}). "
                "Nilai PWR_ADC=0 menandakan sirkuit Boost 5V (U2) pada modul belum mendapatkan arus yang cukup."
            )

        print(f"[OK] Kalibrasi berhasil! R0_OX = {self.r0_ox:.2f} | R0_RED = {self.r0_red:.2f}")

    def get_all_gas_ppm(self):
        """Menghitung konsentrasi seluruh gas (PPM) & status deteksi kebocoran."""
        ox_data, red_data, power_adc, ox_adc, red_adc = self.read_sensor_raw()

        ratio_ox = (ox_data / self.r0_ox) if self.r0_ox > 0 else 0.0
        ratio_red = (red_data / self.r0_red) if self.r0_red > 0 else 0.0

        # 1. Carbon Monoxide (CO): Range 1 - 1000 PPM
        co = (0.425 - ratio_red) / 0.000405 if ratio_red <= 0.425 else 0.0
        co = min(max(co, 0.0), 1000.0)
        if co < 1.0:
            co = 0.0

        # 2. Methane (CH4): Range 1000 - 25000 PPM
        ch4 = (0.786 - ratio_red) / 0.000023 if ratio_red <= 0.786 else 0.0
        ch4 = min(max(ch4, 0.0), 25000.0)
        if ch4 < 1000.0:
            ch4 = 0.0

        # 3. Ethanol (C2H5OH): Range 10 - 500 PPM
        c2h5oh = (0.306 - ratio_red) / 0.00057 if ratio_red <= 0.306 else 0.0
        c2h5oh = min(max(c2h5oh, 0.0), 500.0)
        if c2h5oh < 10.0:
            c2h5oh = 0.0

        # 4. Hydrogen (H2): Range 1 - 1000 PPM
        h2 = (0.279 - ratio_red) / 0.00026 if ratio_red <= 0.279 else 0.0
        h2 = min(max(h2, 0.0), 1000.0)
        if h2 < 1.0:
            h2 = 0.0

        # 5. Ammonia (NH3): Range 1 - 500 PPM
        nh3 = (0.8 - ratio_red) / 0.0015 if ratio_red <= 0.8 else 0.0
        nh3 = min(max(nh3, 0.0), 500.0)
        if nh3 < 1.0:
            nh3 = 0.0

        # 6. Nitrogen Dioxide (NO2): Range 0.1 - 10 PPM
        no2 = (ratio_ox - 0.045) / 6.13 if ratio_ox >= 1.1 else 0.0
        no2 = min(max(no2, 0.0), 10.0)
        if no2 < 0.1:
            no2 = 0.0

        # Status kebocoran sesuai fungsi getGasExist() di datasheet Sample 2
        leak_status = {
            "CO": ratio_red < 0.425,
            "CH4": ratio_red < 0.786,
            "C2H5OH": ratio_red < 0.306,
            "H2": ratio_red < 0.279,
            "NH3": ratio_red < 0.800,
            "NO2": ratio_ox > 1.100,
        }

        return {
            "ox_data": ox_data,
            "red_data": red_data,
            "power_adc": power_adc,
            "ox_adc": ox_adc,
            "red_adc": red_adc,
            "ratio_ox": round(ratio_ox, 4),
            "ratio_red": round(ratio_red, 4),
            "co_ppm": round(co, 1),
            "ch4_ppm": round(ch4, 1),
            "c2h5oh_ppm": round(c2h5oh, 1),
            "h2_ppm": round(h2, 1),
            "nh3_ppm": round(nh3, 1),
            "no2_ppm": round(no2, 2),
            "leak_exist": leak_status,
        }

    def close(self):
        self.bus.close()


def main():
    print("=" * 68)
    print("  DFROBOT SEN0377 (MiCS-4514) MONITOR - RASPBERRY PI 5 (I2C BUS 3)")
    print("=" * 68)

    mics = None
    try:
        mics = DFRobotMICS(bus_number=I2C_BUS_NUMBER)

        # Cek status daya sesuai prosedur setup() pada datasheet DFRobot
        mode = mics.get_power_state()
        if mode == SLEEP_MODE:
            print("[INIT] Sensor berada dalam SLEEP_MODE (0x00). Mengirim perintah Wake-Up...")
            mics.wake_up_mode()
            verify_mode = mics.get_power_state()
            print(f"[INIT] Status mode setelah Wake-Up: 0x{verify_mode:02X}")
        else:
            print(f"[INIT] Sensor sudah dalam WAKE_UP_MODE (0x{mode:02X}).")

        # Jalankan pemanasan & kalibrasi
        mics.warm_up_and_calibrate(warmup_seconds=CALIBRATION_TIME_SECONDS)

        print("\n[LOOP] Membaca data konsentrasi gas secara real-time (Ctrl+C untuk berhenti)...")
        print("-" * 68)

        while True:
            res = mics.get_all_gas_ppm()
            active_leaks = [gas for gas, exist in res["leak_exist"].items() if exist]
            leak_str = ", ".join(active_leaks) if active_leaks else "Aman (Tidak ada)"

            print(
                f"[ADC] ox_data={res['ox_data']:4d} | red_data={res['red_data']:4d} | "
                f"PWR={res['power_adc']:4d} | Ratio OX={res['ratio_ox']:.3f} | Ratio RED={res['ratio_red']:.3f}"
            )
            print(
                f"[PPM] CO={res['co_ppm']:6.1f} | C2H5OH={res['c2h5oh_ppm']:5.1f} | "
                f"H2={res['h2_ppm']:6.1f} | NH3={res['nh3_ppm']:5.1f} | "
                f"CH4={res['ch4_ppm']:7.1f} | NO2={res['no2_ppm']:4.2f}"
            )
            print(f"[DETEKSI GAS] Indikasi Gas Terdeteksi: {leak_str}\n")

            time.sleep(READ_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[INFO] Program dihentikan oleh pengguna.")
    except Exception as err:
        print(f"\n[ERROR] {err}")
    finally:
        if mics is not None:
            mics.close()
            print("[INFO] Koneksi I2C Bus 3 ditutup.")


if __name__ == "__main__":
    main()
