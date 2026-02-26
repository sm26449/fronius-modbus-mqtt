#!/usr/bin/env python3
"""
Docker healthcheck script for Fronius Modbus MQTT

Checks:
1. Health status file exists and is recent
2. Status is 'healthy' or 'sleep' (sleep mode is valid at night)
3. MQTT connection is active (if enabled)
4. Modbus connection is active (except in sleep mode)

Exit codes:
0 = healthy
1 = unhealthy
"""

import os
import sys
import time

HEALTH_FILE = '/tmp/fronius_health'
MAX_AGE_SECONDS = 120  # Normal max age
MAX_AGE_SLEEP = 600    # Allow longer interval in sleep mode (10 min)


def check_health():
    """Check if the service is healthy"""

    # Check if health file exists
    if not os.path.exists(HEALTH_FILE):
        print("Health file not found - service may still be starting")
        return 1

    try:
        with open(HEALTH_FILE, 'r') as f:
            lines = f.readlines()

        if len(lines) < 2:
            print("Invalid health file format")
            return 1

        # Check timestamp (first line)
        timestamp = int(lines[0].strip())
        age = time.time() - timestamp

        # Check status (second line)
        status = lines[1].strip()

        # Parse key:value fields (prefix-based, order-independent)
        fields = {}
        for line in lines[2:]:
            line = line.strip()
            if ':' in line:
                key, _, value = line.partition(':')
                fields[key] = value

        sleep_mode = fields.get('sleep_mode') == 'True'
        is_night = fields.get('night_time') == 'True'

        # Determine max age based on mode
        max_age = MAX_AGE_SLEEP if sleep_mode else MAX_AGE_SECONDS

        if age > max_age:
            print(f"Health file is stale ({int(age)}s old, max {max_age}s)")
            return 1

        # Accept 'healthy' or 'sleep' as valid states
        if status not in ('healthy', 'sleep'):
            print(f"Service status: {status}")
            return 1

        # In sleep mode, don't check Modbus (it's expected to be down)
        if status == 'sleep':
            if fields.get('mqtt') == 'False':
                print("MQTT disconnected during sleep mode")
                return 1

            mode_info = "(night)" if is_night else "(DataManager unavailable)"
            print(f"Sleep mode {mode_info} - last check {int(age)}s ago")
            return 0

        # Check MQTT
        if fields.get('mqtt') == 'False':
            print("MQTT disconnected")
            return 1

        # Check Modbus
        if fields.get('modbus') == 'False':
            print("Modbus disconnected")
            return 1

        print(f"Healthy (last check {int(age)}s ago)")
        return 0

    except Exception as e:
        print(f"Error reading health file: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(check_health())
