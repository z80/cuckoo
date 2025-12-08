import time
from machine import I2C, Pin
from pyb import Pin, DAC, ADC
import bmi08



def main():
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

    print_timeout = 100
    print_counter = 1000

    x_min = 0
    x_max = 0
    y_min = 0
    y_max = 0
    z_min = 0
    z_max = 0

    while True:

        led1.on()
        try:
            x, y, z, qty = imu.read_gyro_sum()
        except:
            time.sleep( 0.002 )
            continue

        if qty == 0:
            time.sleep( 0.002 )
            continue

        x = x // qty
        y = y // qty
        x = z // qty

        if x < x_min:
            x_min = x

        if x > x_max:
            x_max = x

        if y < y_min:
            y_min = x

        if y > y_max:
            y_max = y

        if z < z_min:
            z_min = z

        if z > z_max:
            z_max = z

        if (z > 32767) or (z < -32767):
            if (print_counter >= print_timeout):
                print_counter = 0

        if (print_counter < print_timeout):
            print_counter += 1
            print( "z_min:", z_min, "; z_max:", z_max, "; z:", z )


        time.sleep( 0.002 )

main()

