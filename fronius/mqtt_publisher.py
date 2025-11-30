"""MQTT Publisher with change detection and topic management"""

import time
import json
import threading
from typing import Dict, Any, Optional, Set
import paho.mqtt.client as mqtt

from .config import MQTTConfig
from .logging_setup import get_logger


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
        self.connected = False
        self.last_values: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.log = get_logger()

        # Stats
        self.messages_published = 0
        self.messages_skipped = 0
        self.connection_count = 0

        if config.enabled:
            self._setup_client()

    def _setup_client(self):
        """Setup MQTT client with callbacks"""
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if self.config.username:
            self.client.username_pw_set(
                self.config.username,
                self.config.password
            )

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
        """Handle disconnection"""
        self.connected = False
        if reason_code != 0:
            self.log.warning(f"MQTT disconnected unexpectedly: {reason_code}")

    def connect(self) -> bool:
        """
        Connect to MQTT broker.

        Returns:
            True if connection successful
        """
        if not self.config.enabled:
            self.log.info("MQTT publishing disabled")
            return False

        try:
            self.client.connect(
                self.config.broker,
                self.config.port,
                keepalive=60
            )
            self.client.loop_start()

            # Wait briefly for connection
            for _ in range(10):
                if self.connected:
                    break
                time.sleep(0.1)

            return self.connected

        except Exception as e:
            self.log.error(f"MQTT connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
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

        Args:
            topic: MQTT topic
            payload: String payload
            retain: Override retain setting

        Returns:
            True if published successfully
        """
        if not self.connected:
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

            return False

        except Exception as e:
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

        Args:
            device_id: Device identifier
            data: Parsed inverter data dictionary
        """
        if not self.connected:
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

        Args:
            device_id: Device identifier
            data: Parsed meter data dictionary
        """
        if not self.connected:
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

        Args:
            device_id: Device identifier (inverter serial number)
            data: Parsed storage data dictionary from Model 124
        """
        if not self.connected:
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
        Publish application status.

        Args:
            status: Status string ('online', 'offline', etc.)
        """
        topic = f"{self.config.topic_prefix}/status"
        self.publish(topic, status)

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
            'connection_count': self.connection_count
        }
