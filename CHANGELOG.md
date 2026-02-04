# Changelog

All notable changes to Fronius Modbus MQTT will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-02-04

### Added
- **Runtime Monitoring**
  - Per-device online/offline status tracking with automatic detection
  - Device marked offline after 3 consecutive read failures
  - Aggregate status for device types: "online", "partial", or "offline"
  - Cumulative read error counter per device
  - Last seen timestamp (ISO format) per device
  - Container uptime display (format: "Xd Xh Xm")
  - Periodic model_id re-verification (hourly) to detect configuration drift
  - Model_id verification triggered after 5 consecutive errors
  - Exponential backoff for offline devices (10s → 20s → 40s → 60s max)
  - New MQTT topics:
    - `fronius/inverter/status` - Aggregate inverter status
    - `fronius/meter/status` - Aggregate meter status
    - `fronius/inverter/{id}/runtime/*` - Per-device runtime stats
    - `fronius/meter/{id}/runtime/*` - Per-device runtime stats
  - Home Assistant autodiscovery for runtime sensors (entity_category: diagnostic)
  - Thread-safe runtime state tracking with proper locking
  - Uptime added to health file for Docker healthcheck

### Changed
- Health file now includes `uptime` field

### Fixed
- **Model ID Reading from DataManager Buffer**
  - Fronius DataManager buffer could retain residual SunSpec header data (0x5365 = "Se")
  - This caused invalid model_id values (e.g., 21365 instead of 103) for some inverters
  - Fix: Force TCP reconnection before reading model_id to clear stale buffer
  - Added retry logic (up to 3 attempts) with validation against known SunSpec models
  - Valid models: 101-103, 111-113 (inverters), 201-204 (meters)
- **Aggregate Status Publishing**
  - Fixed fronius-meter container incorrectly publishing "offline" for inverter aggregate status
  - Aggregate status now only published for device types with at least one device
- **Home Assistant Runtime Sensors**
  - Removed `state_class: total_increasing` from read_errors (counter resets on restart)
  - Added `retain=True` for all runtime MQTT topics (HA gets last state on reconnect)

## [1.2.7] - 2026-01-11

### Changed
- **InfluxDB Bucket Creation Moved to Container Entrypoint**
  - Bucket creation now happens inside container startup instead of post_deploy hooks
  - Waits for InfluxDB availability (up to 30 seconds) before creating bucket
  - More reliable than external docker exec commands
  - Bucket is created via InfluxDB API with proper error handling

### Fixed
- **InfluxDB Bucket Detection**
  - Fixed bucket existence check to properly handle JSON responses with spaces/newlines
  - Extracts org ID from bucket list instead of org API (works with bucket-only permissions)
  - Added curl as system dependency in Dockerfile for API calls

## [1.2.6] - 2026-01-11

### Added
- **Config Auto-Initialization**
  - New entrypoint.sh script that initializes config from defaults on first run
  - Default config is bundled in `/app/config.default/` inside the image
  - If mounted `/app/config` volume is empty, defaults are copied automatically
  - Eliminates need to manually copy config files before first start

### Changed
- **Config Volume Now Writable**
  - Removed `:ro` (read-only) flag from config volume mount in service templates
  - Allows container to write default config to mounted volume

### Fixed
- **Power Factor Scale for Primo Inverters**
  - Primo inverters report PF in high-precision format (0-10000) with SF=-2
  - This caused PF values ~100x too high (e.g., -99.95 instead of -0.9995)
  - Now detects format by checking raw value magnitude: abs(raw) > 100 uses SF=-4
  - Works correctly for both Symo (low precision) and Primo (high precision) inverters

### Tested Devices
- Fronius Primo (single-phase, Model 101)
- Fronius Smart Meter 63A-1 (single-phase, Model 201)

## [1.2.5] - 2026-01-07

### Added
- **Home Assistant MQTT Autodiscovery**
  - Automatic registration of Fronius devices in Home Assistant
  - Hierarchical topic structure: `homeassistant/sensor/fronius/inverter_1/w/config`
  - Supports inverters, meters, and storage devices
  - Includes proper device_class, state_class, and unit_of_measurement
  - MPPT string sensors included when available
  - Availability topic support (`fronius/status`) - devices go offline when container stops
  - Origin metadata (software name, version, GitHub URL)
  - Software version in device info
  - Enable with `HA_DISCOVERY_ENABLED=true` environment variable
  - Or `mqtt.ha_discovery_enabled: true` in YAML config
  - Discovery configs are retained and published once at startup
  - Binary sensors for status fields (`active`, `connected`, `*_enabled`)
  - MQTT Last Will Testament (LWT) for crash detection - status automatically goes offline

## [1.2.4] - 2026-01-04

### Fixed
- **Power Factor Scale Correction**
  - Fronius devices report PF as percentage (0-100) but SunSpec scale factor (PF_SF) returns 0 instead of -2
  - This caused PF values to be 100x too high (e.g., 7-14 instead of 0.07-0.14)
  - Fixed in `register_parser.py`: `parse_inverter_measurements()` and `parse_meter_measurements()`
  - Fixed in `modbus_client.py`: `_read_immediate_controls()` (Model 123)
  - PF values now correctly in -1.0 to 1.0 range

## [1.2.3] - 2026-01-04

### Added
- **Modbus Connection Retry Logic**
  - Retry with exponential backoff at startup (10 attempts, 2s to 60s delay)
  - Prevents container exit when Fronius DataManager is temporarily unavailable

- **InfluxDB Connection Retry Logic**
  - Retry with exponential backoff at startup (10 attempts, 2s to 60s delay)
  - Background reconnection thread if initial connection fails
  - Automatic reconnection every 30 seconds until successful
  - Graceful thread cleanup on shutdown
  - Write error detection triggers automatic reconnection

- **MQTT Connection Retry Logic**
  - Retry with exponential backoff at startup (10 attempts, 2s to 60s delay)
  - Background reconnection thread if initial connection fails
  - Automatic reconnection every 30 seconds until successful
  - Graceful thread cleanup on shutdown

### Fixed
- Modbus connection failures on container restart no longer cause immediate exit
- InfluxDB connection failures on container restart no longer require manual restart
- MQTT connection failures on container restart no longer require manual restart
- Services continue operating while attempting reconnection to unavailable backends
- InfluxDB write errors now trigger automatic reconnection when connection is lost

## [1.2.2] - 2025-01-01

### Fixed
- **Modbus Register Validation**
  - Added null check for register responses to prevent crashes
  - Fixed index bounds check for `evt_vnd4` parsing (requires 50 registers, not 49)

- **Error Handling & Logging**
  - Added exception logging in `ping_host()` for easier debugging
  - Changed retry failure logs from DEBUG to WARNING level for visibility
  - Added logging for storage register read failures
  - Changed health file write failure log from DEBUG to WARNING

- **Cross-Platform Support**
  - Fixed `ping_host()` for macOS (timeout in milliseconds vs seconds)
  - Added platform-specific documentation for ping timeout behavior

- **Shutdown Responsiveness**
  - Replaced `time.sleep()` with `Event.wait()` for interruptible sleep
  - Shutdown now responds immediately instead of waiting for poll interval (up to 5 min)

### Changed
- **Healthcheck Consistency**
  - Standardized healthcheck across all service files to use `healthcheck.py`
  - Previously `service.inverters.yaml` and `service.meter.yaml` used `pgrep`

## [1.2.1] - 2024-12-04

### Fixed
- **Docker Ping Check Issue**
  - Disabled `PING_CHECK_ENABLED` by default in Docker templates
  - ICMP ping requires `NET_RAW` capability not available in standard containers
  - Service would incorrectly enter sleep mode due to failed ping checks
  - **Note**: To enable ping checks in Docker, use `network_mode: host` or add `cap_add: [NET_RAW]`

### Added
- **InfluxDB Bucket Auto-Creation**
  - Added post_deploy hook to create InfluxDB bucket automatically
  - Runs `influx bucket create` in stack's influxdb container
  - Silently skips if bucket already exists

### Changed
- Updated service templates (`service.yaml`, `service.inverters.yaml`, `service.meter.yaml`)
  - Added `PING_CHECK_ENABLED=false` to environment
  - Added InfluxDB bucket creation hook in post_deploy

## [1.2.0] - 2024-12-03

### Added
- **Night/Sleep Mode Detection**
  - Detects when Fronius DataManager enters sleep mode at night
  - Ping check before attempting Modbus connection
  - Configurable night hours (default: 21:00-06:00)
  - Reduced polling interval during sleep mode (default: 5 minutes)
  - Automatic recovery when DataManager wakes up
  - New configuration options:
    - `NIGHT_MODE_ENABLED` - Enable/disable night mode detection
    - `NIGHT_POLL_INTERVAL` - Polling interval during sleep (seconds)
    - `NIGHT_START_HOUR` / `NIGHT_END_HOUR` - Night time window
    - `PING_CHECK_ENABLED` - Check host availability before connect
    - `CONSECUTIVE_FAILURES_FOR_SLEEP` - Failures before entering sleep

### Changed
- **Healthcheck Updated for Sleep Mode**
  - Accepts 'sleep' status as healthy (container stays running at night)
  - Extended max age to 10 minutes during sleep mode
  - Reports sleep mode reason (night time or DataManager unavailable)
- **Health File Format**
  - Added `sleep_mode:True/False` field
  - Added `night_time:True/False` field

## [1.1.0] - 2024-12-03

### Added
- **Docker Healthcheck**
  - New `healthcheck.py` script for container health monitoring
  - Checks Modbus connectivity and MQTT connection status
  - Writes health status to `/tmp/fronius_health` every 30 seconds
  - `HEALTHCHECK` directive added to Dockerfile
- **MPPT String Data (Model 160)**
  - Per-string DC voltage, current, and power readings
  - Per-string DC energy (lifetime)
  - Per-string temperature monitoring
  - Publishes to MQTT: `fronius/inverter/{serial}/mppt/string{n}/DCA|DCV|DCW|DCWH|Tmp`
  - Writes to InfluxDB: `string{n}_current`, `string{n}_voltage`, `string{n}_power`, `string{n}_energy`
- **Immediate Controls (Model 123)**
  - Connection status monitoring
  - Power limit percentage reading
  - Power factor settings
  - Reactive power (VAR) settings
  - Read every 60 seconds (configurable via `CONTROLS_POLL_INTERVAL`)
- **Storage Support Detection (Model 124)**
  - Automatically detects inverters with battery storage
  - Reads storage data if available (charge state, battery voltage, etc.)
- **Retry Logic**
  - Configurable retry attempts for Modbus reads
  - Exponential backoff on connection failures
  - Automatic reconnection after unit ID changes

### Changed
- **Single DevicePoller Thread**
  - Merged inverter and meter polling into single thread
  - Reduces Modbus TCP connection conflicts on DataManager
  - More reliable polling on systems with multiple devices
- **Connection Reset on Unit ID Change**
  - Forces TCP reconnection when switching between devices
  - Prevents stale data from DataManager's internal buffer

### Fixed
- Stale MPPT data when reading after main registers
- Model mismatch errors due to DataManager buffering

## [1.0.0] - 2024-11-30

### Added
- **Core Features**
  - SunSpec protocol support with automatic scale factor handling
  - Multi-device support (multiple inverters + smart meter)
  - MQTT publishing with change detection
  - InfluxDB integration with batching and rate limiting
- **Device Support**
  - Inverter Models 101/102/103 (Single/Split/Three Phase)
  - Meter Models 201-204 (Single/Split/Three Phase)
  - Common Block Model 1 (device identification)
- **Configuration**
  - YAML configuration file support
  - Environment variable override for all settings
  - Docker-ready with volume mounts
- **Publishing Modes**
  - `changed` - Only publish when values change (reduces traffic)
  - `all` - Publish all values at each interval
- **Event Parsing**
  - Decode Fronius vendor event flags
  - Human-readable event descriptions
  - Per-inverter-type event filtering (Symo, SnapINverter, etc.)
- **Device Cache**
  - Persistent device information cache
  - Reduces discovery time on restart
- **Docker Support**
  - Multi-stage Dockerfile
  - Separate containers for inverters/meters (service variants)
  - Health check integration
- **Integration**
  - docker-setup template integration
  - Three variants: `fronius`, `fronius:inverters`, `fronius:meter`

### Architecture
- Modular Python package structure
- Thread-safe Modbus connection with locking
- Configurable logging with file output
- Graceful shutdown handling

---

## Version History Summary

| Version | Date | Highlights |
|---------|------|------------|
| 1.3.0 | 2026-02-04 | Runtime monitoring with per-device status, error tracking, uptime |
| 1.2.7 | 2026-01-11 | InfluxDB bucket creation in container entrypoint |
| 1.2.6 | 2026-01-11 | Config auto-initialization, writable config volume |
| 1.2.5 | 2026-01-07 | Home Assistant MQTT autodiscovery with hierarchical topics |
| 1.2.4 | 2026-01-04 | Power Factor scale correction (was 100x too high) |
| 1.2.3 | 2026-01-04 | Connection retry logic for Modbus, MQTT, InfluxDB |
| 1.2.2 | 2025-01-01 | Modbus register validation, cross-platform ping fix |
| 1.2.1 | 2024-12-04 | Docker ping fix, InfluxDB bucket auto-creation |
| 1.2.0 | 2024-12-03 | Night/sleep mode detection, ping check, improved healthcheck |
| 1.1.0 | 2024-12-03 | MPPT string data, Model 123 controls, single poller thread |
| 1.0.0 | 2024-11-30 | Initial release with SunSpec protocol, MQTT, InfluxDB |

---

## SunSpec Models Supported

| Model | Description | Version |
|-------|-------------|---------|
| 1 | Common Block | 1.0.0 |
| 101-103 | Inverter (Single/Split/Three Phase) | 1.0.0 |
| 123 | Immediate Controls | 1.1.0 |
| 124 | Basic Storage Controls | 1.1.0 |
| 160 | MPPT (Multiple Power Point Tracker) | 1.1.0 |
| 201-204 | Meter (Single/Split/Three Phase) | 1.0.0 |

## Tested Devices

| Device | Model | Version Tested |
|--------|-------|----------------|
| Fronius Symo 17.5-3-M | Inverter | 1.0.0 |
| Fronius Symo Advanced 17.5-3-M | Inverter | 1.1.0 |
| Fronius Symo Advanced 20.0-3-M | Inverter | 1.1.0 |
| Fronius Smart Meter TS 5kA-3 | Meter | 1.0.0 |
