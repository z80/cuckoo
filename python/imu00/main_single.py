import time
import math
from machine import I2C, Pin
from pyb import Pin, DAC, ADC
import bmi08

def load_calibration(filename="calibration.txt"):
    results = {}
    try:
        with open(filename) as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val_str = parts[1].strip().split()[0]
                    try:
                        val = int(val_str)
                        results[key] = val
                    except ValueError:
                        try:
                            results[key] = float(val_str)
                        except ValueError:
                            pass
    except OSError:
        print("File not found:", filename)
    return results


def profile( x ):
    abs_x = abs(x)
    ret = math.sqrt( abs_x )
    if x < 0.0:
        ret = -ret
    return ret


def calculate_gain( arg_min, arg_max, arg, val_min, val_max ):
    # For now use a linear formula.
    x = (arg - arg_min) / (arg_max - arg_min)

    if x < 0.0:
        return val_min

    if x > 1.0:
        return val_max

    val = (val_max - val_min) * x + val_min
    return val




X_NEGATIVE = False
Y_NEGATIVE = False
Z_NEGATIVE = True


def main():
    print( "Loading calibration" )
    vals = load_calibration()
    x_low  = vals['x_low']
    x_high = vals['x_high']
    y_low  = vals['y_low']
    y_high = vals['y_high']
    dac_high = vals['dac_high']
    dac_low  = vals['dac_low']
    x_dz_center = (x_high + x_low) * 0.5
    x_dz_range  = (x_high - x_low) * 0.5
    y_dz_center = (y_high + y_low) * 0.5
    y_dz_range  = (y_high - y_low) * 0.5


    en_on  = vals['en_on']
    en_off = vals['en_off']
    en_max = vals['en_max']

    gain_min = vals['gain_min']
    gain_max = vals['gain_max']

    gyro_deadzone = vals['gyro_dz']
    del vals

    out_en = False

    print( "Entered" )
    # Configure I2C2 with PB10 (SCL) and PB11 (SDA) at 10 kHz
    i2c = I2C(2, freq=100000)
    print( "Initialized I2C_2" )

    dev_list = i2c.scan()
    print( "dev list: ", dev_list )

    imu = bmi08.BMI08(i2c)
    print( "Created and configured BMI085" )

    imu.init()
    print( "Initialized" )

    #imu.init_gyro_fifo()
    print( "Initialized FIFO" )

    led1 = pyb.Pin('A15', Pin.OUT)
    led2 = pyb.Pin('C10', Pin.OUT)

    dac_x = DAC(Pin('A5'), bits=12, buffering=True)
    dac_y = DAC(Pin('A4'), bits=12, buffering=True)

    adc_en = ADC( Pin('A2') )
    pin_en = Pin('A3', Pin.OUT)
    adc_alpha = 0.3
    adc_accum = 600.0

    led1.off()
    led2.off()
    pin_en.off()

    print_timeout = 5
    print_counter = 0

    while True:

        adc = adc_en.read()
        adc_accum = adc_alpha*adc + (1.0-adc_alpha)*adc_accum
        if out_en:
            should_disable = adc_accum > en_off
            if should_disable:
                out_en = False
                pin_en.off()
                led1.off()

        else:
            should_enable = adc_accum < en_on
            if should_enable:
                out_en = True
                pin_en.on()
                led1.on()

        try:
            #x, y, z, qty = imu.read_gyro_sum()
            x, y, z = imu.read_gyro()
            qty = 1
        except:
            time.sleep( 0.01 )
            continue

        if qty != 0:
            if X_NEGATIVE:
                x = -x
            if Y_NEGATIVE:
                y = -y
            if Z_NEGATIVE:
                z = -z
            # 250 deg per second correspond to 32767.
            # Actually, for some reason I see numbers up to +/- 65535.
            # For dac it should be 2047
            # So, max to max gain is 2047 / 32767
            scale = 1.0 / (32768.0 * qty)
            x *= scale
            y *= scale
            z *= scale

            x = profile( x )
            y = profile( y )
            z = profile( z )

            scale = 2047.0
            x *= scale
            y *= scale
            z *= scale

            val_x = z
            val_y = x

            # Calculate gain based on current enable L2 value.
            gain = calculate_gain( en_on, en_max, adc_accum, gain_min, gain_max )

            if val_x < -gyro_deadzone:
                val_x = gain*(val_x + gyro_deadzone) - x_dz_range

            elif val_x > gyro_deadzone:
                val_x = gain*(val_x - gyro_deadzone) + x_dz_range

            else:
                val_x = 0.0

            if val_y < -gyro_deadzone:
                val_y = gain*(val_y + gyro_deadzone) - y_dz_range

            elif val_y > gyro_deadzone:
                val_y = gain*(val_y - gyro_deadzone) + y_dz_range

            else:
                val_y = 0.0

            val_x += x_dz_center
            val_y += y_dz_center

            val_x = int(val_x)
            val_y = int(val_y)

            if val_x < dac_low:
                val_x = dac_low
            elif val_x > dac_high:
                val_x = dac_high

            if val_y < dac_low:
                val_y = dac_low
            elif val_y > dac_high:
                val_y = dac_high

            dac_x.write( val_x )
            dac_y.write( val_y )

        time.sleep( 0.01 )

        print_counter += 1
        if print_counter >= print_timeout:
            #print( "x: ", val_x, "y: ", val_y, "z: ", val_z, "L2: ", adc_accum, "en: ", out_en )
            print( "x: ", val_x, "y: ", val_y, "L2: ", adc_accum, "en: ", out_en, "gain: ", gain )
            print_counter = 0

main()

