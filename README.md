# Fronius Modbus MQTT

Python application that reads data from Fronius inverters and smart meters via Modbus TCP and publishes to MQTT and/or InfluxDB.

## Features

- **SunSpec Protocol Support** - Full SunSpec Modbus implementation with scale factors
- **Multi-Device Support** - Poll multiple inverters and smart meters
- **MPPT Data** - Per-string voltage, current, and power (Model 160)
- **Immediate Controls** - Read inverter control settings (Model 123)
- **Event Parsing** - Decode Fronius event flags with human-readable descriptions
- **Publish Modes** - Publish on change or publish all values
- **Docker Support** - Separate containers for inverters and meters
- **MQTT Integration** - Publish to any MQTT broker with configurable topics
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
```

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
1. Create a bucket named `fronius` in InfluxDB
2. Create an API token with read/write permissions for the bucket
3. Copy the token to your configuration

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

### Inverter Topics
```
fronius/inverter/{serial}/ac_power
fronius/inverter/{serial}/dc_power
fronius/inverter/{serial}/ac_voltage_an
fronius/inverter/{serial}/ac_voltage_bn
fronius/inverter/{serial}/ac_voltage_cn
fronius/inverter/{serial}/ac_current
fronius/inverter/{serial}/ac_frequency
fronius/inverter/{serial}/lifetime_energy
fronius/inverter/{serial}/status
fronius/inverter/{serial}/events
fronius/inverter/{serial}/mppt/1/voltage
fronius/inverter/{serial}/mppt/1/current
fronius/inverter/{serial}/mppt/1/power
fronius/inverter/{serial}/mppt/2/voltage
fronius/inverter/{serial}/mppt/2/current
fronius/inverter/{serial}/mppt/2/power
```

### Meter Topics
```
fronius/meter/{serial}/power_total
fronius/meter/{serial}/power_a
fronius/meter/{serial}/power_b
fronius/meter/{serial}/power_c
fronius/meter/{serial}/voltage_an
fronius/meter/{serial}/voltage_bn
fronius/meter/{serial}/voltage_cn
fronius/meter/{serial}/current_a
fronius/meter/{serial}/current_b
fronius/meter/{serial}/current_c
fronius/meter/{serial}/frequency
fronius/meter/{serial}/energy_exported
fronius/meter/{serial}/energy_imported
```

## InfluxDB Measurements

### fronius_inverter
| Field | Type | Description |
|-------|------|-------------|
| ac_power | float | AC power output (W) |
| dc_power | float | DC power input (W) |
| ac_voltage_an/bn/cn | float | Phase voltages (V) |
| ac_current | float | AC current (A) |
| ac_frequency | float | Grid frequency (Hz) |
| lifetime_energy | float | Total energy produced (Wh) |
| status_code | int | Operating status code |

### fronius_meter
| Field | Type | Description |
|-------|------|-------------|
| power_total | float | Total power (W) |
| power_a/b/c | float | Per-phase power (W) |
| voltage_an/bn/cn | float | Phase voltages (V) |
| current_a/b/c | float | Per-phase current (A) |
| frequency | float | Grid frequency (Hz) |
| energy_exported | float | Energy exported (Wh) |
| energy_imported | float | Energy imported (Wh) |

## Project Structure

```
fronius-modbus-mqtt/
├── fronius_modbus_mqtt.py      # Main entry point
├── fronius/                    # Python package
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
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
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

Tested with:
- Fronius Symo 17.5-3-M
- Fronius Symo Advanced 17.5-3-M
- Fronius Symo Advanced 20.0-3-M
- Fronius Smart Meter TS 5kA-3

Should work with any Fronius inverter with Modbus TCP enabled via DataManager.

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

## Contributing

Found a bug or have a feature request? Please open an issue on [GitHub Issues](https://github.com/sm26449/fronius-modbus-mqtt/issues).

## Author

**Stefan M**
- Email: sm26449@diysolar.ro
- GitHub: [@sm26449](https://github.com/sm26449)

## License

MIT License - Free and open source. See [LICENSE](LICENSE) for details.
