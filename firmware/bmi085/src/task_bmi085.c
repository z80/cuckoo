#include "task_bmi085.h"
#include "task_led.h"

#include "cmsis_os.h"
#include "bmi08.h"
#include "bmi08x.h"
#include "bmi08_defs.h"

#include "main.h"

#include "magdwick_imu.h"

#include "bmi085_io.h"

extern I2C_HandleTypeDef hi2c1;
extern I2C_HandleTypeDef hi2c2;

struct TRawImu
{
	// Raw data.
	uint8_t acc[6];
	uint8_t gyro[6];
};

typedef struct TRawImu RawImu;

struct TImu
{
	// 0..31 absolute IMU index defining where it is located.
	uint8_t index;

	struct TMagdwickQuat           quat;
	struct TMagdwickBiasEstimation bias;
};

#define STATE_SET_CHANNEL 1
#define STATE_READ_ACC    2
#define STATE_READ_GYRO   3

struct TAllImus
{
	int imus_qty_a,
	    imus_qty_b;

	int array_index_a,
	    array_index_b;

	uint8_t state_a,
	        state_b;

	// Queue doesn't send structures by value, only by pointer.
	// Due to that keeping all the data in-place here.
	struct TRawImu raw_imu_a[16],
	               raw_imu_b[16];
	struct TMagdwickParams params;
	struct TImu    imus_a[16],
	               imus_b[16];
};

static struct TAllImus all_imus;
static struct TImuData16 imu_data_16;

static void init_all();
static void initiate_data_io();
static void raw_data_to_acc( uint8_t * data, struct TMagdwickImuData * imu );
static void raw_data_to_gyro( uint8_t * data, struct TMagdwickImuData * imu );
static void init_discrete_imu_data();
static void discretize_imu_data();

// Mutex for accessing IMU data.
static osMutexDef_t mutex;
static osMutexId    mutexId;

// Data queue from IRQ to the task.
osMessageQDef(data_queue, 32, uint16_t); // Message queue with 32 slots for uint16_t messages
osMessageQId data_queue_id;

// Declare a task.
static void func_task_bmi085( void * p );
osThreadDef( task_bmi085, func_task_bmi085, osPriorityNormal, 0, 1024 );


// This function starts the task and exists.
// It doesn't do anything else.
void task_bmi085_init()
{
	init_discrete_imu_data();

	mutexId = osMutexCreate( &mutex );
	data_queue_id = osMessageCreate(osMessageQ(data_queue), NULL);
	osThreadCreate( osThread(task_bmi085), NULL );
}

static void init_all()
{
	uint8_t rslt;
	uint8_t index;

	all_imus.imus_qty_a = 0;
	all_imus.imus_qty_b = 0;

	all_imus.state_a = STATE_SET_CHANNEL;
	all_imus.state_b = STATE_SET_CHANNEL;

	magdwick_init_params( &(all_imus.params), 0.1f, 0.01f );

	// Default initialize all IMUs.
	for ( index=0; index<16; index++ )
	{
		struct TImu * imu = &(all_imus.imus_a[index]);
		imu->index = index;
		magdwick_init_quat( &(imu->quat) );
		magdwick_init_bias( &(imu->bias) );

		imu = &(all_imus.imus_b[index]);
		imu->index = index;
		magdwick_init_quat( &(imu->quat) );
		magdwick_init_bias( &(imu->bias) );
	}

	for (index=0; index<16; index++)
	{
		rslt = bmi085_init( index );
		if ( rslt == 0 )
		{
			struct TImu * imu = &(all_imus.imus_a[all_imus.imus_qty_a]);
			imu->index = index;
			all_imus.imus_qty_a += 1;
		}
	}

	for (index=16; index<32; index++)
	{
		rslt = bmi085_init( index );
		if ( rslt == 0 )
		{
			struct TImu * imu = &(all_imus.imus_b[all_imus.imus_qty_b]);
			imu->index = index;
			all_imus.imus_qty_b += 1;
		}
	}

//	rslt = bmi085_init( 28 );
//	osDelay( 1000 );
}

static void initiate_data_io()
{
	if ( all_imus.imus_qty_a > 0 )
	{
		all_imus.array_index_a = 0;
		struct TImu * imu = &(all_imus.imus_a[0]);
		bmi085_switch_irq( imu->index );
	}

	if ( all_imus.imus_qty_b > 0 )
	{
		all_imus.array_index_b = 0;
		struct TImu * imu = &(all_imus.imus_b[0]);
		bmi085_switch_irq( imu->index );
	}
}

static void raw_data_to_acc( uint8_t * data, struct TMagdwickImuData * imu )
{
	// Scale is 8g
	// Conversion coefficient is value * 8.0 / 32768.0

	const float scale = 8.0 / 32768.0;

    uint8_t lsb = data[0];
    uint8_t msb = data[1];
    uint16_t msblsb = (msb << 8) | lsb;
    int16_t val = ((int16_t) msblsb); /* Data in X axis */
    imu->a[0] = (float)val * scale;

    lsb = data[2];
    msb = data[3];
    msblsb = (msb << 8) | lsb;
    val = ((int16_t) msblsb); /* Data in Y axis */
    imu->a[1] = (float)val * scale;

    lsb = data[4];
    msb = data[5];
    msblsb = (msb << 8) | lsb;
    val = ((int16_t) msblsb); /* Data in Z axis */
    imu->a[2] = (float)val * scale;
}

static void raw_data_to_gyro( uint8_t * data, struct TMagdwickImuData * imu )
{
	// Scale is 250deg/s
	// Conversion coefficient is value * (250.0 * 3.1415926535) / (180.0 * 32768.0)
	const float scale = (250.0 * 3.1415926535) / (180.0 * 32768.0);

    uint8_t lsb = data[0];
    uint8_t msb = data[1];
    uint16_t msblsb = (msb << 8) | lsb;
    int16_t val = ((int16_t) msblsb); /* Data in X axis */
    imu->w[0] = (float)val * scale;

    lsb = data[2];
    msb = data[3];
    msblsb = (msb << 8) | lsb;
    val = ((int16_t) msblsb); /* Data in Y axis */
    imu->w[1] = (float)val * scale;

    lsb = data[4];
    msb = data[5];
    msblsb = (msb << 8) | lsb;
    val = ((int16_t) msblsb); /* Data in Z axis */
    imu->w[2] = (float)val * scale;
}


static void init_discrete_imu_data()
{
	uint8_t index;
	for ( index=0; index<32; index++ )
	{
		struct TQuat16 * quat16 = &(imu_data_16.quats[index]);
		quat16->w = 32767;
		quat16->x = 0;
		quat16->y = 0;
		quat16->z = 0;
	}
}

static void discretize_imu_data()
{
	osMutexWait( mutexId, osWaitForever );

		// How many IMUs are detected in total.
		imu_data_16.imus_detected = 0;

		// Discretize IMU data.
		uint8_t index;
		for ( index=0; index<all_imus.imus_qty_a; index++ )
		{
			struct TImu * imu = &(all_imus.imus_a[index]);
			uint8_t imu_index = imu->index;

			imu_data_16.imus_detected = imu_data_16.imus_detected | (1 << imu_index);

			struct TQuat16 * quat16 = &(imu_data_16.quats[imu_index]);

			struct TMagdwickQuat * quat = &(imu->quat);
			float v = quat->q[0] * 32767.0f;
			if (v > 32767.0f)
				quat16->w = 32767;
			else if ( v < -32767.0f )
				quat16->w = -32767;
			else
				quat16->w = (int16_t)v;

			v = quat->q[1] * 32767.0f;
			if (v > 32767.0f)
				quat16->x = 32767;
			else if ( v < -32767.0f )
				quat16->x = -32767;
			else
				quat16->x = (int16_t)v;

			v = quat->q[2] * 32767.0f;
			if (v > 32767.0f)
				quat16->y = 32767;
			else if ( v < -32767.0f )
				quat16->y = -32767;
			else
				quat16->y = (int16_t)v;

			v = quat->q[3] * 32767.0f;
			if (v > 32767.0f)
				quat16->z = 32767;
			else if ( v < -32767.0f )
				quat16->z = -32767;
			else
				quat16->z = (int16_t)v;
		}
		for ( index=0; index<all_imus.imus_qty_b; index++ )
		{
			struct TImu * imu = &(all_imus.imus_b[index]);
			uint8_t imu_index = imu->index;

			imu_data_16.imus_detected = imu_data_16.imus_detected | (1 << imu_index);

			struct TQuat16 * quat16 = &(imu_data_16.quats[imu_index]);

			struct TMagdwickQuat * quat = &(imu->quat);
			float v = quat->q[0] * 32767.0f;
			if (v > 32767.0f)
				quat16->w = 32767;
			else if ( v < -32767.0f )
				quat16->w = -32767;
			else
				quat16->w = (int16_t)v;

			v = quat->q[1] * 32767.0f;
			if (v > 32767.0f)
				quat16->x = 32767;
			else if ( v < -32767.0f )
				quat16->x = -32767;
			else
				quat16->x = (int16_t)v;

			v = quat->q[2] * 32767.0f;
			if (v > 32767.0f)
				quat16->y = 32767;
			else if ( v < -32767.0f )
				quat16->y = -32767;
			else
				quat16->y = (int16_t)v;

			v = quat->q[3] * 32767.0f;
			if (v > 32767.0f)
				quat16->z = 32767;
			else if ( v < -32767.0f )
				quat16->z = -32767;
			else
				quat16->z = (int16_t)v;
		}

	osMutexRelease( mutexId );
}




void HAL_I2C_MasterTxCpltCallback(I2C_HandleTypeDef *hi2c)
{
	//return;

	if ( hi2c == &hi2c1 )
	{
		all_imus.state_a = STATE_READ_ACC;
		uint8_t array_index_a = all_imus.array_index_a;
		struct TImu * imu = &(all_imus.imus_a[array_index_a]);
		uint8_t imu_index = imu->index;

		bmi085_read_acc_irq( imu_index, all_imus.raw_imu_a[array_index_a].acc );
	}
	else
	{
		all_imus.state_b = STATE_READ_ACC;
		uint8_t array_index_b = all_imus.array_index_b;
		struct TImu * imu = &(all_imus.imus_b[array_index_b]);
		uint8_t imu_index = imu->index;

		bmi085_read_acc_irq( imu_index, all_imus.raw_imu_b[array_index_b].acc );
	}
}

// Callback function for memory read complete
void HAL_I2C_MemRxCpltCallback(I2C_HandleTypeDef *hi2c)
{
	//return;

	if ( hi2c == &hi2c1 )
	{
		if ( all_imus.state_a == STATE_READ_ACC )
		{
			// Now read gyro.
			all_imus.state_a = STATE_READ_GYRO;
			uint8_t array_index_a = all_imus.array_index_a;
			struct TImu * imu = &(all_imus.imus_a[array_index_a]);
			uint8_t imu_index = imu->index;

			bmi085_read_gyro_irq( imu_index, all_imus.raw_imu_a[array_index_a].gyro );
		}
		else
		{
			// Send the data to the queue.
			uint8_t array_index_a = all_imus.array_index_a;
			struct TRawImu * raw_imu = &(all_imus.raw_imu_a[array_index_a]);
			uint16_t array_ind = array_index_a;
			uint16_t bus_ind   = 0;
			uint16_t data = (bus_ind << 8) | array_ind;
			osMessagePut(data_queue_id, data, 0);

			// Proceed to the next one or stop.
			all_imus.array_index_a += 1;
			if ( all_imus.array_index_a >= all_imus.imus_qty_a )
				return;

			struct TImu * imu = &(all_imus.imus_a[all_imus.array_index_a]);
			uint8_t imu_index = imu->index;
			all_imus.state_a = STATE_SET_CHANNEL;

			bmi085_switch_irq( imu_index );
		}
	}
	else
	{
		if ( all_imus.state_b == STATE_READ_ACC )
		{
			// Now read gyro.
			all_imus.state_b = STATE_READ_GYRO;
			uint8_t array_index_b = all_imus.array_index_b;
			struct TImu * imu = &(all_imus.imus_b[array_index_b]);
			uint8_t imu_index = imu->index;

			bmi085_read_gyro_irq( imu_index, all_imus.raw_imu_b[array_index_b].gyro );
		}
		else
		{
			// Send the data to the queue.
			uint8_t array_index_b = all_imus.array_index_b;
			struct TRawImu * raw_imu = &(all_imus.raw_imu_b[array_index_b]);
			uint16_t array_ind = array_index_b;
			uint16_t bus_ind   = 1;
			uint16_t data = (bus_ind << 8) | array_ind;
			osMessagePut(data_queue_id, data, 0);

			// Proceed to the next one or stop.
			all_imus.array_index_b += 1;
			if ( all_imus.array_index_b >= all_imus.imus_qty_b )
				return;

			struct TImu * imu = &(all_imus.imus_b[all_imus.array_index_b]);
			uint8_t imu_index = imu->index;
			all_imus.state_b = STATE_SET_CHANNEL;

			bmi085_switch_irq( imu_index );
		}

	}
}

// Error callback function
void HAL_I2C_ErrorCallback(I2C_HandleTypeDef *hi2c)
{
    // Error handling
}








static void func_task_bmi085( void * p )
{
	static uint8_t index;
	static uint8_t total_qty;

	init_all();

	total_qty = all_imus.imus_qty_a + all_imus.imus_qty_b;
	//set_led( ind );

	uint32_t PreviousWakeTime = osKernelSysTick();

	for (;;)
	{
		set_instant_led( 0, 1 );

		// Trigger the chain reaction to read data across all detected IMUs.
		initiate_data_io();

		// Read all the data.
		for ( index=0; index<total_qty; index++ )
		{
			osEvent evt = osMessageGet( data_queue_id, osWaitForever );
			if (evt.status == osEventMessage)
			{
				set_instant_led( 1, 1 );

				uint16_t data = evt.value.v;
				uint16_t bus_ind = (data >> 8);
				uint16_t array_ind = data & 0xFF;

				struct TRawImu * raw_imu = (bus_ind == 0) ? &(all_imus.raw_imu_a[array_ind]) : &(all_imus.raw_imu_b[array_ind]);
				// Convert to signed numbers;
				struct TMagdwickImuData scaled_data;
				raw_data_to_acc( raw_imu->acc, &scaled_data );
				raw_data_to_gyro( raw_imu->gyro, &scaled_data );

				// Run AHRS.
				struct TImu * imu = (bus_ind == 0) ? &(all_imus.imus_a[array_ind]) : &(all_imus.imus_b[array_ind]);
				struct TMagdwickBiasEstimation * bias = &(imu->bias);
				struct TMagdwickQuat * quat = &(imu->quat);
				magdwick_update_bias( &(all_imus.params), bias, &scaled_data );
				magdwick_update_imu( quat, &(all_imus.params), &scaled_data );

				set_instant_led( 1, 0 );
			}
		}

		discretize_imu_data();

		set_instant_led( 0, 0 );

		// Wait so that queries happen on exactly regular basis.
		osDelayUntil( &PreviousWakeTime, 10 );
	}
}




void get_bmi085_data( struct TImuData16 * data )
{
	osMutexWait( mutexId, osWaitForever );
		*data = imu_data_16;
	osMutexRelease( mutexId );
}








