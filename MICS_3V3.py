#!/usr/bin/env python3
"""Tes Wake-Up Murni (Raw I2C) DFRobot SEN0377 di Raspberry Pi 5 (Bus 3)."""

import time
from smbus2 import SMBus, i2c_msg

I2C_BUS = 3
ADDR = 0x75

REG_DATA = 0x04
REG_MODE = 0x0A


def raw_read_bytes(bus, reg, length):
    """Membaca register menggunakan transaksi raw I2C write + read."""
    write_msg = i2c_msg.write(ADDR, [reg])
    read_msg = i2c_msg.read(ADDR, length)
    bus.i2c_rdwr(write_msg, read_msg)
    return list(read_msg)


def raw_write_byte(bus, reg, val):
    """Menulis tepat 2 byte [reg, val] secara langsung ke bus I2C."""
    write_msg = i2c_msg.write(ADDR, [reg, val])
    bus.i2c_rdwr(write_msg)


def main():
    print("=" * 60)
    print("  TES WAKE-UP RAW I2C MICS-4514 (BUS 3 - RASPBERRY PI 5)")
    print("=" * 60)

    with SMBus(I2C_BUS) as bus:
        # 1. Baca mode awal
        mode = raw_read_bytes(bus, REG_MODE, 1)[0]
        print(f"[1] Mode awal sebelum Wake-Up : 0x{mode:02X}")

        # 2. Kirim perintah Wake-Up dengan beberapa metode jika belum 0x01
        for attempt in range(1, 4):
            if mode == 0x01:
                break
            print(f"[2] Percobaan {attempt}: Mengirim raw write [0x0A, 0x01]...")
            raw_write_byte(bus, REG_MODE, 0x01)
            time.sleep(0.5)

            mode = raw_read_bytes(bus, REG_MODE, 1)[0]
            print(f"    -> Status mode sekarang   : 0x{mode:02X}")

            if mode == 0x00:
                # Fallback write_byte_data standar
                bus.write_byte_data(ADDR, REG_MODE, 0x01)
                time.sleep(0.5)
                mode = raw_read_bytes(bus, REG_MODE, 1)[0]
                print(f"    -> Status setelah fallback: 0x{mode:02X}")

        if mode != 0x01:
            print(
                "\n[KESIMPULAN HARDWARE]\n"
                "Status tetap 0x00! Ini berarti setiap kali pemanas (heater) menyala,\n"
                "tegangan VCC di sensor anjlok (drop) dan me-reset chip sensor kembali ke 0x00.\n"
                "SOLUSI:\n"
                "1. Pastikan kabel VCC & GND sensor pendek dan menancap kuat.\n"
                "2. Jika saat ini VCC di 3.3V, rangkaian Boost Converter U2 kekurangan arus.\n"
                "   Pasang kapasitor 10uF-100uF antara VCC dan GND dekat sensor, ATAU\n"
                "   gunakan suplai 5V dengan Logic Level Converter di jalur SDA/SCL."
            )
            return

        print("\n[OK] BERHASIL! Sensor sudah terkunci di WAKE_UP_MODE (0x01).")
        print("Membaca perubahan nilai ADC selama 10 detik...\n")

        for s in range(1, 11):
            raw = raw_read_bytes(bus, REG_DATA, 6)
            ox_adc = (raw[0] << 8) | raw[1]
            red_adc = (raw[2] << 8) | raw[3]
            pwr_adc = (raw[4] << 8) | raw[5]

            ox_data = max(0, pwr_adc - ox_adc)
            red_data = max(0, pwr_adc - red_adc)

            print(
                f"Detik {s:2d} | Raw={raw} | "
                f"ox_data={ox_data:4d}, red_data={red_data:4d}, PWR_ADC={pwr_adc:4d}"
            )
            time.sleep(1)


if __name__ == "__main__":
    main()
