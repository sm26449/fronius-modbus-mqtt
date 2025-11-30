#!/usr/bin/env python3
"""
Fronius Modbus MQTT - Modbus TCP to MQTT/InfluxDB Bridge

Reads data from Fronius inverters and smart meters via Modbus TCP
and publishes to MQTT and/or InfluxDB.

Features:
- Autodiscovery of Fronius devices
- SunSpec protocol support with scale factors
- Event flag and status code parsing
- Publish-on-change or publish-all modes
- Device caching for optimized startup
"""

import sys
import os
import time
import signal
import json
import argparse
import atexit
from pathlib import Path

from fronius import (
    __version__,
    setup_logging,
    get_logger,
    get_config,
    RegisterParser,
    FroniusModbusClient,
    MQTTPublisher,
    InfluxDBPublisher,
)


class FroniusModbusMQTT:
    """Main application class"""

    def __init__(self, config_path: str = None, device_filter: str = 'all'):
        """
        Initialize application.

        Args:
            config_path: Optional path to configuration file
            device_filter: 'all', 'inverter', or 'meter' - which devices to poll
        """
        self.running = False
        self.device_filter = device_filter
        self.config = get_config(config_path)

        # Determine log file path - use device-specific log if filter is set
        log_file = self.config.general.log_file
        if log_file and device_filter != 'all':
            # Replace filename with device-specific name
            # e.g., /app/logs/fronius.log -> /app/logs/inverter.log
            log_path = Path(log_file)
            log_file = str(log_path.parent / f"{device_filter}.log")

        # Setup logging
        self.log = setup_logging(
            log_level=self.config.general.log_level,
            log_file=log_file
        )

        # Load register map
        self.register_map = self._load_register_map()

        # Initialize components
        self.modbus_client = None
        self.mqtt_publisher = None
        self.influxdb_publisher = None

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_register_map(self) -> dict:
        """Load register map from JSON file"""
        register_paths = [
            Path(__file__).parent / 'config' / 'registers.json',
            Path('config/registers.json'),
            Path('/app/config/registers.json')
        ]

        for path in register_paths:
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        self.log.debug(f"Loaded register map from {path}")
                        return json.load(f)
                except Exception as e:
                    self.log.warning(f"Error loading register map from {path}: {e}")

        self.log.error("Could not find registers.json")
        sys.exit(1)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.log.info("Shutdown signal received")
        self.running = False

    def _publish_data(self, device_id: int, device_type: str, data: dict):
        """Callback for polling threads to publish data"""
        if device_type == 'inverter':
            if self.mqtt_publisher:
                self.mqtt_publisher.publish_inverter_data(str(device_id), data)
            if self.influxdb_publisher:
                self.influxdb_publisher.write_inverter_data(str(device_id), data)
        elif device_type == 'meter':
            if self.mqtt_publisher:
                self.mqtt_publisher.publish_meter_data(str(device_id), data)
            if self.influxdb_publisher:
                self.influxdb_publisher.write_meter_data(str(device_id), data)
        elif device_type == 'storage':
            if self.mqtt_publisher:
                self.mqtt_publisher.publish_storage_data(str(device_id), data)
            # InfluxDB storage support can be added later if needed

    def _init_modbus(self) -> bool:
        """Initialize Modbus client and connect"""
        self.modbus_client = FroniusModbusClient(
            self.config.modbus,
            self.config.devices,
            self.register_map,
            publish_callback=self._publish_data
        )

        if not self.modbus_client.connect():
            self.log.error("Failed to connect to Modbus server")
            return False

        return True

    def _init_mqtt(self) -> bool:
        """Initialize MQTT publisher"""
        if not self.config.mqtt.enabled:
            self.log.info("MQTT publishing disabled")
            return True

        self.mqtt_publisher = MQTTPublisher(
            self.config.mqtt,
            self.config.general.publish_mode
        )

        if not self.mqtt_publisher.connect():
            self.log.warning("Failed to connect to MQTT broker")
            return False

        # Publish online status
        self.mqtt_publisher.publish_status("online")
        return True

    def _init_influxdb(self) -> bool:
        """Initialize InfluxDB publisher"""
        if not self.config.influxdb.enabled:
            self.log.info("InfluxDB publishing disabled")
            return True

        # Use InfluxDB-specific publish_mode if set, else use general
        publish_mode = self.config.influxdb.publish_mode or self.config.general.publish_mode

        self.influxdb_publisher = InfluxDBPublisher(
            self.config.influxdb,
            publish_mode
        )

        return self.influxdb_publisher.is_enabled()

    def _discover_devices(self):
        """Discover devices at configured IDs based on device_filter"""
        filter_msg = f" (filter: {self.device_filter})" if self.device_filter != 'all' else ""
        self.log.info(f"Discovering devices...{filter_msg}")
        inverters, meters = self.modbus_client.discover_devices(self.device_filter)

        if not inverters and not meters:
            self.log.warning("No devices found!")

    def start(self):
        """Start the application"""
        self.log.info("=" * 60)
        self.log.info(f"Fronius Modbus MQTT v{__version__}")
        self.log.info("=" * 60)

        # Log device configuration
        self.log.info(f"Configured inverters: {self.config.devices.inverters}")
        self.log.info(f"Configured meters: {self.config.devices.meters}")
        self.log.info(f"Meter poll interval: {self.config.devices.meter_poll_interval}s")
        self.log.info(f"Inverter poll delay: {self.config.devices.inverter_poll_delay}s between each")

        # Initialize publishers FIRST (before modbus, so callback can use them)
        self._init_mqtt()
        self._init_influxdb()

        # Initialize Modbus (with publish callback)
        if not self._init_modbus():
            sys.exit(1)

        # Discover devices
        self._discover_devices()

        # Log discovered devices
        self.log.info(f"Active: {len(self.modbus_client.inverters)} inverter(s), {len(self.modbus_client.meters)} meter(s)")

        if not self.modbus_client.inverters and not self.modbus_client.meters:
            self.log.error("No devices found, exiting")
            sys.exit(1)

        # Start device polling threads (they publish directly via callback)
        self.modbus_client.start_polling()

        # Main loop just keeps the app running
        self.running = True
        self._main_loop()

    def _main_loop(self):
        """Main loop - just keeps the app running while threads poll"""
        self.log.info(f"Polling threads started (mode: {self.config.general.publish_mode})")
        self.log.info("Press Ctrl+C to stop")

        while self.running:
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                break

        self._shutdown()

    def _shutdown(self):
        """Clean shutdown"""
        self.log.info("Shutting down...")

        # Publish offline status
        if self.mqtt_publisher and self.mqtt_publisher.connected:
            self.mqtt_publisher.publish_status("offline")
            time.sleep(0.5)  # Allow message to be sent

        # Close connections
        if self.modbus_client:
            self.modbus_client.disconnect()

        if self.mqtt_publisher:
            self.mqtt_publisher.disconnect()

        if self.influxdb_publisher:
            self.influxdb_publisher.flush()
            self.influxdb_publisher.close()

        # Log stats
        if self.modbus_client:
            stats = self.modbus_client.get_stats()
            self.log.info(
                f"Modbus stats: {stats['successful_reads']} reads, "
                f"{stats['failed_reads']} failures"
            )

        if self.mqtt_publisher:
            stats = self.mqtt_publisher.get_stats()
            self.log.info(
                f"MQTT stats: {stats['messages_published']} published, "
                f"{stats['messages_skipped']} skipped"
            )

        if self.influxdb_publisher:
            stats = self.influxdb_publisher.get_stats()
            self.log.info(
                f"InfluxDB stats: {stats['writes_total']} writes, "
                f"{stats['writes_failed']} failures"
            )

        self.log.info("Shutdown complete")


def check_single_instance() -> bool:
    """
    Check if another instance is already running using a PID file.

    Returns:
        True if this is the only instance, False if another instance is running.
    """
    pid_file = Path(__file__).parent / 'data' / 'fronius_modbus_mqtt.pid'
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            with open(pid_file, 'r') as f:
                old_pid = int(f.read().strip())

            # Check if process with this PID is still running
            try:
                os.kill(old_pid, 0)  # Signal 0 just checks if process exists
                # Process exists, check if it's actually our script
                # On macOS/Linux, we can verify the process name
                import subprocess
                result = subprocess.run(
                    ['ps', '-p', str(old_pid), '-o', 'command='],
                    capture_output=True, text=True
                )
                if 'fronius_modbus_mqtt' in result.stdout:
                    return False  # Another instance is running
                # PID exists but it's a different process, stale PID file
            except ProcessLookupError:
                pass  # Process doesn't exist, stale PID file
            except PermissionError:
                return False  # Can't check, assume it's running
        except (ValueError, FileNotFoundError):
            pass  # Invalid or missing PID file

    # Write our PID
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    # Register cleanup
    def cleanup_pid():
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass

    atexit.register(cleanup_pid)
    return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Fronius Modbus MQTT - Read Fronius inverters via Modbus TCP"
    )
    parser.add_argument(
        '-c', '--config',
        help='Path to configuration file',
        default=None
    )
    parser.add_argument(
        '-v', '--version',
        action='version',
        version=f'%(prog)s {__version__}'
    )
    parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force start even if another instance is running'
    )
    parser.add_argument(
        '-d', '--device',
        choices=['all', 'inverter', 'meter'],
        default='all',
        help='Device type to poll: all (default), inverter, or meter'
    )
    args = parser.parse_args()

    # Check for existing instance
    if not args.force and not check_single_instance():
        print("ERROR: Another instance of fronius_modbus_mqtt is already running!")
        print("Use --force to override this check (not recommended).")
        sys.exit(1)

    # Start application
    app = FroniusModbusMQTT(args.config, device_filter=args.device)
    app.start()


if __name__ == "__main__":
    main()
