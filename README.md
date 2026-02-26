# Fronius Modbus MQTT

[![Version](https://img.shields.io/badge/version-1.4.0-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Python application that reads data from Fronius inverters and smart meters via Modbus TCP and publishes to MQTT and/or InfluxDB.

## Features

- **SunSpec Protocol Support** - Full SunSpec Modbus implementation with automatic scale factor handling
- **Multi-Device Support** - Poll multiple inverters and smart meters simultaneously
- **Home Assistant Integration** - MQTT autodiscovery for automatic entity creation
- **Runtime Monitoring** - Per-device online/offline status, read error counters, uptime tracking
- **MPPT Data** - Per-string voltage, current, and power (Model 160)
- **Immediate Controls** - Read inverter control settings (Model 123)
- **Event Parsing** - Decode Fronius event flags with human-readable descriptions
- **Night Mode** - Automatic sleep detection when inverters go offline at night
- **Connection Resilience** - Persistent reconnection monitoring, proactive health checks, data loss prevention
- **Security Hardened** - Non-root container, optional TLS/SSL, secret masking in logs
- **Publish Modes** - Publish on change or publish all values
- **Docker Support** - Separate containers for inverters and meters
- **MQTT Integration** - Publish to any MQTT broker with configurable topics and LWT
- **InfluxDB Integration** - Time-series database storage with batching and rate limiting

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/sm26449/fronius-modbus-mqtt.git
cd fronius-modbus-mqtt
```

### 2. Create Configuration

```bash
# Copy example config
cp config/fronius_modbus_mqtt.example.yaml config/fronius_modbus_mqtt.yaml

# Edit with your settings
nano config/fronius_modbus_mqtt.yaml
```

Minimum configuration:
```yaml
modbus:
  host: 192.168.1.100      # Fronius DataManager IP

mqtt:
  enabled: true
  broker: 192.168.1.100    # MQTT broker IP
```

### 3. Build Docker Images

```bash
docker-compose build
```

### 4. Prepare Storage Directories

**For local development/testing:**
```bash
# Create local storage directories
mkdir -p storage/fronius-inverters/{config,data,logs}
mkdir -p storage/fronius-meter/{config,data,logs}

# Copy config files
cp config/fronius_modbus_mqtt.yaml storage/fronius-inverters/config/
cp config/fronius_modbus_mqtt.yaml storage/fronius-meter/config/
cp config/registers.json storage/fronius-inverters/config/
cp config/registers.json storage/fronius-meter/config/
cp config/FroniusEventFlags.json storage/fronius-inverters/config/
cp config/FroniusEventFlags.json storage/fronius-meter/config/
```

**For production deployment:**
```bash
# Use docker-compose.production.yml for absolute paths
cp docker-compose.production.yml docker-compose.yml

# Create directories on your server
sudo mkdir -p /docker-storage/pv-stack/fronius-inverters/{config,data,logs}
sudo mkdir -p /docker-storage/pv-stack/fronius-meter/{config,data,logs}

# Copy config files
sudo cp config/fronius_modbus_mqtt.yaml /docker-storage/pv-stack/fronius-inverters/config/
sudo cp config/fronius_modbus_mqtt.yaml /docker-storage/pv-stack/fronius-meter/config/
sudo cp config/registers.json /docker-storage/pv-stack/fronius-inverters/config/
sudo cp config/registers.json /docker-storage/pv-stack/fronius-meter/config/
sudo cp config/FroniusEventFlags.json /docker-storage/pv-stack/fronius-inverters/config/
sudo cp config/FroniusEventFlags.json /docker-storage/pv-stack/fronius-meter/config/
```

### 5. Start Containers

```bash
docker-compose up -d
```

### 6. Verify Operation

```bash
# Check container status
docker-compose ps

# View inverter logs
docker logs -f fronius-inverters

# View meter logs
docker logs -f fronius-meter
```

## Configuration Reference

### General Settings

```yaml
general:
  log_level: INFO              # DEBUG, INFO, WARNING, ERROR
  log_file: "/app/logs/fronius.log"  # Log file path
  poll_interval: 5             # Seconds between polling cycles
  publish_mode: changed        # 'changed' or 'all'
```

### Modbus Settings

```yaml
modbus:
  host: 192.168.1.100          # Fronius DataManager IP
  port: 502                    # Modbus TCP port
  timeout: 3                   # Connection timeout (seconds)
  retry_attempts: 3            # Retries on failure
  retry_delay: 0.5             # Delay between retries (seconds)
```

### Device Settings

```yaml
devices:
  inverters: [1, 2, 3, 4]      # Inverter Modbus IDs
  meters: [240]                # Meter Modbus ID
  inverter_poll_delay: 2       # Delay between device reads (seconds)
  inverter_read_delay_ms: 500  # Delay between register blocks (ms)
```

### MQTT Settings

```yaml
mqtt:
  enabled: true
  broker: 192.168.1.100
  port: 1883
  username: ""                 # Optional authentication
  password: ""
  topic_prefix: fronius        # Base topic
  retain: true                 # Retain messages
  qos: 0                       # QoS level (0, 1, 2)
  ha_discovery_enabled: true   # Home Assistant MQTT autodiscovery
  ha_discovery_prefix: homeassistant  # HA discovery topic prefix
```

### Home Assistant Integration

When `ha_discovery_enabled: true`, the application automatically registers all devices and entities in Home Assistant via MQTT autodiscovery.

**Features:**
- Automatic entity creation for all measurements
- Proper device grouping (inverters, meters)
- Correct device_class and state_class for energy dashboard
- Availability tracking via LWT (Last Will Testament)
- MPPT string sensors when available

### InfluxDB Settings

```yaml
influxdb:
  enabled: true
  url: http://192.168.1.100:8086
  token: "your-influxdb-token"
  org: "your-org"
  bucket: "fronius"
  write_interval: 5            # Min seconds between writes per device
  publish_mode: changed        # 'changed' or 'all'
```

**InfluxDB Setup:**
1. Create an API token with read/write permissions for buckets
2. Copy the token to your configuration
3. The bucket specified in `INFLUXDB_BUCKET` is automatically created on container startup if it doesn't exist

## Environment Variables

Configuration can also be set via environment variables (useful for Docker):

| Variable | Description | Default |
|----------|-------------|---------|
| **General** | | |
| `LOG_LEVEL` | Logging level | `INFO` |
| `LOG_FILE` | Log file path | `` |
| `POLL_INTERVAL` | Polling interval (s) | `5` |
| `PUBLISH_MODE` | `changed` or `all` | `changed` |
| `FRONIUS_CONFIG` | Path to YAML config file | `` |
| **Modbus** | | |
| `MODBUS_HOST` | Fronius DataManager IP | _(required)_ |
| `MODBUS_PORT` | Modbus TCP port | `502` |
| `MODBUS_TIMEOUT` | Connection timeout (s) | `3` |
| `MODBUS_RETRY_ATTEMPTS` | Retries per read | `2` |
| `MODBUS_RETRY_DELAY` | Delay between retries (s) | `0.1` |
| **Devices** | | |
| `INVERTER_IDS` | Comma-separated inverter IDs | `1` |
| `METER_IDS` | Comma-separated meter IDs | `240` |
| `METER_POLL_INTERVAL` | Meter polling interval (s) | `2.0` |
| `INVERTER_POLL_DELAY` | Delay between inverter reads (s) | `1.0` |
| `INVERTER_READ_DELAY_MS` | Delay between register blocks (ms) | `200` |
| **Night Mode** | | |
| `NIGHT_MODE_ENABLED` | Enable night/sleep detection | `true` |
| `NIGHT_POLL_INTERVAL` | Poll interval during sleep (s) | `300` |
| `NIGHT_START_HOUR` | Night start hour (24h) | `21` |
| `NIGHT_END_HOUR` | Night end hour (24h) | `6` |
| `PING_CHECK_ENABLED` | Ping host before connect | `true` |
| `CONSECUTIVE_FAILURES_FOR_SLEEP` | Failures before sleep mode | `3` |
| **MQTT** | | |
| `MQTT_ENABLED` | Enable MQTT publishing | `true` |
| `MQTT_BROKER` | MQTT broker address | `localhost` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MQTT_USERNAME` | MQTT username | `` |
| `MQTT_PASSWORD` | MQTT password | `` |
| `MQTT_PREFIX` | MQTT topic prefix | `fronius` |
| `MQTT_RETAIN` | Retain messages | `true` |
| `MQTT_QOS` | QoS level (0, 1, 2) | `0` |
| `HA_DISCOVERY_ENABLED` | Enable HA autodiscovery | `false` |
| `MQTT_TLS_ENABLED` | Enable TLS/SSL for MQTT | `false` |
| `MQTT_TLS_CA_CERTS` | Path to CA certificate file | `` |
| `MQTT_TLS_CERTFILE` | Path to client certificate file | `` |
| `MQTT_TLS_KEYFILE` | Path to client private key file | `` |
| `MQTT_TLS_INSECURE` | Skip hostname verification | `false` |
| **InfluxDB** | | |
| `INFLUXDB_ENABLED` | Enable InfluxDB | `false` |
| `INFLUXDB_URL` | InfluxDB URL | `` |
| `INFLUXDB_TOKEN` | InfluxDB API token | `` |
| `INFLUXDB_ORG` | InfluxDB organization | `` |
| `INFLUXDB_BUCKET` | InfluxDB bucket | `fronius` |
| `INFLUXDB_WRITE_INTERVAL` | Min seconds between writes per device | `5` |
| `INFLUXDB_PUBLISH_MODE` | Override publish mode for InfluxDB | `` |
| `INFLUXDB_VERIFY_SSL` | Verify SSL certificates | `true` |
| `INFLUXDB_SSL_CA_CERT` | Path to CA certificate file | `` |

## Command Line Options

```bash
python fronius_modbus_mqtt.py [OPTIONS]

Options:
  -c, --config PATH    Path to configuration file
  -d, --device TYPE    Device type to poll: all, inverter, or meter
  -f, --force          Force start even if another instance is running
  -v, --version        Show version
```

## Docker Commands

```bash
# Build images
docker-compose build

# Build without cache (after code changes)
docker-compose build --no-cache

# Start containers
docker-compose up -d

# Stop containers
docker-compose down

# Restart containers
docker-compose restart

# View logs
docker logs -f fronius-inverters
docker logs -f fronius-meter

# Check status
docker-compose ps
```

## MQTT Topics

Topics use the Modbus unit ID (not serial number) for device identification.

### Status Topics
```
fronius/status                    # "online" or "offline" (LWT)
fronius/inverter/status           # Aggregate: "online", "partial", or "offline"
fronius/meter/status              # Aggregate: "online", "partial", or "offline"
```

### Runtime Monitoring Topics
Per-device runtime statistics (diagnostic entities in Home Assistant):
```
fronius/inverter/{id}/runtime/status       # "online" or "offline"
fronius/inverter/{id}/runtime/last_seen    # ISO timestamp (e.g., "2026-02-04T10:45:23")
fronius/inverter/{id}/runtime/read_errors  # Cumulative error count
fronius/inverter/{id}/runtime/uptime       # Container uptime (e.g., "4d 12h 35m")
fronius/inverter/{id}/runtime/model_id     # SunSpec model ID (e.g., 103)

fronius/meter/{id}/runtime/status          # Same structure for meters
fronius/meter/{id}/runtime/last_seen
fronius/meter/{id}/runtime/read_errors
fronius/meter/{id}/runtime/uptime
fronius/meter/{id}/runtime/model_id
```

### Inverter Topics
```
fronius/inverter/{id}/W           # AC Power (W)
fronius/inverter/{id}/DCW         # DC Power (W)
fronius/inverter/{id}/PhVphA      # Phase A Voltage (V)
fronius/inverter/{id}/PhVphB      # Phase B Voltage (V) - three-phase only
fronius/inverter/{id}/PhVphC      # Phase C Voltage (V) - three-phase only
fronius/inverter/{id}/A           # AC Current (A)
fronius/inverter/{id}/Hz          # Frequency (Hz)
fronius/inverter/{id}/WH          # Lifetime Energy (Wh)
fronius/inverter/{id}/PF          # Power Factor (-1.0 to 1.0)
fronius/inverter/{id}/St          # Status Code
fronius/inverter/{id}/status      # Status Description
fronius/inverter/{id}/events      # Active Events (JSON)
fronius/inverter/{id}/mppt/1/DCV  # MPPT1 Voltage (V)
fronius/inverter/{id}/mppt/1/DCA  # MPPT1 Current (A)
fronius/inverter/{id}/mppt/1/DCW  # MPPT1 Power (W)
fronius/inverter/{id}/mppt/2/DCV  # MPPT2 Voltage (V)
fronius/inverter/{id}/mppt/2/DCA  # MPPT2 Current (A)
fronius/inverter/{id}/mppt/2/DCW  # MPPT2 Power (W)
```

### Meter Topics
```
fronius/meter/{id}/W              # Total Power (W)
fronius/meter/{id}/WphA           # Phase A Power (W)
fronius/meter/{id}/WphB           # Phase B Power (W)
fronius/meter/{id}/WphC           # Phase C Power (W)
fronius/meter/{id}/PhVphA         # Phase A Voltage (V)
fronius/meter/{id}/PhVphB         # Phase B Voltage (V)
fronius/meter/{id}/PhVphC         # Phase C Voltage (V)
fronius/meter/{id}/AphA           # Phase A Current (A)
fronius/meter/{id}/AphB           # Phase B Current (A)
fronius/meter/{id}/AphC           # Phase C Current (A)
fronius/meter/{id}/Hz             # Frequency (Hz)
fronius/meter/{id}/PF             # Power Factor (-1.0 to 1.0)
fronius/meter/{id}/TotWhExp       # Total Energy Exported (Wh)
fronius/meter/{id}/TotWhImp       # Total Energy Imported (Wh)
```

### Example
For inverter with Modbus ID 1 and meter with ID 240:
```
fronius/inverter/1/W              # Inverter power
fronius/meter/240/W               # Meter power
```

## InfluxDB Measurements

For the complete InfluxDB schema with all tags, fields, example Flux queries, and write configuration, see **[INFLUXDB_SCHEMA.md](INFLUXDB_SCHEMA.md)**.

### fronius_inverter
| Field | Type | Description |
|-------|------|-------------|
| ac_power | float | AC power output (W) |
| ac_current, ac_current_a/b/c | float | AC current total and per-phase (A) |
| ac_voltage_an/bn/cn | float | Line-to-neutral voltages (V) |
| ac_voltage_ab/bc/ca | float | Line-to-line voltages (V) |
| ac_frequency | float | Grid frequency (Hz) |
| dc_power | float | DC power input (W) |
| dc_voltage | float | DC voltage (V) |
| dc_current | float | DC current (A) |
| power_factor | float | Power factor (-1.0 to 1.0) |
| apparent_power | float | Apparent power (VA) |
| reactive_power | float | Reactive power (var) |
| lifetime_energy | float | Total energy produced (Wh) |
| temp_cabinet/heatsink/transformer/other | float | Temperatures (C) |
| status_code | int | Operating status code |
| status_alarm | bool | Alarm flag |
| event_count | int | Number of active events |
| events_json | string | JSON array with decoded event codes, descriptions and class |
| mppt_num_modules | int | Number of MPPT modules |
| string{N}_current | float | MPPT string N current (A) |
| string{N}_voltage | float | MPPT string N voltage (V) |
| string{N}_power | float | MPPT string N power (W) |
| string{N}_energy | float | MPPT string N lifetime energy (Wh) |

### fronius_meter
| Field | Type | Description |
|-------|------|-------------|
| power_total, power_a/b/c | float | Active power total and per-phase (W) |
| current_total, current_a/b/c | float | Current total and per-phase (A) |
| voltage_ln_avg, voltage_an/bn/cn | float | Line-to-neutral voltages (V) |
| voltage_ll_avg, voltage_ab/bc/ca | float | Line-to-line voltages (V) |
| frequency | float | Grid frequency (Hz) |
| va_total, va_a/b/c | float | Apparent power (VA) |
| var_total, var_a/b/c | float | Reactive power (var) |
| pf_avg, pf_a/b/c | float | Power factor (-1.0 to 1.0) |
| energy_exported, energy_exported_a/b/c | float | Energy exported to grid (Wh) |
| energy_imported, energy_imported_a/b/c | float | Energy imported from grid (Wh) |

## Project Structure

```
fronius-modbus-mqtt/
├── fronius_modbus_mqtt.py      # Main entry point
├── fronius/                    # Python package
│   ├── __init__.py             # Version and exports
│   ├── config.py               # YAML configuration loader
│   ├── modbus_client.py        # Modbus TCP client with autodiscovery
│   ├── register_parser.py      # SunSpec register parsing
│   ├── mqtt_publisher.py       # MQTT publishing with change detection
│   ├── influxdb_publisher.py   # InfluxDB writer with batching
│   ├── device_cache.py         # Persistent device cache
│   └── logging_setup.py        # Logging configuration
├── config/
│   ├── fronius_modbus_mqtt.example.yaml  # Example configuration
│   ├── registers.json          # Modbus register definitions
│   └── FroniusEventFlags.json  # Event flag mappings
├── Dockerfile                  # Container image definition
├── entrypoint.sh               # Container startup with config init
├── healthcheck.py              # Docker healthcheck script
├── docker-compose.yml          # Development compose
├── docker-compose.production.yml  # Production compose
├── requirements.txt
├── CHANGELOG.md                # Version history
├── INFLUXDB_SCHEMA.md          # Complete InfluxDB schema reference
└── INTERNAL.md                 # Internal technical documentation
```

## SunSpec Models

| Model | Description |
|-------|-------------|
| 1 | Common Block (Manufacturer, Model, Serial) |
| 101-103 | Inverter (Single/Split/Three Phase) |
| 123 | Immediate Controls |
| 160 | MPPT (Multiple Power Point Tracker) |
| 201-204 | Meter (Single/Split/Three Phase) |

## Supported Devices

### Inverters (Tested)
| Model | Type | Notes |
|-------|------|-------|
| Fronius Primo 3.0-1 | Single-phase | Model 101 |
| Fronius Primo 5.0-1 | Single-phase | Model 101 |
| Fronius Symo 17.5-3-M | Three-phase | Model 103 |
| Fronius Symo Advanced 17.5-3-M | Three-phase | Model 103 |
| Fronius Symo Advanced 20.0-3-M | Three-phase | Model 103 |

### Smart Meters (Tested)
| Model | Type | Notes |
|-------|------|-------|
| Fronius Smart Meter TS 5kA-3 | Three-phase | Model 203 |
| Fronius Smart Meter 63A-1 | Single-phase | Model 201 |

### Compatibility
Should work with any Fronius device that supports:
- Modbus TCP via DataManager 2.0 or integrated DataManager
- SunSpec protocol (Models 1, 101-103, 123, 160, 201-204)

## Troubleshooting

### Connection Issues
- Verify Modbus TCP is enabled on the Fronius DataManager
- Check firewall allows port 502
- Ensure correct IP address in configuration

### No Data
- Check inverter Modbus IDs (typically 1-4)
- Verify meter ID (typically 240)
- Review logs for error messages

### InfluxDB Errors
- Verify bucket exists
- Check API token has write permissions
- Confirm organization name is correct

## Manual Installation (without Docker)

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit configuration
cp config/fronius_modbus_mqtt.example.yaml config/fronius_modbus_mqtt.yaml
nano config/fronius_modbus_mqtt.yaml

# Run
python fronius_modbus_mqtt.py

# Run for inverters only
python fronius_modbus_mqtt.py -d inverter

# Run for meter only
python fronius_modbus_mqtt.py -d meter
```

## Integration with docker-setup

This project integrates seamlessly with [docker-setup](https://github.com/sm26449/docker-setup):

```bash
# Install using docker-setup
cd /opt/docker-setup
sudo ./install.sh
# Select: Add Services -> fronius (or fronius:inverters, fronius:meter)
```

**Available variants:**
- `fronius` - All devices (inverters + meter)
- `fronius:inverters` - Inverters only
- `fronius:meter` - Smart meter only

## Contributing

Found a bug or have a feature request? Please open an issue on [GitHub Issues](https://github.com/sm26449/fronius-modbus-mqtt/issues).

## Authors

**Stefan M** - [sm26449@diysolar.ro](mailto:sm26449@diysolar.ro)

**Claude** (Anthropic) - Pair programming partner

## License

MIT License - Free and open source software.

Copyright (c) 2024-2026 Stefan M <sm26449@diysolar.ro>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

**Disclaimer**: This software is provided "as is", without warranty of any kind. Use at your own risk when monitoring critical energy systems.
