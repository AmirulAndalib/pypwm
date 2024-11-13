#!/usr/bin/python3

import RPi.GPIO as GPIO
import time
import subprocess
import logging
import psutil
import atexit
import signal
import sys

# GPIO setup
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(14, GPIO.OUT)
pwm = GPIO.PWM(14, 100)

# Logging configuration
# Set up logging to syslog
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("\nPress Ctrl+C to quit \n")

# Initializations
dc = 0
current_dc = 0
pwm.start(dc)

# Temperature thresholds and hysteresis
temp_threshold_high = 45
temp_threshold_medium = 40
temp_threshold_low_1 = 35
temp_threshold_low_2 = 30
hysteresis = 2

# Function to clean up GPIO and turn off the fan
def cleanup(signum=None, frame=None):
    try:
        pwm.ChangeDutyCycle(0)  # Set fan speed to 0
        time.sleep(1.0)  # Wait for the fan to stop
        pwm.stop()
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(14, GPIO.OUT)
        GPIO.output(14, GPIO.LOW)
        GPIO.cleanup()
        logging.info("Program terminated -- Cleaning up GPIO")
        logger.info("Fan turned off. Exiting program.")
    except Exception as e:
        logger.info(f"An error occurred during cleanup: {str(e)}")
    sys.exit(0)

# Register the cleanup function to be called on exit, SIGINT, and SIGTERM
atexit.register(cleanup)
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# Dry run at 0% speed for 30 seconds
logger.info("FAN IS RUNNING AT 0% speed for 30 seconds.. (DRY RUN 1)")
start_time = time.time()
while time.time() - start_time < 30.0:
    pwm.ChangeDutyCycle(0)
    time.sleep(1.0)

# Fan speed ramp-up from 0 to 100 over 100 seconds
logger.info("FAN SPEED IS INCREASING FROM 0 TO 100% over 100 seconds.. (DRY RUN 2)")
start_time = time.time()
while time.time() - start_time < 100.0:
    dc += 1
    pwm.ChangeDutyCycle(dc)
    time.sleep(1.0)

# Full-speed run at 100% for 60 seconds
logger.info("FAN IS RUNNING AT 100% speed for 60 seconds.. (DRY RUN 3)")
start_time = time.time()
while time.time() - start_time < 60.0:
    pwm.ChangeDutyCycle(100)
    time.sleep(1.0)

try:
    while True:
        # Get CPU temperature
        temp = subprocess.getoutput("vcgencmd measure_temp|sed 's/[^0-9.]//g'")

        # Temperature control logic with hysteresis
        if round(float(temp)) >= temp_threshold_high + hysteresis:
            dc = 100
        elif round(float(temp)) >= temp_threshold_medium + hysteresis:
            dc = 85
        elif round(float(temp)) >= temp_threshold_low_1 + hysteresis:
            dc = 75
        elif round(float(temp)) >= temp_threshold_low_2 + hysteresis:
            dc = 60
        else:
            dc = 40

        # Dynamic Fan Control based on system load
        system_load = psutil.cpu_percent()
        if system_load > 80:
            dc = 100
        elif system_load > 60:
            dc = 85
        elif system_load > 40:
            dc = 75
        else:
            dc = 40

        # Fan speed ramp-up
        log_message = f"FAN IS RUNNING AT {dc}% speed, TEMPERATURE IS {float(temp)}°C"
        logger.info(log_message)
        sys.stdout.write(log_message + '\n')
        sys.stdout.flush()

        for current_dc in range(current_dc, dc + 1, 5):
            pwm.ChangeDutyCycle(current_dc)
            time.sleep(1.0)

        # Log temperature, fan duty cycle, system load
        log_entry = (
            f"CPU Temp is: {float(temp)}°C, Fan speed is: {dc}%, Load is: {system_load}%"
        )
        logging.info(log_entry)

        # Fan Failure Detection
        if dc == 0:
            logger.info("Fan failure detected! Check the fan.")
            # Take corrective action, e.g., turn off the system or send an alert

        # Sleep for a specific duration
        time.sleep(60.0)

except KeyboardInterrupt:
    # Fan speed ramp-down
    for current_dc in range(current_dc, 0, -5):
        pwm.ChangeDutyCycle(current_dc)
        time.sleep(1.0)

    cleanup()  # Clean up GPIO on keyboard interrupt

    # Log and logger.info exit message
    logging.info("Ctrl + C pressed -- Ending program")
    logger.info("Ctrl + C pressed -- Ending program")
except Exception as e:
    logger.info(f"An error occurred: {str(e)}")
    cleanup()  # Clean up GPIO on error
