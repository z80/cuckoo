import time
from machine import I2C, Pin
import bmi08   # your C extension module

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

while True:
    try:
        #ret = imu.read_gyro()
        ret = imu.read_gyro_sum()
        print( ret )
    except:
        pass
    time.sleep( 0.03 )


