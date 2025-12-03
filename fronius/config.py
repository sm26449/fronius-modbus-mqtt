"""YAML Configuration loader for Fronius Modbus MQTT

Supports configuration via:
1. YAML config file (default)
2. Environment variables (override YAML values)

Environment variable mapping:
  MODBUS_HOST, MODBUS_PORT, MODBUS_TIMEOUT
  INVERTER_IDS (comma-separated), METER_IDS (comma-separated)
  MQTT_ENABLED, MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD
  MQTT_PREFIX, MQTT_RETAIN, MQTT_QOS
  INFLUXDB_ENABLED, INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG
  INFLUXDB_BUCKET, INFLUXDB_WRITE_INTERVAL, INFLUXDB_PUBLISH_MODE
  POLL_INTERVAL, PUBLISH_MODE, LOG_LEVEL
"""

import os
import yaml
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


def _env_get(key: str, default: Any = None, type_cast: type = str) -> Any:
    """Get environment variable with type casting.

    Args:
        key: Environment variable name
        default: Default value if not set
        type_cast: Type to cast the value to (str, int, float, bool, list)

    Returns:
        The environment variable value cast to the specified type, or default
    """
    value = os.environ.get(key)
    if value is None:
        return default

    if type_cast == bool:
        return value.lower() in ('true', '1', 'yes', 'on')
    elif type_cast == int:
        try:
            return int(value)
        except ValueError:
            return default
    elif type_cast == float:
        try:
            return float(value)
        except ValueError:
            return default
    elif type_cast == list:
        # Parse comma-separated list of integers
        try:
            return [int(x.strip()) for x in value.split(',') if x.strip()]
        except ValueError:
            return default
    return value


@dataclass
class ModbusConfig:
    """Modbus TCP connection settings"""
    host: str
    port: int = 502
    timeout: int = 3
    retry_attempts: int = 2
    retry_delay: float = 0.1
    # Night/sleep mode settings
    night_mode_enabled: bool = True          # Enable night mode detection
    night_poll_interval: int = 300           # Poll every 5 min when in night mode
    night_start_hour: int = 21               # Consider night after 21:00
    night_end_hour: int = 6                  # Consider day after 06:00
    ping_check_enabled: bool = True          # Check host availability with ping
    consecutive_failures_for_sleep: int = 3  # Enter sleep mode after N failures


@dataclass
class DevicesConfig:
    """Device configuration - explicit device IDs"""
    inverters: List[int] = field(default_factory=list)  # List of inverter Modbus IDs
    meters: List[int] = field(default_factory=list)      # List of meter Modbus IDs
    meter_poll_interval: float = 2.0    # Meter polling interval in seconds
    inverter_poll_delay: float = 1.0    # Delay between inverter reads in seconds
    inverter_read_delay_ms: int = 200   # Delay between register blocks within same inverter


@dataclass
class MQTTConfig:
    """MQTT broker settings"""
    enabled: bool = True
    broker: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "fronius"
    retain: bool = True
    qos: int = 0


@dataclass
class InfluxDBConfig:
    """InfluxDB settings"""
    enabled: bool = False
    url: str = ""
    token: str = ""
    org: str = ""
    bucket: str = "fronius"
    write_interval: int = 5
    publish_mode: str = ""  # Empty = use general.publish_mode


@dataclass
class GeneralConfig:
    """General application settings"""
    log_level: str = "INFO"
    log_file: str = ""
    poll_interval: int = 5
    publish_mode: str = "changed"  # 'changed' or 'all'


class ConfigLoader:
    """YAML configuration loader with environment variable override support"""

    _instance: Optional['ConfigLoader'] = None

    def __init__(self, config_path: str = None):
        self.config: Dict = {}
        self.general: GeneralConfig = None
        self.modbus: ModbusConfig = None
        self.devices: DevicesConfig = None
        self.mqtt: MQTTConfig = None
        self.influxdb: InfluxDBConfig = None
        self._load_config(config_path)

    @classmethod
    def get_instance(cls, config_path: str = None) -> 'ConfigLoader':
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = ConfigLoader(config_path)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton (useful for testing)"""
        cls._instance = None

    def _load_config(self, config_path: str = None):
        """Load and parse configuration from YAML file or environment variables"""
        # Try to load YAML config file
        paths = [
            config_path,
            os.environ.get('FRONIUS_CONFIG'),
            '/app/config/fronius_modbus_mqtt.yaml',
            'config/fronius_modbus_mqtt.yaml',
            'fronius_modbus_mqtt.yaml'
        ]

        yaml_loaded = False
        for path in filter(None, paths):
            if os.path.exists(path):
                with open(path, 'r') as f:
                    self.config = yaml.safe_load(f) or {}
                yaml_loaded = True
                break

        # If no YAML file found, check if we have enough env vars to run
        if not yaml_loaded:
            if os.environ.get('MODBUS_HOST'):
                self.config = {}  # Empty config, will use env vars
            else:
                raise FileNotFoundError(
                    "No configuration file found and MODBUS_HOST not set.\n"
                    "Either provide a YAML config file or set environment variables.\n"
                    "Searched paths:\n" +
                    "\n".join(f"  - {p}" for p in filter(None, paths))
                )

        self._parse_config()

    def _parse_config(self):
        """Parse configuration into dataclasses with environment variable overrides"""

        # Parse general settings
        gen = self.config.get('general', {})
        self.general = GeneralConfig(
            log_level=_env_get('LOG_LEVEL', gen.get('log_level', 'INFO')),
            log_file=_env_get('LOG_FILE', gen.get('log_file', '')),
            poll_interval=_env_get('POLL_INTERVAL', gen.get('poll_interval', 5), int),
            publish_mode=_env_get('PUBLISH_MODE', gen.get('publish_mode', 'changed'))
        )

        # Parse modbus settings (required - from env or yaml)
        mb = self.config.get('modbus', {})
        modbus_host = _env_get('MODBUS_HOST', mb.get('host'))
        if not modbus_host:
            raise ValueError("modbus.host is required (set MODBUS_HOST env var or modbus.host in YAML)")

        self.modbus = ModbusConfig(
            host=modbus_host,
            port=_env_get('MODBUS_PORT', mb.get('port', 502), int),
            timeout=_env_get('MODBUS_TIMEOUT', mb.get('timeout', 3), int),
            retry_attempts=_env_get('MODBUS_RETRY_ATTEMPTS', mb.get('retry_attempts', 2), int),
            retry_delay=_env_get('MODBUS_RETRY_DELAY', mb.get('retry_delay', 0.1), float),
            # Night mode settings
            night_mode_enabled=_env_get('NIGHT_MODE_ENABLED', mb.get('night_mode_enabled', True), bool),
            night_poll_interval=_env_get('NIGHT_POLL_INTERVAL', mb.get('night_poll_interval', 300), int),
            night_start_hour=_env_get('NIGHT_START_HOUR', mb.get('night_start_hour', 21), int),
            night_end_hour=_env_get('NIGHT_END_HOUR', mb.get('night_end_hour', 6), int),
            ping_check_enabled=_env_get('PING_CHECK_ENABLED', mb.get('ping_check_enabled', True), bool),
            consecutive_failures_for_sleep=_env_get('CONSECUTIVE_FAILURES_FOR_SLEEP', mb.get('consecutive_failures_for_sleep', 3), int)
        )

        # Parse devices settings
        dev = self.config.get('devices', {})

        # Get inverters from env or yaml
        inverters = _env_get('INVERTER_IDS', None, list)
        if inverters is None:
            inverters = dev.get('inverters', [1])
            if isinstance(inverters, int):
                inverters = [inverters]

        # Get meters from env or yaml
        meters = _env_get('METER_IDS', None, list)
        if meters is None:
            meters = dev.get('meters', [240])
            if isinstance(meters, int):
                meters = [meters]

        self.devices = DevicesConfig(
            inverters=inverters,
            meters=meters,
            meter_poll_interval=_env_get('METER_POLL_INTERVAL', dev.get('meter_poll_interval', 2.0), float),
            inverter_poll_delay=_env_get('INVERTER_POLL_DELAY', dev.get('inverter_poll_delay', 1.0), float),
            inverter_read_delay_ms=_env_get('INVERTER_READ_DELAY_MS', dev.get('inverter_read_delay_ms', 200), int)
        )

        # Parse MQTT settings
        mq = self.config.get('mqtt', {})
        self.mqtt = MQTTConfig(
            enabled=_env_get('MQTT_ENABLED', mq.get('enabled', True), bool),
            broker=_env_get('MQTT_BROKER', mq.get('broker', 'localhost')),
            port=_env_get('MQTT_PORT', mq.get('port', 1883), int),
            username=_env_get('MQTT_USERNAME', mq.get('username', '')),
            password=_env_get('MQTT_PASSWORD', mq.get('password', '')),
            topic_prefix=_env_get('MQTT_PREFIX', mq.get('topic_prefix', 'fronius')),
            retain=_env_get('MQTT_RETAIN', mq.get('retain', True), bool),
            qos=_env_get('MQTT_QOS', mq.get('qos', 0), int)
        )

        # Parse InfluxDB settings
        idb = self.config.get('influxdb', {})
        self.influxdb = InfluxDBConfig(
            enabled=_env_get('INFLUXDB_ENABLED', idb.get('enabled', False), bool),
            url=_env_get('INFLUXDB_URL', idb.get('url', '')),
            token=_env_get('INFLUXDB_TOKEN', idb.get('token', '')),
            org=_env_get('INFLUXDB_ORG', idb.get('org', '')),
            bucket=_env_get('INFLUXDB_BUCKET', idb.get('bucket', 'fronius')),
            write_interval=_env_get('INFLUXDB_WRITE_INTERVAL', idb.get('write_interval', 5), int),
            publish_mode=_env_get('INFLUXDB_PUBLISH_MODE', idb.get('publish_mode', ''))
        )


def get_config(config_path: str = None) -> ConfigLoader:
    """Get configuration singleton"""
    return ConfigLoader.get_instance(config_path)
