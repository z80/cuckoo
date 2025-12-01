import time
from machine import I2C, Pin
from pyb import Pin, DAC, ADC
import bmi08
GAIN_X = 1.0
GAIN_Y = 1.0
GAIN_Z = -1.0

OUT_EN_TH = 750
OUT_DIS_TH = 800
out_en = False

DAC_ZERO = 2048 + 700
DAC_RANGE = 1340

GYRO_X_DEAD_ZONE = 5
GYRO_Y_DEAD_ZONE = 5
GYRO_Z_DEAD_ZONE = 5

# Vertical
DAC_X_DEAD_ZONE_P = 600 # Down
DAC_X_DEAD_ZONE_N = 550 # Up

# Not used
DAC_Y_DEAD_ZONE_P = 600
DAC_Y_DEAD_ZONE_N = 300

# Horizontal
DAC_Z_DEAD_ZONE_P = 700 # Left
DAC_Z_DEAD_ZONE_N = 500 # Right

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

imu.init_gyro_fifo()
print( "Initialized FIFO" )

led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)

dac_x = DAC(Pin('A5'), bits=12)
dac_y = DAC(Pin('A4'), bits=12)

adc_en = ADC( Pin('A2') )
pin_en = Pin('A3', Pin.OUT)
adc_alpha = 0.3
adc_accum = 600.0

led1.off()
led2.off()
pin_en.off()

print_timeout = 25
print_counter = 0

while True:

    led1.on()
    adc = adc_en.read()
    adc_accum = adc_alpha*adc + (1.0-adc_alpha)*adc_accum
    if out_en:
        should_disable = adc_accum > OUT_DIS_TH
        if should_disable:
            out_en = False
            pin_en.off()

    else:
        should_enable = adc_accum < OUT_EN_TH
        if should_enable:
            out_en = True
            pin_en.on()

    try:
        x, y, z, qty = imu.read_gyro_sum()
    except:
        time.sleep( 0.002 )
        continue

    if qty != 0:
        led2.on()
        # 250 deg per second correspond to 32767.
        # For dac it should be 2047
        # So, max to max gain is 2047 / 32767
        scale = 2047 / (32767 * qty)
        x *= scale
        y *= scale
        z *= scale
        val_x = int(GAIN_X * x)
        if val_x > GYRO_X_DEAD_ZONE:
            val_x += DAC_X_DEAD_ZONE_P
        elif val_x < -GYRO_X_DEAD_ZONE:
            val_x -= DAC_X_DEAD_ZONE_N
        if val_x > DAC_RANGE:
            val_x = DAC_RANGE
        elif val_x < -DAC_RANGE:
            val_x = -DAC_RANGE

        val_y = int(GAIN_Y * y)
        if val_y > GYRO_Y_DEAD_ZONE:
            val_y += DAC_Y_DEAD_ZONE_P
        elif val_y < -GYRO_Y_DEAD_ZONE:
            val_y -= DAC_Y_DEAD_ZONE_N
        if val_y > DAC_RANGE:
            val_y = DAC_RANGE
        elif val_y < -DAC_RANGE:
            val_y = -DAC_RANGE

        val_z = int(GAIN_Z * z)
        if val_z > GYRO_Z_DEAD_ZONE:
            val_z += DAC_Z_DEAD_ZONE_P
        elif val_z < -GYRO_Z_DEAD_ZONE:
            val_z -= DAC_Z_DEAD_ZONE_N
        if val_z > DAC_RANGE:
            val_z = DAC_RANGE
        elif val_z < -DAC_RANGE:
            val_z = -DAC_RANGE

        out_x = val_x + DAC_ZERO
        out_y = val_y + DAC_ZERO
        out_z = val_z + DAC_ZERO

        dac_x.write( out_z )
        dac_y.write( out_x )
        led2.off()

    led1.off()

    time.sleep( 0.002 )

    print_counter += 1
    if print_counter >= print_timeout:
        #print( "x: ", val_x, "y: ", val_y, "z: ", val_z, "L2: ", adc_accum, "en: ", out_en )
        #print( "x: ", val_x, "z: ", val_z, "L2: ", adc_accum, "en: ", out_en )
        print_counter = 0



