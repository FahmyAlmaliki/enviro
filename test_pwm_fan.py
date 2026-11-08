import time
from gpiozero import PWMOutputDevice

# GPIO 5 pada penomoran BCM (sesuai pinout Raspberry Pi 5)
# Sesuaikan nilai frequency (Hz) sesuai kebutuhan, contoh: 1000 Hz (1 kHz)
pwm = PWMOutputDevice(5, frequency=1000)

try:
    print("Menyalakan PWM pada GPIO 5. Tekan Ctrl+C untuk berhenti.")
    
    while True:
        # Menaikkan duty cycle dari 0% ke 100% (fade in)
        print("Menaikkan brightness/kecepatan...")
        for duty in range(0, 60, 1):
            pwm.value = duty / 100.0
            time.sleep(0.05)
            
        # Menurunkan duty cycle dari 100% ke 0% (fade out)
        print("Menurunkan brightness/kecepatan...")
        for duty in range(60, -1, -1):
            pwm.value = duty / 100.0
            time.sleep(0.05)

except KeyboardInterrupt:
    # Matikan PWM saat program dihentikan pengguna
    pwm.off()
    print("\nProgram dihentikan. PWM dimatikan.")
