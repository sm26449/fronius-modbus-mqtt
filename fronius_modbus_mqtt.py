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

# Health file for Docker healthcheck
HEALTH_FILE = '/tmp/fronius_health'

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
from fronius.modbus_client import PowerLimitCommand


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
        self._start_time = time.time()
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
        self.monitoring_server = None

        # Partial-fleet watchdog state (incident 2026-08-02): count consecutive
        # 30s stat cycles where the collector reads only part of the inverter
        # fleet, and force a full Modbus reconnect once it persists.
        self._partial_cycles = 0

        def _env_int(name: str, default: int, minimum: int) -> int:
            """Parse a positive-int env; fall back (loudly) on garbage so a
            typo can't crash boot or disable the watchdog (review M21)."""
            raw = os.environ.get(name)
            if raw is None:
                return default
            try:
                v = int(raw)
                if v < minimum:
                    raise ValueError(f"< {minimum}")
                return v
            except ValueError as e:
                self.log.warning(
                    f"env {name}={raw!r} invalid ({e}) — using {default}")
                return default

        # 10 cycles × 30s = 5 min of sustained partial before self-heal reconnect.
        self._partial_cycles_for_reconnect = _env_int('PARTIAL_RECONNECT_CYCLES', 10, 1)
        # If still partial 20 min after the reconnect+reconcile didn't heal it,
        # exit(1) so Docker does a full restart — the proven last-resort remedy.
        # Only escalates while at least one inverter is up (DataManager reachable
        # but wedged); a fully-dark fleet is left to the alert (may be legit).
        self._partial_cycles_for_exit = _env_int('PARTIAL_EXIT_CYCLES', 40, 2)

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
        """Callback for polling threads to publish data.

        Exceptions are caught per-publisher so one failing publisher
        doesn't block the other.
        """
        if device_type == 'inverter':
            if self.mqtt_publisher:
                try:
                    self.mqtt_publisher.publish_inverter_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"MQTT publish error for inverter {device_id}: {e}")
            if self.influxdb_publisher:
                try:
                    self.influxdb_publisher.write_inverter_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"InfluxDB write error for inverter {device_id}: {e}")
        elif device_type == 'meter':
            if self.mqtt_publisher:
                try:
                    self.mqtt_publisher.publish_meter_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"MQTT publish error for meter {device_id}: {e}")
            if self.influxdb_publisher:
                try:
                    self.influxdb_publisher.write_meter_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"InfluxDB write error for meter {device_id}: {e}")
        elif device_type == 'storage':
            if self.mqtt_publisher:
                try:
                    self.mqtt_publisher.publish_storage_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"MQTT publish error for storage {device_id}: {e}")
            if self.influxdb_publisher:
                try:
                    self.influxdb_publisher.write_storage_data(str(device_id), data)
                except Exception as e:
                    self.log.error(f"InfluxDB write error for storage {device_id}: {e}")

    def _handle_mqtt_command(self, device_id: str, command: str, payload: dict):
        """Handle incoming MQTT commands (called from MQTT thread)."""
        if not self.config.write or not self.config.write.enabled:
            self.log.warning(f"MQTT command '{command}' rejected — writes disabled")
            if self.mqtt_publisher:
                self.mqtt_publisher.publish_command_result(device_id, command, {
                    'status': 'rejected', 'reason': 'writes disabled'
                })
            return

        if not self.modbus_client or not self.modbus_client.device_poller:
            self.log.warning(f"MQTT command '{command}' rejected — poller not ready")
            if self.mqtt_publisher:
                # L5: publish the rejection (like every sibling branch) so the OV
                # node isn't left waiting silently for a result that never comes.
                self.mqtt_publisher.publish_command_result(device_id, command, {
                    'status': 'rejected', 'reason': 'poller not ready'
                })
            return

        try:
            dev_id = int(device_id)
        except ValueError:
            self.log.warning(f"MQTT command: invalid device_id '{device_id}'")
            return

        if command == 'set_power_limit':
            limit_pct = payload.get('limit_pct')
            if limit_pct is None:
                self.log.warning("MQTT command: missing 'limit_pct' in payload")
                if self.mqtt_publisher:
                    self.mqtt_publisher.publish_command_result(device_id, command, {
                        'status': 'rejected', 'reason': 'missing limit_pct'
                    })
                return
            try:
                limit_pct = float(limit_pct)
            except (TypeError, ValueError):
                self.log.warning(f"MQTT command: invalid limit_pct '{limit_pct}'")
                if self.mqtt_publisher:
                    self.mqtt_publisher.publish_command_result(device_id, command, {
                        'status': 'rejected', 'reason': f'invalid limit_pct: {limit_pct}'
                    })
                return

            try:
                revert_timeout = int(payload.get('revert_timeout', 0))
                ramp_time = int(payload.get('ramp_time', 0))
            except (TypeError, ValueError) as e:
                self.log.warning(f"MQTT command: invalid payload parameter: {e}")
                if self.mqtt_publisher:
                    self.mqtt_publisher.publish_command_result(device_id, command, {
                        'status': 'rejected', 'reason': f'invalid parameter: {e}'
                    })
                return

            cmd = PowerLimitCommand(
                device_id=dev_id,
                limit_pct=limit_pct,
                revert_timeout=revert_timeout,
                ramp_time=ramp_time,
                source=payload.get("source", "mqtt"),
            )
            if not self.modbus_client.device_poller.queue_power_limit_command(cmd):
                if self.mqtt_publisher:
                    self.mqtt_publisher.publish_command_result(device_id, command, {
                        'status': 'rejected', 'reason': 'validation failed or queue full'
                    })

        elif command == 'restore_power_limit':
            cmd = PowerLimitCommand(
                device_id=dev_id,
                limit_pct=100.0,
                source=payload.get("source", "mqtt"),
            )
            if not self.modbus_client.device_poller.queue_power_limit_command(cmd):
                if self.mqtt_publisher:
                    self.mqtt_publisher.publish_command_result(device_id, command, {
                        'status': 'rejected', 'reason': 'validation failed or queue full'
                    })
        else:
            self.log.warning(f"MQTT command: unknown command '{command}'")
            if self.mqtt_publisher:
                self.mqtt_publisher.publish_command_result(device_id, command, {
                    'status': 'rejected', 'reason': f'unknown command: {command}'
                })

    def _publish_command_result(self, device_id: str, command: str, result: dict):
        """Callback from DevicePoller to publish command results via MQTT."""
        if self.mqtt_publisher:
            self.mqtt_publisher.publish_command_result(device_id, command, result)

    def _init_modbus(self) -> bool:
        """Initialize Modbus client and connect with retry logic"""
        self.modbus_client = FroniusModbusClient(
            self.config.modbus,
            self.config.devices,
            self.register_map,
            publish_callback=self._publish_data,
            debug_config=self.config.debug,
            write_config=self.config.write,
            command_result_callback=self._publish_command_result,
        )

        # Retry configuration
        max_attempts = 10
        initial_delay = 2
        max_delay = 60
        delay = initial_delay

        for attempt in range(1, max_attempts + 1):
            if self.modbus_client.connect():
                return True

            if attempt < max_attempts:
                self.log.warning(
                    f"Modbus connection attempt {attempt}/{max_attempts} failed, "
                    f"retrying in {delay}s..."
                )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

        self.log.error(f"Failed to connect to Modbus server after {max_attempts} attempts")
        return False

    def _init_mqtt(self) -> bool:
        """Initialize MQTT publisher"""
        if not self.config.mqtt.enabled:
            self.log.info("MQTT publishing disabled")
            return True

        # Pass command callback only if writes are enabled
        cmd_callback = None
        if self.config.write and self.config.write.enabled:
            cmd_callback = self._handle_mqtt_command

        self.mqtt_publisher = MQTTPublisher(
            self.config.mqtt,
            self.config.general.publish_mode,
            command_callback=cmd_callback,
            write_config=self.config.write,
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

        # Publish Home Assistant discovery configs if enabled
        if self.config.mqtt.ha_discovery_enabled and self.mqtt_publisher:
            self._publish_ha_discovery(inverters, meters)

    def _publish_ha_discovery(self, inverters: list, meters: list):
        """Publish Home Assistant MQTT discovery configs for all discovered devices"""
        self.log.info("Publishing Home Assistant discovery configs...")
        total_configs = 0

        # Publish inverter discovery configs
        for inverter in inverters:
            # device_id must match what's used in MQTT topics (unit_id from Modbus)
            device_id = str(inverter.get('device_id', 'unknown'))
            serial_number = inverter.get('serial_number', '')
            model = inverter.get('model', '')
            manufacturer = inverter.get('manufacturer', 'Fronius')

            # Count MPPT strings if available
            num_mppt = 0
            if 'mppt' in inverter and 'num_modules' in inverter['mppt']:
                num_mppt = inverter['mppt']['num_modules']

            count = self.mqtt_publisher.publish_ha_discovery_inverter(
                device_id, model, manufacturer, num_mppt, serial_number
            )
            total_configs += count

            # Publish runtime discovery for inverter
            count = self.mqtt_publisher.publish_ha_discovery_runtime(
                'inverter', device_id, model, manufacturer, serial_number
            )
            total_configs += count

            # Publish storage discovery if inverter has storage
            if inverter.get('has_storage'):
                count = self.mqtt_publisher.publish_ha_discovery_storage(
                    device_id, model, manufacturer, serial_number
                )
                total_configs += count

        # Publish meter discovery configs
        for meter in meters:
            # device_id must match what's used in MQTT topics (unit_id from Modbus)
            device_id = str(meter.get('device_id', 'unknown'))
            serial_number = meter.get('serial_number', '')
            model = meter.get('model', '')
            manufacturer = meter.get('manufacturer', 'Fronius')

            count = self.mqtt_publisher.publish_ha_discovery_meter(
                device_id, model, manufacturer, serial_number
            )
            total_configs += count

            # Publish runtime discovery for meter
            count = self.mqtt_publisher.publish_ha_discovery_runtime(
                'meter', device_id, model, manufacturer, serial_number
            )
            total_configs += count

        self.log.info(f"Published {total_configs} HA discovery configs")

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
        self.log.info(f"Data validation: {'enabled' if self.config.debug.validate_data else 'disabled'}")
        self.log.info(f"Night inverter skip: {'enabled' if self.config.modbus.night_skip_inverters else 'disabled'}")
        if self.config.write and self.config.write.enabled:
            self.log.warning(
                f"Modbus WRITE enabled — power limit range: "
                f"[{self.config.write.min_power_limit_pct}%, {self.config.write.max_power_limit_pct}%], "
                f"rate limit: {self.config.write.rate_limit_seconds}s, "
                f"auto-revert: {self.config.write.auto_revert_seconds}s"
            )
        else:
            self.log.info("Modbus write: disabled")

        # Clean stale health file from previous run
        self._cleanup_health_file()

        # Initialize publishers FIRST (before modbus, so callback can use them)
        if not self._init_mqtt():
            self.log.error("MQTT initialization failed, exiting")
            self._shutdown()
            sys.exit(1)
        self._init_influxdb()

        # Initialize Modbus (with publish callback)
        if not self._init_modbus():
            self._shutdown()
            sys.exit(1)

        # Discover devices
        self._discover_devices()

        # Log discovered devices
        self.log.info(f"Active: {len(self.modbus_client.inverters)} inverter(s), {len(self.modbus_client.meters)} meter(s)")

        if not self.modbus_client.inverters and not self.modbus_client.meters:
            self.log.error("No devices found, exiting")
            self._shutdown()
            sys.exit(1)

        # Start device polling threads (they publish directly via callback)
        self.modbus_client.start_polling()

        # Start monitoring HTTP server if enabled
        if self.config.monitoring and self.config.monitoring.enabled:
            from fronius.monitoring import MonitoringServer
            self.monitoring_server = MonitoringServer(self, port=self.config.monitoring.port)
            self.monitoring_server.start()

        # Main loop just keeps the app running
        self.running = True
        self._main_loop()

    def _format_uptime(self) -> str:
        """Format container uptime as 'Xd Xh Xm'."""
        elapsed = int(time.time() - self._start_time)
        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        minutes = (elapsed % 3600) // 60

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0 or days > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")

        return " ".join(parts)

    def _publish_runtime_stats(self):
        """Publish runtime statistics for all devices."""
        if not self.mqtt_publisher or not self.mqtt_publisher.connected:
            return

        if not self.modbus_client or not self.modbus_client.device_poller:
            return

        # Get runtime stats from poller
        stats = self.modbus_client.device_poller.get_runtime_stats()
        uptime = self._format_uptime()

        # Publish aggregate status only for device types we're monitoring
        if stats['inverter_total'] > 0:
            self.mqtt_publisher.publish_aggregate_status('inverter', stats['inverter_status'])
        if stats['meter_total'] > 0:
            self.mqtt_publisher.publish_aggregate_status('meter', stats['meter_status'])

        # Publish per-device runtime
        for key, device_data in stats['devices'].items():
            # Parse key to get device_type and device_id (format: "inverter_1" or "meter_240")
            parts = key.split('_', 1)
            if len(parts) == 2:
                device_type, device_id = parts
                self.mqtt_publisher.publish_device_runtime(
                    device_type, device_id, device_data, uptime
                )
            else:
                self.log.debug(f"Unexpected runtime key format: {key}")

    def _partial_fleet_watchdog(self, stats: dict):
        """Self-heal sustained partial inverter reads.

        Compares online against the CONFIGURED fleet size (not the discovered
        one): under-discovery at boot yields inverter_total=1 which would look
        "fully online" against itself — the exact blind spot that let the
        2026-08-02 incident run for hours. Escalates reconnect -> full restart.
        """
        poller = self.modbus_client.device_poller if self.modbus_client else None
        if not poller:
            return
        configured = len(self.config.devices.inverters)
        online = stats.get('inverter_online', 0)
        discovered = stats.get('inverter_total', 0)
        # Partial = fewer inverters reporting than are CONFIGURED (covers both
        # offline devices and boot under-discovery, where discovered<configured).
        is_partial = configured > 1 and online < configured
        if not is_partial:
            self._partial_cycles = 0
            return

        self._partial_cycles += 1
        self.log.warning(
            f"Partial inverter fleet: {online} online / {discovered} discovered "
            f"/ {configured} configured (cycle {self._partial_cycles})"
        )
        # Step 1 — force reconnect + reconcile (re-identify missing inverters).
        if self._partial_cycles == self._partial_cycles_for_reconnect:
            poller.request_reconnect(
                f"{online}/{configured} inverters after "
                f"{self._partial_cycles} cycles"
            )
        # Step 2 — last resort: full process restart via Docker, but only while
        # the DataManager is reachable (>=1 inverter up = wedge, restart-curable).
        # A fully-dark fleet (online==0) is left alone (night/DataManager reboot).
        elif (self._partial_cycles >= self._partial_cycles_for_exit and online > 0):
            self.log.error(
                f"Partial fleet unrecovered after {self._partial_cycles} cycles "
                f"({online}/{configured}) — exit(1) for full Docker restart"
            )
            self._shutdown()
            os._exit(1)

    def _main_loop(self):
        """Main loop - just keeps the app running while threads poll"""
        self.log.info(f"Polling threads started (mode: {self.config.general.publish_mode})")
        self.log.info("Press Ctrl+C to stop")

        health_interval = 30  # Write health file every 30 seconds
        last_health_write = 0

        while self.running:
            try:
                time.sleep(1)

                # Write health file and publish runtime stats periodically
                now = time.time()
                if now - last_health_write >= health_interval:
                    self._write_health_file()
                    # Partial-fleet watchdog runs FIRST and UNCONDITIONALLY —
                    # it must self-heal even when MQTT is down (previously it
                    # lived inside _publish_runtime_stats after an early return
                    # on !mqtt.connected, so the fix was dead exactly when a
                    # broker outage coincided with a fleet wedge — review
                    # M11/M19/M22). It only needs poller stats, not MQTT.
                    if self.modbus_client and self.modbus_client.device_poller:
                        self._partial_fleet_watchdog(
                            self.modbus_client.device_poller.get_runtime_stats())
                    self._publish_runtime_stats()
                    last_health_write = now

            except KeyboardInterrupt:
                break
            except Exception:
                # Health/stats/watchdog are best-effort — a transient exception
                # (e.g. MQTT hiccup mid-publish) must NOT kill the process and
                # leave polling + active OV power-limits orphaned without a
                # clean _shutdown. Log the traceback and keep the loop alive.
                self.log.exception("Main-loop stats cycle failed — continuing")

        self._shutdown()

    def _cleanup_health_file(self):
        """Remove stale health file from previous run."""
        try:
            if os.path.exists(HEALTH_FILE):
                os.remove(HEALTH_FILE)
        except Exception:
            pass

    def _write_health_file(self):
        """Write health status to file for Docker healthcheck"""
        try:
            # Determine health status
            mqtt_connected = self.mqtt_publisher.connected if self.mqtt_publisher else True
            influxdb_connected = self.influxdb_publisher.connected if self.influxdb_publisher else True
            influxdb_enabled = bool(self.influxdb_publisher and self.influxdb_publisher.config.enabled)

            # Get poller status (includes sleep mode info)
            poller_status = {}
            if self.modbus_client and self.modbus_client.device_poller:
                poller_status = self.modbus_client.device_poller.get_status()

            in_sleep_mode = poller_status.get('in_sleep_mode', False)
            modbus_connected = poller_status.get('connected', False)
            is_night = poller_status.get('is_night_time', False)

            # Fleet completeness — the socket being "connected" said nothing
            # about whether we're actually reading the whole fleet (the 2026-08-02
            # incident: modbus:True while reading 1/4, healthcheck said healthy).
            configured_inv = len(self.config.devices.inverters)
            online_inv = 0
            if self.modbus_client and self.modbus_client.device_poller:
                rs = self.modbus_client.device_poller.get_runtime_stats()
                online_inv = rs.get('inverter_online', 0)
            # Partial fleet during daytime is a real degradation (self-heal is
            # working on it) — surface it as 'degraded' rather than a bald
            # 'healthy'. NOT 'unhealthy': restart:unless-stopped ignores health,
            # and the collector self-heals; the alert + escalation own recovery.
            fleet_partial = (not in_sleep_mode and configured_inv > 1
                             and online_inv < configured_inv)

            # Status can be: healthy, sleep, unhealthy
            # Sleep mode is considered healthy (DataManager is just unavailable at night)
            if in_sleep_mode:
                status = 'sleep'
            elif not modbus_connected:
                status = 'unhealthy'
            elif fleet_partial:
                status = 'degraded'
            else:
                status = 'healthy'

            # Disconnection counts
            mqtt_disconnections = self.mqtt_publisher.disconnection_count if self.mqtt_publisher else 0
            influxdb_disconnections = self.influxdb_publisher.disconnection_count if self.influxdb_publisher else 0

            # Atomic write: write to temp file then rename to prevent partial reads
            tmp_file = HEALTH_FILE + '.tmp'
            with open(tmp_file, 'w') as f:
                f.write(f"{int(time.time())}\n")
                f.write(f"{status}\n")
                f.write(f"mqtt:{mqtt_connected}\n")
                f.write(f"influxdb:{influxdb_connected}\n")
                f.write(f"influxdb_enabled:{influxdb_enabled}\n")
                f.write(f"modbus:{modbus_connected}\n")
                f.write(f"inverters_online:{online_inv}\n")
                f.write(f"inverters_configured:{configured_inv}\n")
                f.write(f"sleep_mode:{in_sleep_mode}\n")
                f.write(f"night_time:{is_night}\n")
                f.write(f"uptime:{self._format_uptime()}\n")
                f.write(f"mqtt_disconnections:{mqtt_disconnections}\n")
                f.write(f"influxdb_disconnections:{influxdb_disconnections}\n")
                # Write stats (only if enabled)
                if (self.config.write and self.config.write.enabled
                        and self.modbus_client and self.modbus_client.device_poller):
                    ws = self.modbus_client.device_poller.get_write_stats()
                    f.write(f"writes_total:{ws['writes_total']}\n")
                    f.write(f"writes_failed:{ws['writes_failed']}\n")
            os.replace(tmp_file, HEALTH_FILE)
        except Exception as e:
            self.log.warning(f"Failed to write health file: {e}")

    def _shutdown(self):
        """Clean shutdown"""
        self.log.info("Shutting down...")

        # Publish offline status
        if self.mqtt_publisher and self.mqtt_publisher.connected:
            self.mqtt_publisher.publish_status("offline")
            time.sleep(0.5)  # Allow message to be sent

        # Stop poller loop first (but keep connection alive for restore)
        if self.modbus_client and self.modbus_client.device_poller:
            self.modbus_client.device_poller.stop()
            self.modbus_client.device_poller.join(timeout=10)

        # Restore active power limits to 100% (safe — poller stopped, connection alive)
        if (self.modbus_client and self.modbus_client.device_poller
                and self.config.write and self.config.write.enabled):
            self.modbus_client.device_poller.restore_all_power_limits()

        # Close connections (skip poller stop — already done above)
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
            if self.modbus_client.device_poller:
                ws = self.modbus_client.device_poller.get_write_stats()
                if ws['writes_total'] > 0:
                    self.log.info(
                        f"Write stats: {ws['writes_total']} writes, "
                        f"{ws['writes_failed']} failures"
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

        # Remove health file so Docker healthcheck sees container as stopped
        self._cleanup_health_file()

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
