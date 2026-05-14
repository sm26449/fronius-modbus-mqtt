"""YAML Configuration loader for Fronius Modbus MQTT

Supports configuration via:
1. YAML config file (default)
2. Environment variables (override YAML values)

Environment variable mapping:
  MODBUS_HOST, MODBUS_PORT, MODBUS_TIMEOUT
  INVERTER_IDS (comma-separated), METER_IDS (comma-separated)
  MQTT_ENABLED, MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD
  MQTT_PREFIX, MQTT_RETAIN, MQTT_QOS, HA_DISCOVERY_ENABLED
  MQTT_TLS_ENABLED, MQTT_TLS_CA_CERTS, MQTT_TLS_CERTFILE, MQTT_TLS_KEYFILE, MQTT_TLS_INSECURE
  INFLUXDB_ENABLED, INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG
  INFLUXDB_BUCKET, INFLUXDB_WRITE_INTERVAL, INFLUXDB_PUBLISH_MODE
  INFLUXDB_VERIFY_SSL, INFLUXDB_SSL_CA_CERT
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


class ConfigValidationError(ValueError):
    """Raised when configuration values are invalid"""
    pass


def _validate_range(value, name: str, min_val=None, max_val=None):
    """Validate a numeric value is within range"""
    if min_val is not None and value < min_val:
        raise ConfigValidationError(f"{name}={value} must be >= {min_val}")
    if max_val is not None and value > max_val:
        raise ConfigValidationError(f"{name}={value} must be <= {max_val}")


@dataclass
class WriteConfig:
    """Modbus write control settings (disabled by default)."""
    enabled: bool = False
    min_power_limit_pct: float = 10.0     # Safety floor: never below 10%
    max_power_limit_pct: float = 100.0    # Safety ceiling
    rate_limit_seconds: int = 30          # Min interval between writes per device
    auto_revert_seconds: int = 3600       # Auto-restore 100% after 1 hour (0=disabled)
    stabilization_delay: float = 2.0      # Seconds to wait after write before next read
    command_topic_suffix: str = "cmd"     # MQTT topic suffix for commands
    # Shared command queue size across all inverters. At rate_limit_seconds=30s
    # × N inverters with bursty automatic controllers (e.g. OV protection
    # firing on voltage transitions), 10 fills up quickly at peak. 50 gives
    # ~4 minutes of headroom before backpressure rejection kicks in.
    command_queue_size: int = 50

    def __post_init__(self):
        _validate_range(self.min_power_limit_pct, "write.min_power_limit_pct", 0, 100)
        _validate_range(self.max_power_limit_pct, "write.max_power_limit_pct", 0, 100)
        _validate_range(self.rate_limit_seconds, "write.rate_limit_seconds", 5, 3600)
        _validate_range(self.auto_revert_seconds, "write.auto_revert_seconds", 0, 86400)
        _validate_range(self.stabilization_delay, "write.stabilization_delay", 0.5, 30)
        _validate_range(self.command_queue_size, "write.command_queue_size", 1, 1000)
        if self.min_power_limit_pct > self.max_power_limit_pct:
            raise ConfigValidationError(
                f"write.min_power_limit_pct ({self.min_power_limit_pct}) "
                f"must be <= write.max_power_limit_pct ({self.max_power_limit_pct})"
            )


@dataclass
class MonitoringConfig:
    """Built-in HTTP monitoring server settings."""
    enabled: bool = False
    port: int = 8080

    def __post_init__(self):
        _validate_range(self.port, "monitoring.port", 1, 65535)


@dataclass
class DebugConfig:
    """Diagnostic and data validation settings for buffer corruption detection."""
    validate_data: bool = True             # Enable buffer corruption detection + reconciliation
    log_register_values: bool = False      # Log raw register hex values per read
    log_scale_factors: bool = False        # Log scale factor calculations
    log_reconciliation: bool = True        # Log corruption detection + reconciliation (WARNING level)
    log_publish_data: bool = False         # Log full data dict before publish
    log_status_transitions: bool = True    # Log inverter status changes (WARNING level)


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
    night_skip_inverters: bool = True        # Skip inverter polling at night (meters still polled)

    def __post_init__(self):
        _validate_range(self.port, "modbus.port", 1, 65535)
        _validate_range(self.timeout, "modbus.timeout", 1, 60)
        _validate_range(self.retry_attempts, "modbus.retry_attempts", 0, 20)
        _validate_range(self.retry_delay, "modbus.retry_delay", 0, 30)
        _validate_range(self.night_poll_interval, "modbus.night_poll_interval", 10, 3600)
        _validate_range(self.night_start_hour, "modbus.night_start_hour", 0, 23)
        _validate_range(self.night_end_hour, "modbus.night_end_hour", 0, 23)
        _validate_range(self.consecutive_failures_for_sleep, "modbus.consecutive_failures_for_sleep", 1, 100)


@dataclass
class DevicesConfig:
    """Device configuration - explicit device IDs"""
    inverters: List[int] = field(default_factory=list)  # List of inverter Modbus IDs
    meters: List[int] = field(default_factory=list)      # List of meter Modbus IDs
    meter_poll_interval: float = 2.0    # Meter polling interval in seconds
    inverter_poll_delay: float = 1.0    # Delay between inverter reads in seconds
    inverter_read_delay_ms: int = 200   # Delay between register blocks within same inverter

    def __post_init__(self):
        _validate_range(self.meter_poll_interval, "devices.meter_poll_interval", 0.5, 300)
        _validate_range(self.inverter_poll_delay, "devices.inverter_poll_delay", 0.1, 60)
        _validate_range(self.inverter_read_delay_ms, "devices.inverter_read_delay_ms", 50, 5000)


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
    ha_discovery_enabled: bool = False  # Home Assistant MQTT autodiscovery
    # TLS settings (optional)
    tls_enabled: bool = False
    tls_ca_certs: str = ""      # Path to CA certificate file
    tls_certfile: str = ""      # Path to client certificate file
    tls_keyfile: str = ""       # Path to client private key file
    tls_insecure: bool = False  # Skip hostname verification (for IP-based connections)

    def __post_init__(self):
        _validate_range(self.port, "mqtt.port", 1, 65535)
        if self.qos not in (0, 1, 2):
            raise ConfigValidationError(f"mqtt.qos={self.qos} must be 0, 1, or 2")

    def __repr__(self) -> str:
        masked_pw = "***" if self.password else ""
        return (
            f"MQTTConfig(broker={self.broker!r}, port={self.port}, "
            f"username={self.username!r}, password={masked_pw!r}, "
            f"tls_enabled={self.tls_enabled})"
        )


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
    # TLS settings (optional)
    verify_ssl: bool = True    # Verify SSL certificates
    ssl_ca_cert: str = ""      # Path to CA certificate file

    def __post_init__(self):
        _validate_range(self.write_interval, "influxdb.write_interval", 1, 3600)
        if self.publish_mode and self.publish_mode not in ('changed', 'all'):
            raise ConfigValidationError(
                f"influxdb.publish_mode={self.publish_mode!r} must be 'changed', 'all', or empty"
            )
        # Cross-field validation: required fields when enabled
        if self.enabled:
            if not self.url:
                raise ConfigValidationError("influxdb.url is required when InfluxDB is enabled")
            if not self.token:
                raise ConfigValidationError("influxdb.token is required when InfluxDB is enabled")
            if not self.org:
                raise ConfigValidationError("influxdb.org is required when InfluxDB is enabled")

    def __repr__(self) -> str:
        masked_token = "***" if self.token else ""
        return (
            f"InfluxDBConfig(url={self.url!r}, org={self.org!r}, "
            f"bucket={self.bucket!r}, token={masked_token!r}, "
            f"verify_ssl={self.verify_ssl})"
        )


@dataclass
class GeneralConfig:
    """General application settings"""
    log_level: str = "INFO"
    log_file: str = ""
    poll_interval: int = 5
    publish_mode: str = "changed"  # 'changed' or 'all'

    def __post_init__(self):
        valid_levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
        if self.log_level.upper() not in valid_levels:
            raise ConfigValidationError(
                f"general.log_level={self.log_level!r} must be one of {valid_levels}"
            )
        self.log_level = self.log_level.upper()
        _validate_range(self.poll_interval, "general.poll_interval", 1, 3600)
        if self.publish_mode not in ('changed', 'all'):
            raise ConfigValidationError(
                f"general.publish_mode={self.publish_mode!r} must be 'changed' or 'all'"
            )


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
        self.debug: DebugConfig = None
        self.write: WriteConfig = None
        self.monitoring: MonitoringConfig = None
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
            log_level=_env_get('LOG_LEVEL', gen.get('log_level') or 'INFO'),
            log_file=_env_get('LOG_FILE', gen.get('log_file') or ''),
            poll_interval=_env_get('POLL_INTERVAL', gen.get('poll_interval') or 5, int),
            publish_mode=_env_get('PUBLISH_MODE', gen.get('publish_mode') or 'changed')
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
            consecutive_failures_for_sleep=_env_get('CONSECUTIVE_FAILURES_FOR_SLEEP', mb.get('consecutive_failures_for_sleep', 3), int),
            night_skip_inverters=_env_get('NIGHT_SKIP_INVERTERS', mb.get('night_skip_inverters', True), bool),
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
            qos=_env_get('MQTT_QOS', mq.get('qos', 0), int),
            ha_discovery_enabled=_env_get('HA_DISCOVERY_ENABLED', mq.get('ha_discovery_enabled', False), bool),
            tls_enabled=_env_get('MQTT_TLS_ENABLED', mq.get('tls_enabled', False), bool),
            tls_ca_certs=_env_get('MQTT_TLS_CA_CERTS', mq.get('tls_ca_certs', '')),
            tls_certfile=_env_get('MQTT_TLS_CERTFILE', mq.get('tls_certfile', '')),
            tls_keyfile=_env_get('MQTT_TLS_KEYFILE', mq.get('tls_keyfile', '')),
            tls_insecure=_env_get('MQTT_TLS_INSECURE', mq.get('tls_insecure', False), bool),
        )

        # Parse debug settings
        dbg = self.config.get('debug', {})
        self.debug = DebugConfig(
            validate_data=_env_get('DEBUG_VALIDATE_DATA', dbg.get('validate_data', True), bool),
            log_register_values=_env_get('DEBUG_LOG_REGISTERS', dbg.get('log_register_values', False), bool),
            log_scale_factors=_env_get('DEBUG_LOG_SCALE_FACTORS', dbg.get('log_scale_factors', False), bool),
            log_reconciliation=_env_get('DEBUG_LOG_RECONCILIATION', dbg.get('log_reconciliation', True), bool),
            log_publish_data=_env_get('DEBUG_LOG_PUBLISH_DATA', dbg.get('log_publish_data', False), bool),
            log_status_transitions=_env_get('DEBUG_LOG_STATUS_TRANSITIONS', dbg.get('log_status_transitions', True), bool),
        )

        # Parse write control settings
        wr = self.config.get('write', {})
        self.write = WriteConfig(
            enabled=_env_get('WRITE_ENABLED', wr.get('enabled', False), bool),
            min_power_limit_pct=_env_get('WRITE_MIN_POWER_LIMIT', wr.get('min_power_limit_pct', 10.0), float),
            max_power_limit_pct=_env_get('WRITE_MAX_POWER_LIMIT', wr.get('max_power_limit_pct', 100.0), float),
            rate_limit_seconds=_env_get('WRITE_RATE_LIMIT', wr.get('rate_limit_seconds', 30), int),
            auto_revert_seconds=_env_get('WRITE_AUTO_REVERT', wr.get('auto_revert_seconds', 3600), int),
            stabilization_delay=_env_get('WRITE_STABILIZATION_DELAY', wr.get('stabilization_delay', 2.0), float),
            command_topic_suffix=_env_get('WRITE_COMMAND_TOPIC', wr.get('command_topic_suffix', 'cmd')),
            command_queue_size=_env_get('WRITE_COMMAND_QUEUE_SIZE', wr.get('command_queue_size', 50), int),
        )

        # Parse monitoring settings
        mon = self.config.get('monitoring', {})
        self.monitoring = MonitoringConfig(
            enabled=_env_get('MONITORING_ENABLED', mon.get('enabled', False), bool),
            port=_env_get('MONITORING_PORT', mon.get('port', 8080), int),
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
            publish_mode=_env_get('INFLUXDB_PUBLISH_MODE', idb.get('publish_mode', '')),
            verify_ssl=_env_get('INFLUXDB_VERIFY_SSL', idb.get('verify_ssl', True), bool),
            ssl_ca_cert=_env_get('INFLUXDB_SSL_CA_CERT', idb.get('ssl_ca_cert', '')),
        )


def get_config(config_path: str = None) -> ConfigLoader:
    """Get configuration singleton"""
    return ConfigLoader.get_instance(config_path)
