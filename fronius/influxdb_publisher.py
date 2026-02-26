"""InfluxDB Publisher with batching and change detection"""

import json
import time
import threading
from typing import Dict, Any, Optional

from .config import InfluxDBConfig
from .logging_setup import get_logger

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2  # seconds
RETRY_MAX_DELAY = 60  # seconds
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30  # seconds


class InfluxDBPublisher:
    """
    InfluxDB Publisher for Fronius data.

    Features:
    - Line protocol generation
    - Batched writes
    - Rate limiting per device
    - Publish-on-change mode
    - Automatic reconnection
    """

    def __init__(self, config: InfluxDBConfig, publish_mode: str = 'changed'):
        """
        Initialize InfluxDB publisher.

        Args:
            config: InfluxDB configuration
            publish_mode: 'changed' or 'all'
        """
        self.config = config
        self.publish_mode = publish_mode
        self.client = None
        self.write_api = None
        self.connected = False
        self.last_values: Dict[str, Dict] = {}
        self.last_write_time: Dict[str, float] = {}
        self.lock = threading.Lock()
        self.log = get_logger()

        # Stats
        self.writes_total = 0
        self.writes_failed = 0

        # Reconnection thread control
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            self._setup_client_with_retry()
            # Start background reconnection thread if not connected
            if not self.connected:
                self._start_reconnect_thread()

    def _setup_client(self):
        """Setup InfluxDB client"""
        try:
            from influxdb_client import InfluxDBClient, WriteOptions

            # Clean up old connections first
            if self.write_api:
                try:
                    self.write_api.close()
                except Exception:
                    pass
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass

            self.client = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org
            )

            self.write_api = self.client.write_api(write_options=WriteOptions(
                batch_size=100,
                flush_interval=10_000,
                jitter_interval=2_000,
                retry_interval=5_000,
                max_retries=3
            ))

            # Test connection
            health = self.client.health()
            if health.status == "pass":
                self.connected = True
                self.log.info(f"InfluxDB connected to {self.config.url}")
            else:
                self.log.warning(f"InfluxDB health check failed: {health.message}")

        except ImportError:
            self.log.warning(
                "influxdb-client not installed. "
                "Install with: pip install influxdb-client"
            )
            self.config.enabled = False
        except Exception as e:
            self.log.warning(f"InfluxDB health check failed: {e}")
            self.connected = False

    def _setup_client_with_retry(self):
        """Setup InfluxDB client with retry logic"""
        delay = RETRY_INITIAL_DELAY

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            self._setup_client()

            if self.connected:
                return  # Successfully connected

            if attempt < RETRY_MAX_ATTEMPTS:
                self.log.info(
                    f"InfluxDB connection attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed, "
                    f"retrying in {delay}s..."
                )
                time.sleep(delay)
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

        self.log.warning(
            f"InfluxDB: all {RETRY_MAX_ATTEMPTS} connection attempts failed. "
            "Will continue trying in background."
        )

    def _start_reconnect_thread(self):
        """Start background thread for reconnection attempts"""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return  # Thread already running

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="InfluxDB-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        self.log.info("InfluxDB reconnection thread started")

    def _reconnect_loop(self):
        """Background loop that attempts to reconnect to InfluxDB"""
        while not self._stop_reconnect.is_set():
            if not self.connected:
                self.log.debug("Attempting InfluxDB reconnection...")
                self._setup_client()

                if self.connected:
                    self.log.info("InfluxDB reconnected successfully")
                    break

            # Wait before next attempt
            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def _handle_write_error(self, error: Exception):
        """
        Handle write errors and trigger reconnection if needed.

        Args:
            error: The exception that occurred during write
        """
        # Check if this is a connection-related error
        error_str = str(error).lower()
        connection_errors = [
            'connection refused',
            'connection reset',
            'connection closed',
            'no route to host',
            'network is unreachable',
            'timeout',
            'timed out',
            'broken pipe',
            'connection aborted',
        ]

        is_connection_error = any(err in error_str for err in connection_errors)

        if is_connection_error and self.connected:
            self.log.warning("InfluxDB connection lost, starting reconnection...")
            self.connected = False
            self._start_reconnect_thread()

    def is_enabled(self) -> bool:
        """Check if InfluxDB publishing is enabled and connected"""
        return self.config.enabled and self.connected

    def _should_write(self, key: str, data: Dict) -> bool:
        """
        Check if data should be written based on mode and interval.

        Args:
            key: Unique device key
            data: Data to write

        Returns:
            True if should write
        """
        current_time = time.time()

        # Rate limiting
        if key in self.last_write_time:
            elapsed = current_time - self.last_write_time[key]
            if elapsed < self.config.write_interval:
                return False

        # Change detection
        if self.publish_mode == 'changed':
            with self.lock:
                if key in self.last_values:
                    # Compare numeric fields
                    changed = False
                    for field, value in data.items():
                        if isinstance(value, (int, float)):
                            old_value = self.last_values[key].get(field)
                            if old_value is None or old_value != value:
                                changed = True
                                break

                    if not changed:
                        return False

                # Update cached values
                self.last_values[key] = {
                    k: v for k, v in data.items()
                    if isinstance(v, (int, float))
                }

        self.last_write_time[key] = current_time
        return True

    def write_inverter_data(self, device_id: str, data: Dict):
        """
        Write inverter data to InfluxDB.

        Args:
            device_id: Device identifier
            data: Parsed inverter data
        """
        if not self.is_enabled():
            return

        key = f"inverter_{device_id}"
        if not self._should_write(key, data):
            return

        try:
            from influxdb_client import Point

            point = Point("fronius_inverter") \
                .tag("device_id", device_id) \
                .tag("device_type", "inverter")

            # Add model info as tags
            if data.get('model'):
                point = point.tag("model", data['model'])
            if data.get('serial_number'):
                point = point.tag("serial_number", data['serial_number'])

            # Add status as tag
            if 'status' in data:
                point = point.tag("status", data['status'].get('name', 'UNKNOWN'))

            # Numeric fields
            numeric_fields = [
                'ac_power', 'ac_current', 'ac_current_a', 'ac_current_b', 'ac_current_c',
                'ac_voltage_ab', 'ac_voltage_bc', 'ac_voltage_ca',
                'ac_voltage_an', 'ac_voltage_bn', 'ac_voltage_cn',
                'ac_frequency',
                'dc_power', 'dc_voltage', 'dc_current',
                'lifetime_energy',
                'power_factor', 'apparent_power', 'reactive_power',
                'temp_cabinet', 'temp_heatsink', 'temp_transformer', 'temp_other'
            ]

            for field in numeric_fields:
                if field in data and data[field] is not None:
                    point = point.field(field, float(data[field]))

            # Status code as field
            if 'status' in data:
                point = point.field("status_code", data['status'].get('code', 0))
                point = point.field("status_alarm", data['status'].get('alarm', False))

            # Event count + detailed codes (JSON string for InfluxDB)
            if 'events' in data:
                point = point.field("event_count", len(data['events']))
                if data['events']:
                    # Store compact summary: codes + class per event register
                    evt_summary = []
                    for evt in data['events']:
                        codes = [c['code'] for c in evt.get('codes_decoded', [])]
                        descs = [c['description'] for c in evt.get('codes_decoded', [])]
                        evt_summary.append({
                            'codes': codes,
                            'descriptions': descs,
                            'class': evt.get('class', ''),
                        })
                    point = point.field("events_json", json.dumps(evt_summary))

            # MPPT string data (DC per string)
            if 'mppt' in data and data['mppt']:
                mppt = data['mppt']
                if 'num_modules' in mppt:
                    point = point.field("mppt_num_modules", int(mppt['num_modules']))
                if 'modules' in mppt:
                    for i, module in enumerate(mppt['modules'], 1):
                        if module.get('dc_current') is not None:
                            point = point.field(f"string{i}_current", float(module['dc_current']))
                        if module.get('dc_voltage') is not None:
                            point = point.field(f"string{i}_voltage", float(module['dc_voltage']))
                        if module.get('dc_power') is not None:
                            point = point.field(f"string{i}_power", float(module['dc_power']))
                        if module.get('dc_energy') is not None:
                            point = point.field(f"string{i}_energy", float(module['dc_energy']))

            self.write_api.write(bucket=self.config.bucket, record=point)
            self.writes_total += 1

        except Exception as e:
            self.writes_failed += 1
            self.log.error(f"InfluxDB write error for inverter {device_id}: {e}")
            self._handle_write_error(e)

    def write_meter_data(self, device_id: str, data: Dict):
        """
        Write meter data to InfluxDB.

        Args:
            device_id: Device identifier
            data: Parsed meter data
        """
        if not self.is_enabled():
            return

        key = f"meter_{device_id}"
        if not self._should_write(key, data):
            return

        try:
            from influxdb_client import Point

            point = Point("fronius_meter") \
                .tag("device_id", device_id) \
                .tag("device_type", "meter")

            # Add model info as tags
            if data.get('model'):
                point = point.tag("model", data['model'])
            if data.get('serial_number'):
                point = point.tag("serial_number", data['serial_number'])

            # Numeric fields
            numeric_fields = [
                'power_total', 'power_a', 'power_b', 'power_c',
                'current_total', 'current_a', 'current_b', 'current_c',
                'voltage_ln_avg', 'voltage_an', 'voltage_bn', 'voltage_cn',
                'voltage_ll_avg', 'voltage_ab', 'voltage_bc', 'voltage_ca',
                'frequency',
                'va_total', 'va_a', 'va_b', 'va_c',
                'var_total', 'var_a', 'var_b', 'var_c',
                'pf_avg', 'pf_a', 'pf_b', 'pf_c',
                'energy_exported', 'energy_exported_a', 'energy_exported_b', 'energy_exported_c',
                'energy_imported', 'energy_imported_a', 'energy_imported_b', 'energy_imported_c'
            ]

            for field in numeric_fields:
                if field in data and data[field] is not None:
                    point = point.field(field, float(data[field]))

            self.write_api.write(bucket=self.config.bucket, record=point)
            self.writes_total += 1

        except Exception as e:
            self.writes_failed += 1
            self.log.error(f"InfluxDB write error for meter {device_id}: {e}")
            self._handle_write_error(e)

    def flush(self):
        """Flush pending writes"""
        if self.write_api:
            try:
                self.write_api.flush()
            except Exception as e:
                self.log.error(f"InfluxDB flush error: {e}")

    def close(self):
        """Close InfluxDB connection"""
        # Stop reconnection thread
        self._stop_reconnect.set()
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

        if self.write_api:
            try:
                self.write_api.close()
            except Exception:
                pass

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.connected = False
        self.log.info("InfluxDB connection closed")

    def get_stats(self) -> Dict:
        """Return publisher statistics"""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'url': self.config.url,
            'bucket': self.config.bucket,
            'writes_total': self.writes_total,
            'writes_failed': self.writes_failed,
            'publish_mode': self.publish_mode
        }
