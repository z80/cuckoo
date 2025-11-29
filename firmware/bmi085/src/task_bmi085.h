
#ifndef __TASK_BMI085_H_
#define __TASK_BMI085_H_

#include "main.h"

struct TQuat16
{
	int16_t w;
	int16_t x;
	int16_t y;
	int16_t z;
};

// Data structure to hold all up to date IMU readings.
struct TImuData16
{
	// There are up to 32 sensors.
	// bit=1 means it has been detected during enumeration.
	uint32_t imus_detected;
	// Quaternion data.
	struct TQuat16 quats[32];
};


void task_bmi085_init();

void get_bmi085_data( struct TImuData16 * data );


#endif


