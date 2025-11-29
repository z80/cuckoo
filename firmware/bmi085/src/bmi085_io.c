
#include "bmi085_io.h"

#include "cmsis_os.h"
#include "bmi08.h"
#include "bmi08x.h"
#include "bmi08_defs.h"


#define BMI08_READ_WRITE_LEN  UINT8_C(46)

#define MUL_ADDR_1      (0x70<<1)
#define MUL_ADDR_2      (0x71<<1)
#define I2C_TIMEOUT     168000000

extern I2C_HandleTypeDef hi2c1;
extern I2C_HandleTypeDef hi2c2;

struct T_BMI085
{
	uint8_t acc_addr;
	uint8_t gyro_addr;
	struct bmi08_dev bmi085;
};

static void bmi08_interface_init( uint8_t is_primary, uint8_t i2c_bus_index, struct T_BMI085 *bma );
static uint8_t bmi08_hardware_init( struct T_BMI085 * dev );

static void bmi085_delay( uint32_t usec, void *intf_ptr );

static uint8_t bmi085_switch_1( uint8_t channel );
static uint8_t bmi085_switch_2( uint8_t channel );
static BMI08_INTF_RET_TYPE bmi085_bus_read_i2c_1( uint8_t reg_addr, uint8_t *reg_data, uint8_t qty, void *intf_ptr );
static BMI08_INTF_RET_TYPE bmi085_bus_write_i2c_1( uint8_t reg_addr, uint8_t *reg_data, uint8_t qty, void *intf_ptr );
static BMI08_INTF_RET_TYPE bmi085_bus_read_i2c_2( uint8_t reg_addr, uint8_t * reg_data, uint8_t qty, void *intf_ptr );
static BMI08_INTF_RET_TYPE bmi085_bus_write_i2c_2( uint8_t reg_addr, uint8_t * reg_data, uint8_t qty, void *intf_ptr );


uint8_t bmi085_init( uint8_t index )
{
	struct T_BMI085 bmi;
	// 0..15 are on I2C1, 16..31 are on I2C2.
	uint8_t i2c_bus_index  = (index < 16) ? 0 : 1;
	// 0, 2, 4, ..., 8 are on primary address.
	// 1, 3, ..., 15 are on secondary address.
	uint8_t use_primary_addr = ( (index & 1) == 0 ) ? 1 : 0;
	if (index >= 16)
		index -= 16;
	// I2C multiplexer index 0..7.
	uint8_t channel_ind      = index / 2;

	bmi08_interface_init( use_primary_addr, i2c_bus_index, &bmi );
	if ( i2c_bus_index == 0 )
	{
		uint8_t ret = bmi085_switch_1( channel_ind );
		if ( ret != 0 )
			return 100;
	}
	else
	{
		uint8_t ret = bmi085_switch_2( channel_ind );
		if ( ret != 0 )
			return 101;
	}
	int8_t rslt = bmi08_hardware_init( &bmi );
	if ( rslt != BMI08_OK )
		return (uint8_t)rslt;

	return 0;
}


// Indices 0-15.
// Indices 16-31.
uint8_t bmi085_switch_irq( uint8_t index )
{
	//uint8_t use_primary_addr = ( (index & 1) == 0 ) ? 1 : 0;
	uint8_t i2c_bus_index  = (index < 16) ? 0 : 1;
	if ( index >= 16 )
		index -= 16;
	// I2C multiplexer index 0..7.
	uint8_t channel_ind      = index / 2;


	uint8_t result = 0;
	if ( i2c_bus_index == 0 )
	{
		// Declared static so it preserves.
	    static unsigned char data_1;
	    data_1 = (1 << channel_ind);

		HAL_StatusTypeDef res = HAL_I2C_Master_Transmit_IT(&hi2c1, MUL_ADDR_1, &data_1, 1 );
		result = ( res == HAL_OK ) ? 0 : 1;
	}
	else
	{
		// Declared static so it preserves.
	    static unsigned char data_2;
	    data_2 = (1 << channel_ind);

		HAL_StatusTypeDef res = HAL_I2C_Master_Transmit_IT(&hi2c2, MUL_ADDR_2, &data_2, 1 );
		result = ( res == HAL_OK ) ? 0 : 1;
	}

	return result;
}

uint8_t bmi085_read_acc_irq( uint8_t index, uint8_t * data )
{
	// 0..15 are on I2C1, 16..31 are on I2C2.
	uint8_t i2c_bus_index  = (index < 16) ? 0 : 1;
	// 0, 2, 4, ..., 8 are on primary address.
	// 1, 3, ..., 15 are on secondary address.
	uint8_t use_primary_addr = ( (index & 1) == 0 ) ? 1 : 0;
	if (index >= 16)
		index -= 16;
	// I2C multiplexer index 0..7.
	uint8_t channel_ind      = index / 2;

	uint8_t dev_addr = (use_primary_addr) ? (BMI08_ACCEL_I2C_ADDR_PRIMARY << 1) : (BMI08_ACCEL_I2C_ADDR_SECONDARY << 1);

	uint8_t result = 0;
	if ( i2c_bus_index == 0 )
	{
		HAL_StatusTypeDef res = HAL_I2C_Mem_Read_IT( &hi2c1, dev_addr, BMI08_REG_ACCEL_X_LSB, I2C_MEMADD_SIZE_8BIT, data, 6 );
		result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	}
	else
	{
		HAL_StatusTypeDef res = HAL_I2C_Mem_Read_IT( &hi2c2, dev_addr, BMI08_REG_ACCEL_X_LSB, I2C_MEMADD_SIZE_8BIT, data, 6 );
		result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	}

	return result;
}

uint8_t bmi085_read_gyro_irq( uint8_t index, uint8_t * data )
{
	// 0..15 are on I2C1, 16..31 are on I2C2.
	uint8_t i2c_bus_index  = (index < 16) ? 0 : 1;
	// 0, 2, 4, ..., 8 are on primary address.
	// 1, 3, ..., 15 are on secondary address.
	uint8_t use_primary_addr = ( (index & 1) == 0 ) ? 1 : 0;
	if (index >= 16)
		index -= 16;
	// I2C multiplexer index 0..7.
	uint8_t channel_ind      = index / 2;

	uint8_t dev_addr = (use_primary_addr) ? (BMI08_GYRO_I2C_ADDR_PRIMARY << 1) : (BMI08_GYRO_I2C_ADDR_SECONDARY << 1);

	uint8_t result = 0;
	if ( i2c_bus_index == 0 )
	{
		HAL_StatusTypeDef res = HAL_I2C_Mem_Read_IT( &hi2c1, dev_addr, BMI08_REG_GYRO_X_LSB, I2C_MEMADD_SIZE_8BIT, data, 6 );
		result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	}
	else
	{
		HAL_StatusTypeDef res = HAL_I2C_Mem_Read_IT( &hi2c2, dev_addr, BMI08_REG_GYRO_X_LSB, I2C_MEMADD_SIZE_8BIT, data, 6 );
		result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	}

	return result;
}









static void bmi085_delay( uint32_t usec, void *intf_ptr )
{
	(void)intf_ptr;

	uint32_t msec = usec / 1000;
	if (msec < 1)
		msec = 1;
	osDelay( msec );
}

static uint8_t bmi085_switch_1( uint8_t channel )
{
    unsigned char data = (1 << channel);
	HAL_StatusTypeDef res = HAL_I2C_Master_Transmit( &hi2c1, MUL_ADDR_1, &data, 1, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? 0 : 1;

    return result;
}

static uint8_t bmi085_switch_2( uint8_t channel )
{
    unsigned char data = (1 << channel);
	HAL_StatusTypeDef res = HAL_I2C_Master_Transmit( &hi2c2, MUL_ADDR_2, &data, 1, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? 0 : 1;

    return result;
}

static BMI08_INTF_RET_TYPE bmi085_bus_read_i2c_1( uint8_t reg_addr, uint8_t *reg_data, uint8_t qty, void *intf_ptr )
{
	uint8_t dev_addr = *(uint8_t*)intf_ptr;
	dev_addr *= 2;
	HAL_StatusTypeDef res = HAL_I2C_Mem_Read( &hi2c1, dev_addr, reg_addr, I2C_MEMADD_SIZE_8BIT, reg_data, qty, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	return result;
}

static BMI08_INTF_RET_TYPE bmi085_bus_write_i2c_1( uint8_t reg_addr, uint8_t *reg_data, uint8_t qty, void *intf_ptr )
{
	uint8_t dev_addr = *(uint8_t*)intf_ptr;
	dev_addr *= 2;
	HAL_StatusTypeDef res = HAL_I2C_Mem_Write( &hi2c1, dev_addr, reg_addr, I2C_MEMADD_SIZE_8BIT, reg_data, qty, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	return result;
}

static BMI08_INTF_RET_TYPE bmi085_bus_read_i2c_2( uint8_t reg_addr, uint8_t * reg_data, uint8_t qty, void *intf_ptr )
{
	uint8_t dev_addr = *(uint8_t*)intf_ptr;
	dev_addr *= 2;
	HAL_StatusTypeDef res = HAL_I2C_Mem_Read( &hi2c2, dev_addr, reg_addr, I2C_MEMADD_SIZE_8BIT, reg_data, qty, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	return result;
}

static BMI08_INTF_RET_TYPE bmi085_bus_write_i2c_2( uint8_t reg_addr, uint8_t * reg_data, uint8_t qty, void *intf_ptr )
{
	uint8_t dev_addr = *(uint8_t*)intf_ptr;
	dev_addr *= 2;
	HAL_StatusTypeDef res = HAL_I2C_Mem_Write( &hi2c2, dev_addr, reg_addr, I2C_MEMADD_SIZE_8BIT, reg_data, qty, I2C_TIMEOUT );
	int8_t result = ( res == HAL_OK ) ? BMI08_INTF_RET_SUCCESS : (BMI08_INTF_RET_SUCCESS+1);
	return result;
}




static void bmi08_interface_init( uint8_t use_primary_addr, uint8_t i2c_bus_index, struct T_BMI085 *bma )
{
	if ( use_primary_addr )
	{
		bma->acc_addr  = BMI08_ACCEL_I2C_ADDR_PRIMARY;
		bma->gyro_addr = BMI08_GYRO_I2C_ADDR_PRIMARY;
	}
	else
	{
		bma->acc_addr  = BMI08_ACCEL_I2C_ADDR_SECONDARY;
		bma->gyro_addr = BMI08_GYRO_I2C_ADDR_SECONDARY;
	}

	struct bmi08_dev * bmi08 = &(bma->bmi085);

    bmi08->intf = BMI08_I2C_INTF;
    if ( i2c_bus_index == 0 )
    {
    	bmi08->read = bmi085_bus_read_i2c_1;
    	bmi08->write = bmi085_bus_write_i2c_1;
    }
    else
    {
    	bmi08->read = bmi085_bus_read_i2c_2;
    	bmi08->write = bmi085_bus_write_i2c_2;
    }

    /* Selection of bmi085 or bmi088 sensor variant */
    bmi08->variant = BMI085_VARIANT;

    /* Assign accel device address to accel interface pointer */
    bmi08->intf_ptr_accel = &(bma->acc_addr);

    /* Assign gyro device address to gyro interface pointer */
    bmi08->intf_ptr_gyro = &(bma->gyro_addr);

    /* Configure delay in microseconds */
    bmi08->delay_us = bmi085_delay;

    /* Configure max read/write length (in bytes) ( Supported length depends on target machine) */
    bmi08->read_write_len = BMI08_READ_WRITE_LEN;
}

static uint8_t bmi08_hardware_init( struct T_BMI085 * dev )
{
	struct bmi08_dev * bmi08 = &(dev->bmi085);

    int8_t rslt;

    rslt = bmi08a_soft_reset( bmi08 );
    if ( rslt != BMI08_OK )
    	return 1;

    rslt = bmi08g_soft_reset( bmi08 );
    if ( rslt != BMI08_OK )
    	return 2;

    rslt = bmi08xa_init(bmi08);
    if ( rslt != BMI08_OK )
    	return 3;

    rslt = bmi08a_init(bmi08);
    if ( rslt != BMI08_OK )
    	return 4;

    rslt = bmi08g_init(bmi08);
    if ( rslt != BMI08_OK )
    	return 5;

    rslt = bmi08a_load_config_file(bmi08);
    if ( rslt != BMI08_OK )
    	return 6;

 	bmi08->accel_cfg.odr = BMI08_ACCEL_ODR_100_HZ;
   	bmi08->accel_cfg.range = BMI085_ACCEL_RANGE_8G;

    bmi08->accel_cfg.power = BMI08_ACCEL_PM_ACTIVE;
    bmi08->accel_cfg.bw = BMI08_ACCEL_BW_NORMAL;

    rslt = bmi08a_set_power_mode( bmi08 );
    if ( rslt != BMI08_OK )
    	return 7;

    rslt = bmi08xa_set_meas_conf( bmi08 );
    if ( rslt != BMI08_OK )
    	return 8;

    bmi08->gyro_cfg.odr = BMI08_GYRO_BW_47_ODR_400_HZ;
    bmi08->gyro_cfg.range = BMI08_GYRO_RANGE_250_DPS;
    bmi08->gyro_cfg.bw = BMI08_GYRO_BW_47_ODR_400_HZ;
    bmi08->gyro_cfg.power = BMI08_GYRO_PM_NORMAL;

    rslt = bmi08g_set_power_mode(bmi08);
    if ( rslt != BMI08_OK )
    	return 9;

    rslt = bmi08g_set_meas_conf(bmi08);
    if ( rslt != BMI08_OK )
    	return 10;

    //struct bmi08_data_sync_cfg sync_cfg;
    //sync_cfg.mode = BMI08_ACCEL_DATA_SYNC_MODE_400HZ;

    //rslt = bmi08a_configure_data_synchronization( sync_cfg, bmi08 );

    return 0;
}




