#!/usr/bin/env python3
"""Program standalone pembacaan sensor gas DFRobot MiCS-4514 via I2C."""

import time
import smbus2

# ============================================================
# KONFIGURASI
# ============================================================
I2C_BUS_NUMBER = 1
MICS_ADDRESS = 0x75

# Waktu pemanasan (standar: 180 detik, bisa diubah ke 10-30 detik untuk tes cepat)
MICS_WARMUP_SECONDS = 180
MICS_CALIBRATION_SAMPLES = 10
READ_INTERVAL = 2


# ============================================================
# KONVERSI GAS (Persamaan DFRobot)
# ============================================================
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
        if nh3 < 10:
            nh3 = 0

    no2 = 0
    if ratio_ox >= 1.1:
        no2 = (ratio_ox - 0.045) / 6.13
        no2 = min(no2, 10)
        if no2 < 0.1:
            no2 = 0

    return {
        "co_ppm": round(co, 2),
        "ch4_ppm": round(ch4, 2),
        "ethanol_ppm": round(ethanol, 2),
        "h2_ppm": round(h2, 2),
        "nh3_ppm": round(nh3, 2),
        "no2_ppm": round(no2, 2),
    }


# ============================================================
# PROGRAM UTAMA
# ============================================================
def main():
    print("=" * 55)
    print("        TEST PEMBACAAN SENSOR MiCS-4514 (I2C)")
    print("=" * 55)

    try:
        bus = smbus2.SMBus(I2C_BUS_NUMBER)
        print(f"[OK] Bus I2C-{I2C_BUS_NUMBER} berhasil dibuka")
    except Exception as e:
        print(f"[ERROR] Gagal membuka I2C bus: {e}")
        return

    try:
        # 1. Cek dan aktifkan mode daya sensor (Register 0x0A)
        mode = bus.read_byte_data(MICS_ADDRESS, 0x0A)
        print(f"[MiCS] Power mode saat ini: 0x{mode:02X}")

        if mode == 0x00:
            bus.write_byte_data(MICS_ADDRESS, 0x0A, 0x01)
            print("[MiCS] Mengaktifkan sensor (Wake up)...")
            time.sleep(0.5)

        # 2. Proses Pemanasan (Warmup)
        print(f"[MiCS] Pemanasan sensor selama {MICS_WARMUP_SECONDS} detik...")
        for remaining in range(MICS_WARMUP_SECONDS, 0, -1):
            print(f"\r[MiCS] Sisa waktu warmup: {remaining:3d} detik ", end="", flush=True)
            time.sleep(1)
        print("\n[MiCS] Warmup selesai.")

        # 3. Kalibrasi R0
        ox_samples = []
        red_samples = []
        print(f"\n[MiCS] Mengambil {MICS_CALIBRATION_SAMPLES} sampel kalibrasi R0...")

        for i in range(MICS_CALIBRATION_SAMPLES):
            data = bus.read_i2c_block_data(MICS_ADDRESS, 0x04, 6)

            ox_adc = (data[0] << 8) | data[1]
            red_adc = (data[2] << 8) | data[3]
            power = (data[4] << 8) | data[5]

            ox = max(0, power - ox_adc)
            red = max(0, power - red_adc)

            ox_samples.append(ox)
            red_samples.append(red)

            print(
                f"  Sampel {i + 1:2d}/{MICS_CALIBRATION_SAMPLES} | "
                f"OX_ADC={ox_adc:4d}, RED_ADC={red_adc:4d}, POWER={power:4d} "
                f"-> OX={ox:4d}, RED={red:4d}"
            )
            time.sleep(1)

        r0_ox = sum(ox_samples) / len(ox_samples)
        r0_red = sum(red_samples) / len(red_samples)

        print("-" * 55)
        print(f"[HASIL R0] R0_OX = {r0_ox:.2f} | R0_RED = {r0_red:.2f}")
        print("-" * 55)

        if r0_ox <= 0 or r0_red <= 0:
            print(
                "[ERROR] Kalibrasi R0 tidak valid (bernilai 0).\n"
                "Pastikan pin VCC MiCS-4514 terhubung ke 5V agar heater menyala!"
            )
            return

        # 4. Loop Pembacaan Data Kontinu
        print("\n[SYSTEM] Mulai membaca data (Tekan CTRL+C untuk berhenti)...\n")
        while True:
            data = bus.read_i2c_block_data(MICS_ADDRESS, 0x04, 6)

            ox_adc = (data[0] << 8) | data[1]
            red_adc = (data[2] << 8) | data[3]
            power = (data[4] << 8) | data[5]

            ox = max(0, power - ox_adc)
            red = max(0, power - red_adc)

            ratio_ox = ox / r0_ox
            ratio_red = red / r0_red

            gases = calculate_gas(ratio_ox, ratio_red)

            print(
                f"[RAW] OX_ADC: {ox_adc:4d} | RED_ADC: {red_adc:4d} | "
                f"PWR: {power:4d} | Ratio OX: {ratio_ox:.3f} | Ratio RED: {ratio_red:.3f}"
            )
            print(
                f"[GAS] CO: {gases['co_ppm']} ppm | CH4: {gases['ch4_ppm']} ppm | "
                f"EtOH: {gases['ethanol_ppm']} ppm | H2: {gases['h2_ppm']} ppm | "
                f"NH3: {gases['nh3_ppm']} ppm | NO2: {gases['no2_ppm']} ppm\n"
            )

            time.sleep(READ_INTERVAL)

    except KeyboardInterrupt:
        print("\n[SYSTEM] Pembacaan dihentikan oleh pengguna.")
    except Exception as e:
        print(f"\n[ERROR] Terjadi kesalahan pembacaan MiCS-4514: {e}")
    finally:
        bus.close()
        print("[SYSTEM] Koneksi I2C ditutup.")


if __name__ == "__main__":
    main()
