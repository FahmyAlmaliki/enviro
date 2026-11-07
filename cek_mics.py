#!/usr/bin/env python3
"""Diagnostik & Pembacaan DFRobot MiCS-4514 via I2C."""

import time
import smbus2

I2C_BUS_NUMBER = 1
MICS_ADDRESS = 0x75
MICS_WARMUP_SECONDS = 10


def wakeup_mics(bus):
    """Bangunkan sensor dan pastikan tidak ter-reset kembali ke 0x00."""
    for attempt in range(1, 4):
        mode = bus.read_byte_data(MICS_ADDRESS, 0x0A)
        print(f"[CEK {attempt}] Power mode awal: 0x{mode:02X}")

        if mode == 0x01:
            print("[OK] Sensor sudah dalam keadaan aktif (0x01)!")
            return True

        print(f"[ACTION] Mengirim perintah Wake-up (percobaan {attempt})...")
        # Gunakan write_i2c_block_data sesuai library resmi DFRobot
        bus.write_i2c_block_data(MICS_ADDRESS, 0x0A, [0x01])
        time.sleep(1.0)

        mode_after = bus.read_byte_data(MICS_ADDRESS, 0x0A)
        print(f"[VERIFIKASI] Power mode setelah wake-up: 0x{mode_after:02X}")

        if mode_after == 0x01:
            return True

        print("[WARNING] Mode kembali ke 0x00! Kemungkinan tegangan drop saat heater menyala.")

    return False


def read_raw_adc(bus):
    """Baca 6 byte dari register 0x04 dengan jeda sinkronisasi."""
    # Tulis alamat register terlebih dahulu jika smbus block read langsung gagal
    data = bus.read_i2c_block_data(MICS_ADDRESS, 0x04, 6)
    ox_adc = (data[0] << 8) | data[1]
    red_adc = (data[2] << 8) | data[3]
    power = (data[4] << 8) | data[5]
    return ox_adc, red_adc, power, data


def main():
    print("=" * 60)
    print("       DIAGNOSTIK SENSOR MiCS-4514 (DFROBOT I2C)")
    print("=" * 60)

    bus = smbus2.SMBus(I2C_BUS_NUMBER)

    try:
        if not wakeup_mics(bus):
            print(
                "\n[FATAL] Sensor gagal mempertahankan mode aktif (0x01)!\n"
                "Penyebab: Saat heater dinyalakan, tegangan drop dan mereset chip sensor.\n"
                "Solusi:\n"
                "  1. Pastikan pin VCC modul MiCS-4514 dihubungkan ke pin 5V Raspberry Pi.\n"
                "  2. Gunakan kabel jumper yang pendek dan tidak longgar pada VCC & GND."
            )
            return

        print(f"\n[MiCS] Pemanasan awal {MICS_WARMUP_SECONDS} detik...")
        for remaining in range(MICS_WARMUP_SECONDS, 0, -1):
            print(f"\r[MiCS] Sisa waktu: {remaining:2d} detik ", end="", flush=True)
            time.sleep(1)
        print("\n")

        # Cek ulang apakah selama warmup sensor ter-reset
        current_mode = bus.read_byte_data(MICS_ADDRESS, 0x0A)
        print(f"[STATUS] Power mode setelah warmup: 0x{current_mode:02X}\n")

        for i in range(10):
            ox_adc, red_adc, power, raw_bytes = read_raw_adc(bus)
            ox = max(0, power - ox_adc)
            red = max(0, power - red_adc)
            print(
                f"Sampel {i+1:2d} | Raw Bytes: {raw_bytes} | "
                f"OX_ADC={ox_adc:4d}, RED_ADC={red_adc:4d}, POWER={power:4d} "
                f"-> OX={ox:4d}, RED={red:4d}"
            )
            time.sleep(1)

    finally:
        bus.close()


if __name__ == "__main__":
    main()
