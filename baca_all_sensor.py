
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
