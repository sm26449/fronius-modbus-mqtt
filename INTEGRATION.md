# Fronius Modbus MQTT - Docker Setup Integration

This template integrates the [fronius-modbus-mqtt](https://github.com/sm26449/fronius-modbus-mqtt) project into Docker Services Manager.

## Setup Instructions

### Option 1: Clone into templates folder (Recommended)

```bash
cd /opt/docker-setup/templates
rm -rf fronius  # Remove empty template folder if exists
git clone https://github.com/sm26449/fronius-modbus-mqtt.git fronius
```

### Option 2: Update existing clone

If you already have the repository cloned:

```bash
cd /opt/docker-setup/templates/fronius
git pull origin main
```

## Service Variants

This template provides three variants:

| Variant | Command | Description |
|---------|---------|-------------|
| `fronius` | Default | Monitors both inverters and smart meters |
| `fronius:inverters` | `-d inverters` | Monitors only inverters |
| `fronius:meter` | `-d meter` | Monitors only smart meters |

## Installation via Docker Services Manager

```bash
sudo ./install.sh
# Select: 2 - Add Services
# Choose: fronius (or fronius:inverters / fronius:meter)
```

The installer will prompt for:
- Fronius DataManager IP address
- Modbus device IDs (inverters: 1, meter: 240)
- MQTT broker settings
- InfluxDB settings (optional)

## File Structure

After cloning, the folder should contain:

```
templates/fronius/
├── service.yaml           # Default variant (all devices)
├── service.inverters.yaml # Inverters only variant
├── service.meter.yaml     # Meter only variant
├── Dockerfile             # Container build file
├── fronius_modbus_mqtt.py # Main application
├── fronius/               # Python modules
├── config/
│   ├── registers.json     # Modbus register definitions
│   └── FroniusEventFlags.json
├── README.md              # Original project documentation
└── INTEGRATION.md         # This file
```

## Dependencies

The service templates declare dependencies on:
- `mosquitto` - MQTT broker
- `influxdb` - Time-series database (optional)

These will be auto-detected or installed when deploying the service.

## Manual Docker Usage

If you prefer to run without Docker Services Manager:

```bash
cd templates/fronius
docker-compose up -d
```

See the main [README.md](README.md) for detailed configuration options.

## Updating

To update to the latest version:

```bash
cd /opt/docker-setup/templates/fronius
git pull origin main
docker-compose build --no-cache
docker-compose up -d
```

## Troubleshooting

### Container won't start
- Check Modbus connection: `nc -vz <fronius_ip> 502`
- Verify device IDs match your setup
- Check logs: `docker logs fronius-inverters`

### No data in MQTT/InfluxDB
- Verify MQTT broker is reachable
- Check InfluxDB token and bucket permissions
- Enable DEBUG logging: `LOG_LEVEL=DEBUG`
