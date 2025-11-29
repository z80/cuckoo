import time
from machine import I2C, Pin
from pyb import Pin, DAC, ADC
import bmi08
GAIN_X = 1.0
GAIN_Y = 1.0

OUT_EN_TH = 300
OUT_DIS_TH = 600
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

imu.init_gyro_fifo()
print( "Initialized FIFO" )

led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)

dac_x = DAC(Pin('A4'), bits=12)
dac_y = DAC(Pin('A5'), bits=12)

adc_en = ADC( Pin('A2') )
pin_en = Pin('A3', Pin.OUT)

led1.off()
led2.off()
pin_en.off()

print_timeout = 25
print_counter = 0

while True:

    led1.on()
    adc = adc_en.read()
    if out_en:
        should_disable = adc > OUT_DIS_TH
        if should_disable:
            out_en = False
            pin_en.on()

    else:
        should_enable = adc < OUT_EN_TH
        if should_enable:
            out_en = True
            pin_en.off()

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
        val_x = int(GAIN_X * z + 2048)
        val_y = int(GAIN_Y * y + 2048)
        val_z = int(GAIN_Y * z + 2048)
        dac_x.write( val_x )
        dac_y.write( val_y )
        led2.off()

    led1.off()

    time.sleep( 0.002 )

    print_counter += 1
    if print_counter >= print_timeout:
        print( "x: ", val_x, "y: ", val_y, "z: ", val_z )
        print_counter = 0



