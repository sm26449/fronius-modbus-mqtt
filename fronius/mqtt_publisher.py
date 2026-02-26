"""MQTT Publisher with change detection and topic management"""

import time
import json
import threading
from typing import Dict, Any, Optional, Set, List
import paho.mqtt.client as mqtt

from .config import MQTTConfig
from .logging_setup import get_logger
from . import __version__

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2  # seconds
RETRY_MAX_DELAY = 60  # seconds
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30  # seconds

# Home Assistant Discovery
HA_DISCOVERY_PREFIX = "homeassistant"

# Inverter sensor definitions for HA discovery
# Format: (sunspec_name, ha_name, unit, device_class, state_class, icon)
HA_INVERTER_SENSORS = [
    # AC Power
    ("W", "AC Power", "W", "power", "measurement", None),
    ("VA", "Apparent Power", "VA", "apparent_power", "measurement", None),
    ("VAr", "Reactive Power", "var", "reactive_power", "measurement", None),
    ("PF", "Power Factor", None, "power_factor", "measurement", None),
    ("Hz", "Frequency", "Hz", "frequency", "measurement", None),
    ("WH", "Lifetime Energy", "Wh", "energy", "total_increasing", None),
    # AC Current
    ("A", "AC Current", "A", "current", "measurement", None),
    ("AphA", "AC Current Phase A", "A", "current", "measurement", None),
    ("AphB", "AC Current Phase B", "A", "current", "measurement", None),
    ("AphC", "AC Current Phase C", "A", "current", "measurement", None),
    # AC Voltage
    ("PhVphA", "Voltage Phase A", "V", "voltage", "measurement", None),
    ("PhVphB", "Voltage Phase B", "V", "voltage", "measurement", None),
    ("PhVphC", "Voltage Phase C", "V", "voltage", "measurement", None),
    ("PPVphAB", "Voltage AB", "V", "voltage", "measurement", None),
    ("PPVphBC", "Voltage BC", "V", "voltage", "measurement", None),
    ("PPVphCA", "Voltage CA", "V", "voltage", "measurement", None),
    # DC
    ("DCA", "DC Current", "A", "current", "measurement", None),
    ("DCV", "DC Voltage", "V", "voltage", "measurement", None),
    ("DCW", "DC Power", "W", "power", "measurement", None),
    # Temperatures
    ("TmpCab", "Cabinet Temperature", "°C", "temperature", "measurement", None),
    ("TmpSnk", "Heatsink Temperature", "°C", "temperature", "measurement", None),
    ("TmpTrns", "Transformer Temperature", "°C", "temperature", "measurement", None),
    ("TmpOt", "Other Temperature", "°C", "temperature", "measurement", None),
    # Status
    ("St", "Status Code", None, None, None, "mdi:information-outline"),
    ("status", "Status", None, None, None, "mdi:solar-power"),
]

# Inverter binary sensor definitions for HA discovery
# Format: (sunspec_name, ha_name, device_class, icon)
HA_INVERTER_BINARY_SENSORS = [
    ("active", "Active", "running", "mdi:power"),
]

# Meter sensor definitions for HA discovery
HA_METER_SENSORS = [
    # Power
    ("W", "Power", "W", "power", "measurement", None),
    ("WphA", "Power Phase A", "W", "power", "measurement", None),
    ("WphB", "Power Phase B", "W", "power", "measurement", None),
    ("WphC", "Power Phase C", "W", "power", "measurement", None),
    # Apparent Power
    ("VA", "Apparent Power", "VA", "apparent_power", "measurement", None),
    ("VAphA", "Apparent Power Phase A", "VA", "apparent_power", "measurement", None),
    ("VAphB", "Apparent Power Phase B", "VA", "apparent_power", "measurement", None),
    ("VAphC", "Apparent Power Phase C", "VA", "apparent_power", "measurement", None),
    # Reactive Power
    ("VAR", "Reactive Power", "var", "reactive_power", "measurement", None),
    ("VARphA", "Reactive Power Phase A", "var", "reactive_power", "measurement", None),
    ("VARphB", "Reactive Power Phase B", "var", "reactive_power", "measurement", None),
    ("VARphC", "Reactive Power Phase C", "var", "reactive_power", "measurement", None),
    # Power Factor
    ("PF", "Power Factor", None, "power_factor", "measurement", None),
    ("PFphA", "Power Factor Phase A", None, "power_factor", "measurement", None),
    ("PFphB", "Power Factor Phase B", None, "power_factor", "measurement", None),
    ("PFphC", "Power Factor Phase C", None, "power_factor", "measurement", None),
    # Current
    ("A", "Current", "A", "current", "measurement", None),
    ("AphA", "Current Phase A", "A", "current", "measurement", None),
    ("AphB", "Current Phase B", "A", "current", "measurement", None),
    ("AphC", "Current Phase C", "A", "current", "measurement", None),
    # Voltage LN
    ("PhV", "Voltage LN Average", "V", "voltage", "measurement", None),
    ("PhVphA", "Voltage AN", "V", "voltage", "measurement", None),
    ("PhVphB", "Voltage BN", "V", "voltage", "measurement", None),
    ("PhVphC", "Voltage CN", "V", "voltage", "measurement", None),
    # Voltage LL
    ("PPV", "Voltage LL Average", "V", "voltage", "measurement", None),
    ("PPVphAB", "Voltage AB", "V", "voltage", "measurement", None),
    ("PPVphBC", "Voltage BC", "V", "voltage", "measurement", None),
    ("PPVphCA", "Voltage CA", "V", "voltage", "measurement", None),
    # Frequency
    ("Hz", "Frequency", "Hz", "frequency", "measurement", None),
    # Energy
    ("TotWhExp", "Energy Exported", "Wh", "energy", "total_increasing", None),
    ("TotWhExpPhA", "Energy Exported Phase A", "Wh", "energy", "total_increasing", None),
    ("TotWhExpPhB", "Energy Exported Phase B", "Wh", "energy", "total_increasing", None),
    ("TotWhExpPhC", "Energy Exported Phase C", "Wh", "energy", "total_increasing", None),
    ("TotWhImp", "Energy Imported", "Wh", "energy", "total_increasing", None),
    ("TotWhImpPhA", "Energy Imported Phase A", "Wh", "energy", "total_increasing", None),
    ("TotWhImpPhB", "Energy Imported Phase B", "Wh", "energy", "total_increasing", None),
    ("TotWhImpPhC", "Energy Imported Phase C", "Wh", "energy", "total_increasing", None),
]

# Storage sensor definitions for HA discovery
HA_STORAGE_SENSORS = [
    ("ChaState", "State of Charge", "%", "battery", "measurement", None),
    ("InBatV", "Battery Voltage", "V", "voltage", "measurement", None),
    ("WChaMax", "Max Charge Power", "W", "power", "measurement", None),
    ("status", "Charge Status", None, None, None, "mdi:battery-charging"),
]

# Inverter controls sensor definitions for HA discovery
HA_INVERTER_CONTROLS_SENSORS = [
    ("controls/power_limit_pct", "Power Limit", "%", None, "measurement", "mdi:speedometer"),
    ("controls/power_factor", "Power Factor Setpoint", None, "power_factor", "measurement", None),
]

# Inverter controls binary sensor definitions for HA discovery
# Format: (sunspec_name, ha_name, device_class, icon)
HA_INVERTER_CONTROLS_BINARY_SENSORS = [
    ("controls/connected", "Connected", "connectivity", "mdi:connection"),
    ("controls/power_limit_enabled", "Power Limit Enabled", None, "mdi:toggle-switch"),
    ("controls/power_factor_enabled", "PF Control Enabled", None, "mdi:toggle-switch"),
    ("controls/var_enabled", "VAR Control Enabled", None, "mdi:toggle-switch"),
]

# MPPT string sensor definitions (per string)
HA_MPPT_STRING_SENSORS = [
    ("DCA", "Current", "A", "current", "measurement"),
    ("DCV", "Voltage", "V", "voltage", "measurement"),
    ("DCW", "Power", "W", "power", "measurement"),
    ("DCWH", "Energy", "Wh", "energy", "total_increasing"),
    ("Tmp", "Temperature", "°C", "temperature", "measurement"),
]

# Runtime monitoring sensors (diagnostic category)
# Format: (topic_suffix, ha_name, unit, device_class, state_class, icon)
HA_RUNTIME_SENSORS = [
    ("runtime/status", "Runtime Status", None, None, None, "mdi:heart-pulse"),
    ("runtime/last_seen", "Last Seen", None, "timestamp", None, "mdi:clock-outline"),
    ("runtime/read_errors", "Read Errors", None, None, None, "mdi:alert-circle"),
    ("runtime/uptime", "Uptime", None, None, None, "mdi:timer-outline"),
    ("runtime/model_id", "Model ID", None, None, None, "mdi:identifier"),
]

# Default number of MPPT strings for Fronius inverters
DEFAULT_MPPT_STRINGS = 2


class MQTTPublisher:
    """
    MQTT Publisher for Fronius data.

    Features:
    - Publish-on-change mode
    - Configurable topic structure
    - Automatic reconnection
    - JSON payload formatting
    - Retained messages support
    - SunSpec-compatible topic names
    """

    # Mapping from Python field names to SunSpec register names
    INVERTER_FIELD_MAP = {
        # AC measurements
        'ac_current': 'A',
        'ac_current_a': 'AphA',
        'ac_current_b': 'AphB',
        'ac_current_c': 'AphC',
        'ac_voltage_ab': 'PPVphAB',
        'ac_voltage_bc': 'PPVphBC',
        'ac_voltage_ca': 'PPVphCA',
        'ac_voltage_an': 'PhVphA',
        'ac_voltage_bn': 'PhVphB',
        'ac_voltage_cn': 'PhVphC',
        'ac_power': 'W',
        'ac_frequency': 'Hz',
        'apparent_power': 'VA',
        'reactive_power': 'VAr',
        'power_factor': 'PF',
        'lifetime_energy': 'WH',
        # DC measurements
        'dc_current': 'DCA',
        'dc_voltage': 'DCV',
        'dc_power': 'DCW',
        # Temperatures
        'temp_cabinet': 'TmpCab',
        'temp_heatsink': 'TmpSnk',
        'temp_transformer': 'TmpTrns',
        'temp_other': 'TmpOt',
        # Status
        'status_code': 'St',
        'status_vendor': 'StVnd',
    }

    METER_FIELD_MAP = {
        # Currents
        'current_total': 'A',
        'current_a': 'AphA',
        'current_b': 'AphB',
        'current_c': 'AphC',
        # Voltages LN
        'voltage_ln_avg': 'PhV',
        'voltage_an': 'PhVphA',
        'voltage_bn': 'PhVphB',
        'voltage_cn': 'PhVphC',
        # Voltages LL
        'voltage_ll_avg': 'PPV',
        'voltage_ab': 'PPVphAB',
        'voltage_bc': 'PPVphBC',
        'voltage_ca': 'PPVphCA',
        # Frequency
        'frequency': 'Hz',
        # Power
        'power_total': 'W',
        'power_a': 'WphA',
        'power_b': 'WphB',
        'power_c': 'WphC',
        # Apparent power
        'va_total': 'VA',
        'va_a': 'VAphA',
        'va_b': 'VAphB',
        'va_c': 'VAphC',
        # Reactive power
        'var_total': 'VAR',
        'var_a': 'VARphA',
        'var_b': 'VARphB',
        'var_c': 'VARphC',
        # Power factor
        'pf_avg': 'PF',
        'pf_a': 'PFphA',
        'pf_b': 'PFphB',
        'pf_c': 'PFphC',
        # Energy
        'energy_exported': 'TotWhExp',
        'energy_exported_a': 'TotWhExpPhA',
        'energy_exported_b': 'TotWhExpPhB',
        'energy_exported_c': 'TotWhExpPhC',
        'energy_imported': 'TotWhImp',
        'energy_imported_a': 'TotWhImpPhA',
        'energy_imported_b': 'TotWhImpPhB',
        'energy_imported_c': 'TotWhImpPhC',
    }

    # Storage (Battery) field mapping - Model 124
    STORAGE_FIELD_MAP = {
        # Control/Setpoint registers
        'max_charge_power': 'WChaMax',
        'charge_ramp_rate': 'WChaGra',
        'discharge_ramp_rate': 'WDisChaGra',
        'storage_control_mode': 'StorCtl_Mod',
        'max_charge_va': 'VAChaMax',
        'min_reserve_pct': 'MinRsvPct',
        # Status registers
        'charge_state_pct': 'ChaState',
        'available_storage_ah': 'StorAval',
        'battery_voltage': 'InBatV',
        'charge_status_code': 'ChaSt',
        # Rate setpoints
        'discharge_rate_pct': 'OutWRte',
        'charge_rate_pct': 'InWRte',
        # Timing
        'rate_window_secs': 'InOutWRte_WinTms',
        'rate_revert_secs': 'InOutWRte_RvrtTms',
        'rate_ramp_secs': 'InOutWRte_RmpTms',
        # Grid charging
        'grid_charging_code': 'ChaGriSet',
    }

    def __init__(self, config: MQTTConfig, publish_mode: str = 'changed'):
        """
        Initialize MQTT publisher.

        Args:
            config: MQTT configuration
            publish_mode: 'changed' (only publish changes) or 'all' (always publish)
        """
        self.config = config
        self.publish_mode = publish_mode
        self.client: mqtt.Client = None
        self._connected = threading.Event()
        self.last_values: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.log = get_logger()

        # Stats
        self.messages_published = 0
        self.messages_skipped = 0
        self.connection_count = 0
        self.disconnection_count = 0

        # Reconnection thread control
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None
        self._loop_started = False  # Track if paho network loop is running

        if config.enabled:
            self._setup_client()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @connected.setter
    def connected(self, value: bool):
        if value:
            self._connected.set()
        else:
            if self._connected.is_set():
                self.disconnection_count += 1
            self._connected.clear()

    def _setup_client(self):
        """Setup MQTT client with callbacks"""
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if self.config.username:
            self.client.username_pw_set(
                self.config.username,
                self.config.password
            )

        # TLS configuration (optional)
        if self.config.tls_enabled:
            self.client.tls_set(
                ca_certs=self.config.tls_ca_certs or None,
                certfile=self.config.tls_certfile or None,
                keyfile=self.config.tls_keyfile or None,
            )
            if self.config.tls_insecure:
                self.client.tls_insecure_set(True)
            self.log.info("MQTT TLS enabled")

        # Max outgoing queue size for QoS >= 1 messages.
        # QoS 0 messages are fire-and-forget (not queued when disconnected).
        self.client.max_queued_messages_set(1000)

        # Set Last Will Testament for crash/disconnect detection
        status_topic = f"{self.config.topic_prefix}/status"
        self.client.will_set(status_topic, payload="offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Handle connection established"""
        if reason_code == 0:
            self.connected = True
            self.connection_count += 1
            self.log.info(
                f"MQTT connected to {self.config.broker}:{self.config.port}"
            )
        else:
            self.connected = False
            self.log.error(f"MQTT connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Handle disconnection. Paho auto-reconnects; monitor thread logs state."""
        self.connected = False
        if reason_code != 0:
            self.log.warning(f"MQTT disconnected unexpectedly: {reason_code}")

    def _try_connect(self) -> bool:
        """
        Attempt a single connection to MQTT broker.

        Only call this when paho's network loop is NOT yet running.
        After the first successful connection, paho handles reconnection
        automatically via reconnect_delay_set().

        Returns:
            True if connection successful
        """
        try:
            self.client.connect(
                self.config.broker,
                self.config.port,
                keepalive=60
            )
            self.client.loop_start()
            self._loop_started = True

            # Wait briefly for connection
            for _ in range(10):
                if self.connected:
                    break
                time.sleep(0.1)

            return self.connected

        except Exception as e:
            self.log.warning(f"MQTT connection failed: {e}")
            return False

    def connect(self) -> bool:
        """
        Connect to MQTT broker with retry logic.

        Returns:
            True if connection successful
        """
        if not self.config.enabled:
            self.log.info("MQTT publishing disabled")
            return False

        delay = RETRY_INITIAL_DELAY

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            if self._try_connect():
                # Always start persistent monitor thread
                self._start_reconnect_thread()
                return True

            if attempt < RETRY_MAX_ATTEMPTS:
                self.log.info(
                    f"MQTT connection attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed, "
                    f"retrying in {delay}s..."
                )
                time.sleep(delay)
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

        self.log.warning(
            f"MQTT: all {RETRY_MAX_ATTEMPTS} connection attempts failed. "
            "Will continue trying in background."
        )
        # Always start persistent monitor thread
        self._start_reconnect_thread()
        return False

    def _start_reconnect_thread(self):
        """Start background thread for reconnection attempts"""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return  # Thread already running

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="MQTT-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        self.log.info("MQTT reconnection thread started")

    def _reconnect_loop(self):
        """Persistent background loop that monitors MQTT connection state.

        When paho's network loop is running (_loop_started=True), paho handles
        reconnection automatically via reconnect_delay_set(). This thread only
        monitors and logs state changes.

        When paho's loop was never started (initial connect failed), this thread
        retries _try_connect() to establish the first connection.
        """
        was_connected = self.connected
        while not self._stop_reconnect.is_set():
            if not self.connected:
                if was_connected:
                    # State transition: connected -> disconnected
                    self.log.warning("MQTT connection lost, paho auto-reconnect active")
                    was_connected = False

                if not self._loop_started:
                    # Paho loop never started — need manual connect
                    self.log.debug("Attempting MQTT initial connection...")
                    if self._try_connect():
                        self.log.info("MQTT connected successfully")
                        was_connected = True
            else:
                if not was_connected:
                    # State transition: disconnected -> connected
                    self.log.info("MQTT reconnected successfully")
                    was_connected = True

            # Wait before next check
            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def disconnect(self):
        """Disconnect from MQTT broker"""
        # Stop reconnection thread
        self._stop_reconnect.set()
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False
        self.log.info("MQTT disconnected")

    def _build_topic(self, device_type: str, device_id: str,
                     field: str = None) -> str:
        """
        Build MQTT topic path.

        Args:
            device_type: 'inverter' or 'meter'
            device_id: Device identifier (serial number or ID)
            field: Optional field name

        Returns:
            Topic string like 'fronius/inverter/ABC123/ac_power'
        """
        base = f"{self.config.topic_prefix}/{device_type}/{device_id}"
        if field:
            return f"{base}/{field}"
        return base

    def _should_publish(self, topic: str, value: Any) -> bool:
        """
        Check if value should be published based on mode.

        Args:
            topic: MQTT topic
            value: Value to publish

        Returns:
            True if should publish
        """
        if self.publish_mode == 'all':
            return True

        with self.lock:
            if topic not in self.last_values:
                self.last_values[topic] = value
                return True

            if self.last_values[topic] != value:
                self.last_values[topic] = value
                return True

        return False

    def _publish(self, topic: str, payload: str, retain: bool = None) -> bool:
        """
        Internal publish method.

        With QoS 0 (default), messages are dropped if disconnected.
        With QoS >= 1, paho queues messages internally for delivery on reconnect.

        Args:
            topic: MQTT topic
            payload: String payload
            retain: Override retain setting

        Returns:
            True if published successfully
        """
        if not self.client:
            return False

        if retain is None:
            retain = self.config.retain

        try:
            result = self.client.publish(
                topic,
                payload,
                qos=self.config.qos,
                retain=retain
            )

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.messages_published += 1
                return True
            elif result.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
                self.log.warning("MQTT outgoing queue full, message dropped")
                return False
            elif result.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                # Connection lost but message may be queued
                self.connected = False

            return False

        except Exception as e:
            error_str = str(e).lower()
            if any(err in error_str for err in ['connection', 'socket', 'broken pipe']):
                self.log.warning(f"MQTT connection error during publish: {e}")
                self.connected = False
            else:
                self.log.error(f"MQTT publish error: {e}")
            return False

    def publish(self, topic: str, value: Any, retain: bool = None) -> bool:
        """
        Publish a value to topic.

        Args:
            topic: MQTT topic
            value: Value to publish (will be converted to string/JSON)
            retain: Override retain setting

        Returns:
            True if published successfully
        """
        # Convert to JSON if dict/list
        if isinstance(value, (dict, list)):
            payload = json.dumps(value)
        elif isinstance(value, float):
            payload = str(round(value, 3))
        else:
            payload = str(value)

        return self._publish(topic, payload, retain)

    def publish_if_changed(self, topic: str, value: Any,
                           retain: bool = None) -> bool:
        """
        Publish only if value changed (based on publish_mode).

        Args:
            topic: MQTT topic
            value: Value to publish
            retain: Override retain setting

        Returns:
            True if published, False if skipped or failed
        """
        if self._should_publish(topic, value):
            return self.publish(topic, value, retain)

        self.messages_skipped += 1
        return False

    def publish_inverter_data(self, device_id: str, data: Dict):
        """
        Publish all inverter data fields using SunSpec names.

        With QoS 0, messages are dropped if disconnected (fire-and-forget).

        Args:
            device_id: Device identifier
            data: Parsed inverter data dictionary
        """
        if not self.client:
            return

        device_type = 'inverter'

        # Publish measurement fields with SunSpec names
        for py_field, sunspec_name in self.INVERTER_FIELD_MAP.items():
            if py_field in data and data[py_field] is not None:
                topic = self._build_topic(device_type, device_id, sunspec_name)
                self.publish_if_changed(topic, data[py_field])

        # Status info
        if 'status' in data:
            status = data['status']
            # Status description
            topic = self._build_topic(device_type, device_id, 'status')
            self.publish_if_changed(topic, status.get('description', 'Unknown'))

            # Status code (St)
            topic = self._build_topic(device_type, device_id, 'St')
            self.publish_if_changed(topic, status.get('code', 0))

            # Alarm flag
            topic = self._build_topic(device_type, device_id, 'alarm')
            self.publish_if_changed(topic, status.get('alarm', False))

        # Is active (producing power)
        if 'is_active' in data:
            topic = self._build_topic(device_type, device_id, 'active')
            self.publish_if_changed(topic, data['is_active'])

        # Events (always publish if any exist, don't retain)
        if 'events' in data and data['events']:
            topic = self._build_topic(device_type, device_id, 'events')
            self.publish(topic, data['events'], retain=False)
        elif 'events' in data:
            # Clear events if none active
            topic = self._build_topic(device_type, device_id, 'events')
            self.publish_if_changed(topic, [])

        # Device info fields
        for field in ['model', 'manufacturer', 'serial_number']:
            if field in data and data[field]:
                topic = self._build_topic(device_type, device_id, field)
                self.publish_if_changed(topic, data[field])

        # MPPT string data (DC per string)
        if 'mppt' in data and data['mppt']:
            mppt = data['mppt']
            # Global MPPT info
            if 'num_modules' in mppt:
                topic = self._build_topic(device_type, device_id, 'mppt/num_modules')
                self.publish_if_changed(topic, mppt['num_modules'])

            # Per-module data
            if 'modules' in mppt:
                for i, module in enumerate(mppt['modules'], 1):
                    base = f'mppt/string{i}'
                    if 'dc_current' in module:
                        topic = self._build_topic(device_type, device_id, f'{base}/DCA')
                        self.publish_if_changed(topic, module['dc_current'])
                    if 'dc_voltage' in module:
                        topic = self._build_topic(device_type, device_id, f'{base}/DCV')
                        self.publish_if_changed(topic, module['dc_voltage'])
                    if 'dc_power' in module:
                        topic = self._build_topic(device_type, device_id, f'{base}/DCW')
                        self.publish_if_changed(topic, module['dc_power'])
                    if 'dc_energy' in module:
                        topic = self._build_topic(device_type, device_id, f'{base}/DCWH')
                        self.publish_if_changed(topic, module['dc_energy'])
                    if 'temperature' in module and module['temperature'] is not None:
                        topic = self._build_topic(device_type, device_id, f'{base}/Tmp')
                        self.publish_if_changed(topic, module['temperature'])

        # Controls data (Model 123 - Immediate Controls)
        if 'controls' in data and data['controls']:
            ctrl = data['controls']
            base = 'controls'

            # Connection status
            if 'connected' in ctrl:
                topic = self._build_topic(device_type, device_id, f'{base}/connected')
                self.publish_if_changed(topic, ctrl['connected'])

            # Power limit
            if 'power_limit_pct' in ctrl and ctrl['power_limit_pct'] is not None:
                topic = self._build_topic(device_type, device_id, f'{base}/power_limit_pct')
                self.publish_if_changed(topic, ctrl['power_limit_pct'])
            if 'power_limit_enabled' in ctrl:
                topic = self._build_topic(device_type, device_id, f'{base}/power_limit_enabled')
                self.publish_if_changed(topic, ctrl['power_limit_enabled'])

            # Power factor
            if 'power_factor' in ctrl and ctrl['power_factor'] is not None:
                topic = self._build_topic(device_type, device_id, f'{base}/power_factor')
                self.publish_if_changed(topic, ctrl['power_factor'])
            if 'power_factor_enabled' in ctrl:
                topic = self._build_topic(device_type, device_id, f'{base}/power_factor_enabled')
                self.publish_if_changed(topic, ctrl['power_factor_enabled'])

            # VAR control
            if 'var_enabled' in ctrl:
                topic = self._build_topic(device_type, device_id, f'{base}/var_enabled')
                self.publish_if_changed(topic, ctrl['var_enabled'])

    def publish_meter_data(self, device_id: str, data: Dict):
        """
        Publish all meter data fields using SunSpec names.

        With QoS 0, messages are dropped if disconnected (fire-and-forget).

        Args:
            device_id: Device identifier
            data: Parsed meter data dictionary
        """
        if not self.client:
            return

        device_type = 'meter'

        # Publish measurement fields with SunSpec names
        for py_field, sunspec_name in self.METER_FIELD_MAP.items():
            if py_field in data and data[py_field] is not None:
                topic = self._build_topic(device_type, device_id, sunspec_name)
                self.publish_if_changed(topic, data[py_field])

        # Device info fields
        for field in ['model', 'serial_number']:
            if field in data and data[field]:
                topic = self._build_topic(device_type, device_id, field)
                self.publish_if_changed(topic, data[field])

    def publish_storage_data(self, device_id: str, data: Dict):
        """
        Publish all storage (battery) data fields using SunSpec names.

        With QoS 0, messages are dropped if disconnected (fire-and-forget).

        Args:
            device_id: Device identifier (inverter serial number)
            data: Parsed storage data dictionary from Model 124
        """
        if not self.client:
            return

        device_type = 'storage'

        # Publish measurement fields with SunSpec names
        for py_field, sunspec_name in self.STORAGE_FIELD_MAP.items():
            if py_field in data and data[py_field] is not None:
                topic = self._build_topic(device_type, device_id, sunspec_name)
                self.publish_if_changed(topic, data[py_field])

        # Charge status as human-readable string
        if 'charge_status' in data and data['charge_status']:
            status = data['charge_status']
            topic = self._build_topic(device_type, device_id, 'status')
            self.publish_if_changed(topic, status.get('name', 'UNKNOWN'))

            topic = self._build_topic(device_type, device_id, 'status_description')
            self.publish_if_changed(topic, status.get('description', ''))

        # Grid charging as human-readable string
        if 'grid_charging' in data:
            topic = self._build_topic(device_type, device_id, 'grid_charging')
            self.publish_if_changed(topic, data['grid_charging'])

        # Control mode flags
        if 'charge_limit_active' in data and data['charge_limit_active'] is not None:
            topic = self._build_topic(device_type, device_id, 'charge_limit_active')
            self.publish_if_changed(topic, data['charge_limit_active'])

        if 'discharge_limit_active' in data and data['discharge_limit_active'] is not None:
            topic = self._build_topic(device_type, device_id, 'discharge_limit_active')
            self.publish_if_changed(topic, data['discharge_limit_active'])

    def publish_status(self, status: str):
        """
        Publish application status (retained).

        Args:
            status: Status string ('online', 'offline', etc.)
        """
        topic = f"{self.config.topic_prefix}/status"
        self.publish(topic, status, retain=True)

    def _build_ha_device_info(self, device_type: str, device_id: str,
                              model: str = None, manufacturer: str = "Fronius",
                              serial_number: str = None) -> Dict:
        """
        Build Home Assistant device info block.

        Args:
            device_type: 'inverter', 'meter', or 'storage'
            device_id: Device identifier (Modbus unit ID, used in topics)
            model: Device model name
            manufacturer: Manufacturer name
            serial_number: Device serial number (for display in device name)

        Returns:
            Device info dictionary for HA discovery
        """
        device_name_map = {
            'inverter': 'Inverter',
            'meter': 'Smart Meter',
            'storage': 'Storage'
        }

        # Use device_id (unit_id) for identifier - consistent with MQTT topics
        device_name = f"Fronius {device_name_map.get(device_type, device_type.title())} {device_id}"
        if serial_number:
            device_name = f"Fronius {device_name_map.get(device_type, device_type.title())} {device_id} ({serial_number})"

        device_info = {
            "identifiers": [f"fronius_{device_type}_{device_id}"],
            "name": device_name,
            "manufacturer": manufacturer,
            "sw": f"fronius-modbus-mqtt {__version__}",
        }

        if model:
            device_info["model"] = model

        return device_info

    def _build_ha_origin(self) -> Dict:
        """Build Home Assistant origin block."""
        return {
            "name": "fronius-modbus-mqtt",
            "sw": __version__,
            "url": "https://github.com/sm2669/fronius-modbus-mqtt"
        }

    def _build_ha_sensor_config(self, device_type: str, device_id: str,
                                 sunspec_name: str, ha_name: str,
                                 unit: str = None, device_class: str = None,
                                 state_class: str = None, icon: str = None,
                                 device_info: Dict = None) -> Dict:
        """
        Build Home Assistant sensor discovery config.

        Args:
            device_type: 'inverter', 'meter', or 'storage'
            device_id: Device identifier
            sunspec_name: SunSpec register name (used in state topic)
            ha_name: Human-readable name for HA
            unit: Unit of measurement
            device_class: HA device class
            state_class: HA state class
            icon: MDI icon
            device_info: Pre-built device info dict

        Returns:
            HA discovery config dictionary
        """
        # Build unique ID
        safe_name = sunspec_name.lower().replace("/", "_")
        unique_id = f"fronius_{device_type}_{device_id}_{safe_name}"

        # Build state topic
        state_topic = self._build_topic(device_type, device_id, sunspec_name)

        # Build availability topic
        availability_topic = f"{self.config.topic_prefix}/status"

        config = {
            "name": ha_name,
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "unique_id": unique_id,
            "origin": self._build_ha_origin(),
        }

        if unit:
            config["unit_of_measurement"] = unit
        if device_class:
            config["device_class"] = device_class
        if state_class:
            config["state_class"] = state_class
        if icon:
            config["icon"] = icon
        if device_info:
            config["device"] = device_info

        return config

    def _build_ha_binary_sensor_config(self, device_type: str, device_id: str,
                                        sunspec_name: str, ha_name: str,
                                        device_class: str = None, icon: str = None,
                                        device_info: Dict = None) -> Dict:
        """
        Build Home Assistant binary sensor discovery config.

        Args:
            device_type: 'inverter', 'meter', or 'storage'
            device_id: Device identifier
            sunspec_name: SunSpec register name (used in state topic)
            ha_name: Human-readable name for HA
            device_class: HA device class (connectivity, running, etc.)
            icon: MDI icon
            device_info: Pre-built device info dict

        Returns:
            HA discovery config dictionary
        """
        # Build unique ID
        safe_name = sunspec_name.lower().replace("/", "_")
        unique_id = f"fronius_{device_type}_{device_id}_{safe_name}"

        # Build state topic
        state_topic = self._build_topic(device_type, device_id, sunspec_name)

        # Build availability topic
        availability_topic = f"{self.config.topic_prefix}/status"

        config = {
            "name": ha_name,
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "unique_id": unique_id,
            "origin": self._build_ha_origin(),
            "payload_on": "True",
            "payload_off": "False",
        }

        if device_class:
            config["device_class"] = device_class
        if icon:
            config["icon"] = icon
        if device_info:
            config["device"] = device_info

        return config

    def publish_ha_discovery_inverter(self, device_id: str, model: str = None,
                                       manufacturer: str = "Fronius",
                                       num_mppt_strings: int = 0,
                                       serial_number: str = None) -> int:
        """
        Publish Home Assistant discovery configs for an inverter.

        Args:
            device_id: Device identifier (Modbus unit ID, used in MQTT topics)
            model: Device model name
            manufacturer: Manufacturer name
            num_mppt_strings: Number of MPPT strings to publish (0 = use default)
            serial_number: Device serial number (for unique identifier)

        Returns:
            Number of discovery configs published
        """
        if not self.connected:
            return 0

        device_info = self._build_ha_device_info('inverter', device_id, model, manufacturer, serial_number)
        count = 0

        # Publish main inverter sensors
        for sensor in HA_INVERTER_SENSORS:
            sunspec_name, ha_name, unit, device_class, state_class, icon = sensor

            config = self._build_ha_sensor_config(
                'inverter', device_id, sunspec_name, ha_name,
                unit, device_class, state_class, icon, device_info
            )

            # Build hierarchical discovery topic: homeassistant/sensor/fronius/inverter_1/w/config
            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/inverter_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        # Publish inverter binary sensors
        for sensor in HA_INVERTER_BINARY_SENSORS:
            sunspec_name, ha_name, device_class, icon = sensor

            config = self._build_ha_binary_sensor_config(
                'inverter', device_id, sunspec_name, ha_name,
                device_class, icon, device_info
            )

            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/binary_sensor/fronius/inverter_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        # Publish controls sensors
        for sensor in HA_INVERTER_CONTROLS_SENSORS:
            sunspec_name, ha_name, unit, device_class, state_class, icon = sensor

            config = self._build_ha_sensor_config(
                'inverter', device_id, sunspec_name, ha_name,
                unit, device_class, state_class, icon, device_info
            )

            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/inverter_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        # Publish controls binary sensors
        for sensor in HA_INVERTER_CONTROLS_BINARY_SENSORS:
            sunspec_name, ha_name, device_class, icon = sensor

            config = self._build_ha_binary_sensor_config(
                'inverter', device_id, sunspec_name, ha_name,
                device_class, icon, device_info
            )

            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/binary_sensor/fronius/inverter_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        # Publish MPPT string sensors (use default if not specified)
        mppt_count = num_mppt_strings if num_mppt_strings > 0 else DEFAULT_MPPT_STRINGS
        for string_num in range(1, mppt_count + 1):
            for sensor_suffix, ha_name_suffix, unit, device_class, state_class in HA_MPPT_STRING_SENSORS:
                sunspec_name = f"mppt/string{string_num}/{sensor_suffix}"
                ha_name = f"String {string_num} {ha_name_suffix}"

                config = self._build_ha_sensor_config(
                    'inverter', device_id, sunspec_name, ha_name,
                    unit, device_class, state_class, None, device_info
                )

                safe_name = sunspec_name.lower().replace("/", "_")
                discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/inverter_{device_id}/{safe_name}/config"

                if self._publish(discovery_topic, json.dumps(config), retain=True):
                    count += 1

        self.log.info(f"Published {count} HA discovery configs for inverter {device_id}")
        return count

    def publish_ha_discovery_meter(self, device_id: str, model: str = None,
                                    manufacturer: str = "Fronius",
                                    serial_number: str = None) -> int:
        """
        Publish Home Assistant discovery configs for a meter.

        Args:
            device_id: Device identifier (Modbus unit ID, used in MQTT topics)
            model: Device model name
            manufacturer: Manufacturer name
            serial_number: Device serial number (for unique identifier)

        Returns:
            Number of discovery configs published
        """
        if not self.connected:
            return 0

        device_info = self._build_ha_device_info('meter', device_id, model, manufacturer, serial_number)
        count = 0

        for sensor in HA_METER_SENSORS:
            sunspec_name, ha_name, unit, device_class, state_class, icon = sensor

            config = self._build_ha_sensor_config(
                'meter', device_id, sunspec_name, ha_name,
                unit, device_class, state_class, icon, device_info
            )

            # Build hierarchical discovery topic: homeassistant/sensor/fronius/meter_240/w/config
            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/meter_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        self.log.info(f"Published {count} HA discovery configs for meter {device_id}")
        return count

    def publish_ha_discovery_storage(self, device_id: str, model: str = None,
                                      manufacturer: str = "Fronius",
                                      serial_number: str = None) -> int:
        """
        Publish Home Assistant discovery configs for storage.

        Args:
            device_id: Device identifier (Modbus unit ID, used in MQTT topics)
            model: Device model name
            manufacturer: Manufacturer name
            serial_number: Device serial number (for unique identifier)

        Returns:
            Number of discovery configs published
        """
        if not self.connected:
            return 0

        device_info = self._build_ha_device_info('storage', device_id, model, manufacturer, serial_number)
        count = 0

        for sensor in HA_STORAGE_SENSORS:
            sunspec_name, ha_name, unit, device_class, state_class, icon = sensor

            config = self._build_ha_sensor_config(
                'storage', device_id, sunspec_name, ha_name,
                unit, device_class, state_class, icon, device_info
            )

            # Build hierarchical discovery topic: homeassistant/sensor/fronius/storage_1/chastate/config
            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/storage_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        self.log.info(f"Published {count} HA discovery configs for storage {device_id}")
        return count

    def publish_aggregate_status(self, device_type: str, status: str):
        """
        Publish aggregate status for a device type.

        Args:
            device_type: 'inverter' or 'meter'
            status: 'online', 'partial', or 'offline'
        """
        topic = f"{self.config.topic_prefix}/{device_type}/status"
        self.publish(topic, status, retain=True)

    def publish_device_runtime(self, device_type: str, device_id: str,
                               runtime_data: Dict, uptime: str):
        """
        Publish runtime statistics for a single device.

        Args:
            device_type: 'inverter' or 'meter'
            device_id: Device identifier
            runtime_data: Dict with status, last_seen, read_errors, model_id
            uptime: Container uptime string like "4d 12h 35m"
        """
        if not self.connected:
            return

        base = f"{self.config.topic_prefix}/{device_type}/{device_id}/runtime"

        # Publish runtime fields (retained for HA to get last state on reconnect)
        if 'status' in runtime_data:
            self.publish_if_changed(f"{base}/status", runtime_data['status'], retain=True)
        if 'last_seen' in runtime_data and runtime_data['last_seen']:
            self.publish_if_changed(f"{base}/last_seen", runtime_data['last_seen'], retain=True)
        if 'read_errors' in runtime_data:
            self.publish_if_changed(f"{base}/read_errors", runtime_data['read_errors'], retain=True)
        if 'model_id' in runtime_data and runtime_data['model_id'] is not None:
            self.publish_if_changed(f"{base}/model_id", runtime_data['model_id'], retain=True)

        # Uptime is container-wide, same for all devices
        self.publish_if_changed(f"{base}/uptime", uptime, retain=True)

    def publish_ha_discovery_runtime(self, device_type: str, device_id: str,
                                      model: str = None, manufacturer: str = "Fronius",
                                      serial_number: str = None) -> int:
        """
        Publish Home Assistant discovery configs for runtime sensors.

        Args:
            device_type: 'inverter' or 'meter'
            device_id: Device identifier
            model: Device model name
            manufacturer: Manufacturer name
            serial_number: Device serial number

        Returns:
            Number of discovery configs published
        """
        if not self.connected:
            return 0

        device_info = self._build_ha_device_info(device_type, device_id, model, manufacturer, serial_number)
        count = 0

        for sensor in HA_RUNTIME_SENSORS:
            sunspec_name, ha_name, unit, device_class, state_class, icon = sensor

            config = self._build_ha_sensor_config(
                device_type, device_id, sunspec_name, ha_name,
                unit, device_class, state_class, icon, device_info
            )

            # Mark as diagnostic entity
            config["entity_category"] = "diagnostic"

            # Build discovery topic
            safe_name = sunspec_name.lower().replace("/", "_")
            discovery_topic = f"{HA_DISCOVERY_PREFIX}/sensor/fronius/{device_type}_{device_id}/{safe_name}/config"

            if self._publish(discovery_topic, json.dumps(config), retain=True):
                count += 1

        return count

    def get_stats(self) -> Dict:
        """Return publisher statistics"""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'broker': self.config.broker,
            'port': self.config.port,
            'messages_published': self.messages_published,
            'messages_skipped': self.messages_skipped,
            'publish_mode': self.publish_mode,
            'connection_count': self.connection_count,
            'disconnection_count': self.disconnection_count
        }
