# Changelog

All notable changes to Fronius Modbus MQTT will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - 2026-04-27

### Added
- **Inverter Power Limit Control via Modbus TCP Write**
  - Control inverter output power (WMaxLimPct) via MQTT commands
  - SunSpec Model 123 (Immediate Controls) — atomic 5-register write (40233-40237)
  - MQTT command topics: `fronius/inverter/{id}/cmd/set_power_limit` and `cmd/restore_power_limit`
  - MQTT result topic: `fronius/inverter/{id}/cmd/result` with success/failure status
  - 11-step safety write protocol:
    1. Rate limiting (min 30s between writes per device)
    2. Range validation (configurable min/max, default 10%-100%)
    3. Connection reset (flush DataManager buffer)
    4. Stabilization wait (0.5s for DataManager reset)
    5. Pre-write read (verify Model 123, get current scale factor)
    6. Register value calculation with scale factor
    7. Atomic 5-register write (limit, flags, revert timeout, ramp time, enable)
    8. Post-write stabilization delay (2s default)
    9. Read-back verification (confirm written value matches)
    10. Active limit tracking for auto-revert
    11. Result callback with before/after values
  - Auto-revert: automatically restore 100% after configurable timeout (default 1h)
  - Hardware-level revert: WMaxLim_RvrtTms register provides inverter-side fallback
  - Rate-limited commands re-queued for execution on next poll cycle
  - Bounded command queue (maxsize=10) prevents memory issues
  - All writes serialized with reads through the existing DevicePoller connection
  - Disabled by default — must explicitly set `write.enabled: true`
  - New configuration section: `write:` with `enabled`, `min_power_limit_pct`, `max_power_limit_pct`, `rate_limit_seconds`, `auto_revert_seconds`, `stabilization_delay`
  - Environment variable overrides: `WRITE_ENABLED`, `WRITE_MIN_POWER_LIMIT`, `WRITE_MAX_POWER_LIMIT`, `WRITE_RATE_LIMIT`, `WRITE_AUTO_REVERT`, `WRITE_STABILIZATION_DELAY`
- **Vendor Status Code Persistence to InfluxDB**
  - `vendor_status_code` (int) and `vendor_status_description` (string) fields in `fronius_inverter` measurement
  - Decoded from Fronius vendor event register (evt_vnd1) using FroniusEventFlags.json
  - Enables Grafana alerting on specific vendor fault codes

### Fixed
- **Shutdown Race Condition** — Poller thread now explicitly stopped and joined before `restore_all_power_limits()` runs; prevents concurrent Modbus access on the same connection during shutdown
- **MPPT Float Noise** — `_parse_mppt_module_optimized` now applies `round(result, -sf)` for negative scale factors, matching `RegisterParser.apply_scale_factor()` behavior; eliminates IEEE 754 artifacts (e.g. 5.050000000000001 → 5.05)
- **IEEE 754 Float Noise in Scale Factors** — `RegisterParser.apply_scale_factor()` rounds results for negative scale factors to eliminate floating-point artifacts across all measurements
- **FroniusModbusClient.disconnect() Robustness** — Tolerant to already-stopped poller (checks `is_alive()` before `join()`); explicitly disconnects poller's connection on shutdown
- **WMaxLim_Ena Stuck on Restore** — Restoring power limit to 100% now sets `WMaxLim_Ena=0` (disabled); previously left `Ena=1` which kept inverter in THROTTLED status even at full capacity

### Changed
- **Shutdown Sequence** — Stop poller loop → restore power limits → disconnect connections (was: restore while poller still running)
- **MQTT QoS 1 for Commands** — Command subscription uses QoS 1 for reliable delivery

## [1.5.0] - 2026-03-05

### Added
- **DataManager Buffer Corruption Detection & Reconciliation**
  - Fronius DataManager TCP server has a known buffer retention issue where Model 103 registers
    return stale/zero data while MPPT Model 160 data remains correct
  - Three detection strategies: Model 103 all-zero with MPPT producing, impossible status at night,
    FAULT status while MPPT strings are producing
  - Automatic reconciliation: DC power/voltage/current from MPPT sums (ground truth),
    AC power estimated as DC × 0.97, unreliable fields (voltages, temps) set to None
  - Status code recovery from last valid read or set to SLEEPING at night
  - Per-inverter corruption counters for monitoring
  - Corrupted reads tagged in InfluxDB (`corrupted=true`, `reconciled=true`) for Flux filtering
  - Corruption metadata published to MQTT (`corrupted`, `corruption_reason`, `reconciled`, `reconciled_fields`)
- **Night Inverter Skip**
  - Time-based inverter polling skip during night hours (configurable, default 21:00–06:00)
  - DataManager stays online at night but returns garbage data because inverters cycle sleep/wake
  - Meters continue to be polled (grid import/export data is always valid)
  - Configurable via `modbus.night_skip_inverters` (default: `true`)
  - Logged: "Skipping inverter polling (night mode)" / "Resuming inverter polling (dawn detected)"
- **Configurable Diagnostic Debug System**
  - New `debug:` YAML config section with 6 options (all with safe defaults)
  - `validate_data` — enable/disable buffer corruption detection (default: true)
  - `log_register_values` — log raw register hex values per read (default: false)
  - `log_scale_factors` — log scale factor calculations (default: false)
  - `log_reconciliation` — log corruption detection + reconciliation at WARNING level (default: true)
  - `log_publish_data` — log full data dict before publish (default: false)
  - `log_status_transitions` — log inverter status changes, e.g. MPPT→FAULT (default: true)
  - Environment variable overrides: `DEBUG_VALIDATE_DATA`, `DEBUG_LOG_RECONCILIATION`, etc.
- **Status Transition Tracking**
  - Per-inverter status change logging at WARNING level (after reconciliation, so corrected status is logged)
  - Example: `Inverter 1: MPPT(4) -> FAULT(7)` or `Inverter 2: STARTING(3) -> MPPT(4)`

### Fixed
- **Socket Leak Prevention** — Close old Modbus TCP client before creating new one on reconnect
- **Backoff Overflow Protection** — Cap exponential backoff to `2^6` (64s max) preventing unbounded growth
- **Meter Buffer Flush** — TCP connection reset + 0.3s delay before meter reads (same pattern as inverters)
- **Meter Empty Data Guard** — Skip publish when meter parse returns empty data instead of writing zeros
- **FAULT False Positive Fix** — Status code 7 (FAULT) only flagged as corruption when Model 103 is also all-zero AND MPPT is producing; standalone FAULT without all-zero data is treated as real
- **Dawn STARTING Exclusion** — Status code 3 (STARTING) no longer flagged as corruption at night; only MPPT(4) and THROTTLED(5) are impossible at night
- **Last Valid Data TTL** — 5-minute cache expiry prevents stale status recovery from hours-old data
- **MPPT Scale Factor Validation** — Reject MPPT data when scale factors outside [-10, +10] range (corrupted SFs)
- **MPPT Sanity Bounds** — Skip validation when MPPT values exceed physical limits (P>100kW, V>1kV, I>200A)
- **Register Parser Range Checks** — `decode_int16`, `decode_uint16`, `decode_uint32` validate input range before parsing
- **Register Parser Negative Values** — `struct.pack` mask (`reg & 0xFFFF`) prevents error on negative register values
- **Scale Factor Always WARNING** — Out-of-range scale factors logged at WARNING level (was sometimes DEBUG)
- **MQTT Disconnect Order** — `disconnect()` before `loop_stop()` per paho-mqtt documentation
- **MQTT Publish Error Isolation** — All three `publish_*_data` methods wrapped in try/except (outer/inner pattern)
- **MQTT _publish_data Isolation** — Publisher errors in main callback don't crash the polling thread
- **MQTT _on_connect Safety** — Full try/except wrapper; cache cleared BEFORE online status publish
- **MQTT Float Cache Precision** — Cache stores `round(value, 3)` to prevent phantom re-publishes from float drift
- **InfluxDB health() → ping()** — Replace deprecated `health()` API with `ping()` (influxdb-client 1.30+)
- **InfluxDB flush() Documented** — `flush()` is a no-op in batching mode; documented for forward-compatibility
- **InfluxDB Cross-Field Validation** — URL, token, and org are required when InfluxDB is enabled
- **InfluxDB NaN Filter** — Skip fields with NaN/Inf values before writing (InfluxDB rejects entire batch on NaN)
- **InfluxDB Write API Race Condition** — `write_api` access protected by lock during reconnection
- **Config YAML None Handling** — `None` values from YAML (`key:` without value) fall back to defaults via `or` pattern
- **Health File Atomic Writes** — Write to `.tmp` then `os.replace()` prevents partial reads by Docker healthcheck
- **Health File Lifecycle** — Cleanup on startup (stale from previous run) and shutdown
- **Startup Cleanup** — `_shutdown()` called before `sys.exit(1)` on all failure paths
- **Healthcheck Start Period** — Increased from 60s to 300s to accommodate maximum Modbus retry window (~240s)
- **Log File Encoding** — `RotatingFileHandler` uses explicit `encoding='utf-8'`
- **Night Hours from Config** — Corruption detection uses `night_start_hour`/`night_end_hour` from config (was hardcoded 22/5)

### Security
- **Entrypoint Shell Hardening** — `set -eo pipefail` for proper error propagation
- **Entrypoint Input Validation** — `INFLUXDB_BUCKET` and `INFLUXDB_ORG` validated with regex (`^[a-zA-Z0-9_-]+$`) to prevent shell injection
- **Entrypoint Curl Error Handling** — Explicit error handling instead of broken pipe semantics
- **Entrypoint Dead Code Removal** — Removed `trap EXIT` that never fires after `exec`
- **Entrypoint Non-Recursive Chown** — `chown` on volume directories only (not recursive into mounted data)

### Changed
- **Optimized Modbus Read Sequence**
  - TCP connection reset moved from between Model 103 and MPPT reads to before Model 103
  - Both register types now read on the same fresh connection per device
  - Halves the number of TCP connections per polling cycle (4 instead of 8 for 4 inverters)
  - Before: `[stale conn] → Model 103 (risk of corruption) → CLOSE+0.3s → MPPT (fresh)`
  - After: `CLOSE+0.3s → Model 103 (fresh) → MPPT (same fresh conn)`
- **Recommended Polling Configuration**
  - `poll_interval` default increased from 5s to 10s (aligns with DataManager's 10s SolarNet refresh)
  - `inverter_poll_delay` reduced from 3s to 1s (faster cycle, less DataManager pressure overall)
  - Full cycle: 4 inverters × 1s = 4s, then 6s idle before next cycle

## [1.4.1] - 2026-02-26

### Added
- **InfluxDB Storage Persistence** (Model 124)
  - New `fronius_storage` measurement with charge state, battery voltage, rates, ramps, grid charging
  - Change detection via `_should_write()` to avoid redundant writes
  - Wired from main publish callback alongside MQTT storage publishing
- **MPPT String Temperature**
  - `string{N}_temperature` field added to `fronius_inverter` InfluxDB measurement
- **Extended HA Storage Discovery**
  - Storage sensors expanded from 4 to 13: added OutWRte, InWRte, MinRsvPct, StorAval,
    WDisChaGra, WChaGra, VAChaMax, StorCtl_Mod, ChaGriSet
- **InfluxDB Healthcheck Validation**
  - Health file now includes `influxdb_enabled` field
  - Healthcheck marks container unhealthy when InfluxDB is enabled but disconnected
- **`.dockerignore`** - Reduces Docker build context by excluding .git, __pycache__, docs, etc.

### Fixed
- **MPPT Log Noise** - Per-string log demoted from INFO to DEBUG (~480 lines/min reduction at INFO)
- **Controls Write Isolation** - `_write_controls_data` error no longer marks entire inverter write as failed
- **`_should_publish` Thread Safety** - Return moved inside lock block (mqtt_publisher)
- **`_should_write` Thread Safety** - Rate limiting and change detection fully under lock (influxdb_publisher)
- **InfluxDB Reconnect Thread Safety** - `self.client` read under lock in `_reconnect_loop`
- **Bucket Existence Check** - Entrypoint uses JSON parsing instead of fragile `grep 'not found'`

### Removed
- Dead code: `InverterPoller`, `MeterPoller` backward-compat wrappers, `poll_all_devices`,
  duplicate `parse_mppt_measurements` in register_parser

## [1.4.0] - 2026-02-26

### Added
- **Persistent Connection Monitoring**
  - MQTT and InfluxDB reconnection threads now run permanently (no longer exit after first reconnect)
  - InfluxDB proactive health check every 30s detects disconnections within seconds (previously up to 5 min)
  - MQTT monitor thread tracks state transitions and logs connect/disconnect events
  - Disconnection counter (`disconnection_count`) in both publishers for operational visibility
- **InfluxDB Data Loss Callbacks**
  - `error_callback` logs ERROR when batch write fails permanently (all retries exhausted)
  - `retry_callback` logs WARNING on each retry attempt
  - More aggressive retry config: 10 retries, 5 min max retry time, exponential backoff
- **MQTT Message Queuing**
  - `max_queued_messages_set(1000)` for QoS >= 1 message buffering during disconnections
  - Publish methods no longer silently drop messages when disconnected (attempt delivery via paho)
  - `MQTT_ERR_QUEUE_SIZE` handling with warning log when outgoing queue is full
- **Extended Health Monitoring**
  - InfluxDB connection status (`influxdb:True/False`) in health file
  - MQTT and InfluxDB disconnection counters in health file
  - Healthcheck refactored to prefix-based key:value parsing (robust to field additions)
- **Log Rotation**
  - `RotatingFileHandler` replaces `FileHandler`: 5 MB per file, 3 backups (20 MB max)
- **Security Hardening**
  - Container runs as non-root user `fronius` (entrypoint uses `gosu` for privilege drop)
  - Optional TLS/SSL support for MQTT (`MQTT_TLS_*` env vars) and InfluxDB (`INFLUXDB_VERIFY_SSL`, `INFLUXDB_SSL_CA_CERT`)
  - InfluxDB token no longer exposed in process list (uses `curl -K` config file)
  - `MQTTConfig` and `InfluxDBConfig` mask secrets (password/token) in `__repr__` output
  - Example YAML config updated with TLS options
- **InfluxDB Controls Persistence**
  - Inverter controls (Model 123/SunSpec) persisted to `fronius_controls` measurement
  - Tracks power limit, power factor, VAR settings with change detection
- **Configuration Validation**
  - `__post_init__()` on all config dataclasses validates ranges at load time
  - `ConfigValidationError` with clear messages (e.g., `modbus.port=0 must be >= 1`)
  - Validates: ports, timeouts, poll intervals, QoS, log levels, publish modes

### Changed
- **pymodbus 3.11+ Upgrade**
  - Migrated from pymodbus 3.9.x to 3.11+ (`slave=` → `device_id=` API change)
  - Benefits: DoS vulnerability fix, stricter TCP protocol validation, dev_id/tid response checks
- **Dependency Upper Bounds**
  - All dependencies now have upper bounds to prevent breaking upgrades
  - `pymodbus>=3.11.0,<4.0.0`, `paho-mqtt<3.0.0`, `pyyaml<7.0.0`, `influxdb-client<2.0.0`
- **Thread Safety**
  - `self.connected` (bool) replaced with `threading.Event()` + property in both publishers
  - InfluxDB `_setup_client()` and `write_api.write()` protected by `self.lock`
  - Prevents race conditions during reconnection while polling threads write data
- **MQTT Reconnection Architecture**
  - Paho's built-in auto-reconnect (`reconnect_delay_set`) handles network recovery
  - Custom reconnect thread is now a state monitor (does not call `connect()` on running paho loop)
  - `_try_connect()` only called for initial connection failures (tracked via `_loop_started` flag)
  - Eliminates race condition between paho's network thread and external `connect()` calls
- **Interruptible Shutdown**
  - `time.sleep()` replaced with `_stop_event.wait()` in device polling loop
  - Shutdown responds immediately instead of waiting for poll delay (up to 10s with 4 inverters)

### Fixed
- Reconnection threads died permanently after first successful reconnect (broke on second disconnection)
- MQTT `_publish()` silently dropped all messages when disconnected (now attempts delivery)
- InfluxDB `_setup_client()` could race with `write_api.write()` from polling threads
- Health file parsing in healthcheck.py was index-based (broke when new fields were added)

## [1.3.1] - 2026-02-26

### Added
- **InfluxDB Event Detail Persistence**
  - New `events_json` field in `fronius_inverter` measurement
  - Stores decoded fault event codes, descriptions, and event class as compact JSON
  - Enables Grafana alerting and historical analysis of inverter fault events
  - Only written when events are active (no empty JSON stored)

### Fixed
- Moved `json` import to module level in `influxdb_publisher.py` (was incorrectly inside conditional block)

### Changed
- Updated InfluxDB measurement documentation in README with complete field list

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
| 1.6.0 | 2026-04-27 | Power limit control via MQTT, vendor status to InfluxDB, shutdown safety, float fixes |
| 1.5.0 | 2026-03-05 | Buffer corruption detection, night inverter skip, debug system, 2× audit hardening |
| 1.4.1 | 2026-02-26 | Storage InfluxDB, MPPT temp, thread safety, HA discovery expansion |
| 1.4.0 | 2026-02-26 | Resilient reconnection, data loss prevention, graceful shutdown |
| 1.3.1 | 2026-02-26 | InfluxDB events_json field for fault event persistence |
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
