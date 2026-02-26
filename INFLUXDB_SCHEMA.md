# InfluxDB Schema Reference

Complete schema for the Fronius Modbus MQTT InfluxDB integration.

**Bucket:** `fronius` (configurable via `INFLUXDB_BUCKET`)
**Write Mode:** Batched (100 points, 10s flush interval, 2s jitter)
**Rate Limiting:** Configurable per-device write interval (default: 5s)
**Publish Mode:** `changed` (only write when values change) or `all`

---

## Measurement: `fronius_inverter`

Data from Fronius inverters (SunSpec Models 101-103, 123, 160).

### Tags

| Tag | Type | Description | Example |
|-----|------|-------------|---------|
| `device_id` | string | Modbus unit ID | `1`, `2`, `3`, `4` |
| `device_type` | string | Always `inverter` | `inverter` |
| `model` | string | Device model name | `Fronius Symo Advanced 20.0-3-M` |
| `serial_number` | string | Device serial number | `34559971` |
| `status` | string | SunSpec operating status name | `I_STATUS_MPPT`, `I_STATUS_FAULT` |

### Fields - AC Output

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `ac_power` | float | W | AC power output |
| `ac_current` | float | A | Total AC current |
| `ac_current_a` | float | A | Phase A current |
| `ac_current_b` | float | A | Phase B current (three-phase) |
| `ac_current_c` | float | A | Phase C current (three-phase) |
| `ac_voltage_an` | float | V | Phase A-N voltage |
| `ac_voltage_bn` | float | V | Phase B-N voltage (three-phase) |
| `ac_voltage_cn` | float | V | Phase C-N voltage (three-phase) |
| `ac_voltage_ab` | float | V | Phase A-B voltage (three-phase) |
| `ac_voltage_bc` | float | V | Phase B-C voltage (three-phase) |
| `ac_voltage_ca` | float | V | Phase C-A voltage (three-phase) |
| `ac_frequency` | float | Hz | Grid frequency |
| `power_factor` | float | - | Power factor (-1.0 to 1.0) |
| `apparent_power` | float | VA | Apparent power |
| `reactive_power` | float | var | Reactive power |

### Fields - DC Input

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `dc_power` | float | W | Total DC power input |
| `dc_voltage` | float | V | DC voltage |
| `dc_current` | float | A | DC current |

### Fields - Energy

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `lifetime_energy` | float | Wh | Total lifetime energy produced |

### Fields - Temperature

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `temp_cabinet` | float | C | Cabinet temperature |
| `temp_heatsink` | float | C | Heatsink temperature |
| `temp_transformer` | float | C | Transformer temperature |
| `temp_other` | float | C | Other temperature sensor |

### Fields - Status & Events

| Field | Type | Description |
|-------|------|-------------|
| `status_code` | int | SunSpec operating status code |
| `status_alarm` | bool | Alarm flag (true when fault/alarm active) |
| `event_count` | int | Number of active fault events |
| `events_json` | string | JSON array of decoded events (only present when events active) |

**SunSpec Status Codes:**

| Code | Name | Description |
|------|------|-------------|
| 0 | OFF | Inverter off |
| 1 | SLEEPING | Auto-shutdown |
| 2 | STARTING | Starting up |
| 3 | MPPT_FORCED | Tracking (forced) |
| 4 | MPPT | Tracking power point (normal operation) |
| 5 | THROTTLED | Power limited (grid/temp) |
| 6 | SHUTTING_DOWN | Shutting down |
| 7 | FAULT | Fault condition |
| 8 | STANDBY | Standby |

**`events_json` format:**
```json
[
  {
    "codes": [307, 522],
    "descriptions": ["DC low", "AC freq too high"],
    "class": "Fault"
  }
]
```

Each array element represents one event register (EvtVnd1-4). Fields:
- `codes` - List of numeric SunSpec state codes
- `descriptions` - Human-readable description for each code
- `class` - Event classification from FroniusEventFlags.json

### Fields - MPPT Strings (Model 160)

Dynamic fields per MPPT string (N = 1, 2, ...):

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `mppt_num_modules` | int | - | Number of MPPT modules |
| `string{N}_current` | float | A | String N DC current |
| `string{N}_voltage` | float | V | String N DC voltage |
| `string{N}_power` | float | W | String N DC power |
| `string{N}_energy` | float | Wh | String N lifetime energy |
| `string{N}_temperature` | float | C | String N module temperature |

Typical configurations:
- **Primo** (single-phase): 2 strings (`string1_*`, `string2_*`)
- **Symo** (three-phase): 2 strings (`string1_*`, `string2_*`)

### Installed Inverters

| device_id | Model | Rated Power | Serial |
|-----------|-------|-------------|--------|
| 1 | Fronius Symo Advanced 20.0-3-M | 20 kW | 34559971 |
| 2 | Fronius Symo Advanced 20.0-3-M | 20 kW | 34439632 |
| 3 | Fronius Symo Advanced 17.5-3-M | 17.5 kW | 34312229 |
| 4 | Fronius Symo 17.5-3-M | 17.5 kW | 31459301 |

---

## Measurement: `fronius_storage`

Battery storage data from inverters with storage support (SunSpec Model 124).

### Tags

| Tag | Type | Description | Example |
|-----|------|-------------|---------|
| `device_id` | string | Modbus unit ID (inverter) | `1` |
| `device_type` | string | Always `inverter` | `inverter` |
| `serial_number` | string | Inverter serial number | `34559971` |

### Fields

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `charge_state_pct` | float | % | State of charge |
| `battery_voltage` | float | V | Internal battery voltage |
| `max_charge_power` | float | W | Maximum charge power |
| `charge_status_code` | float | - | Charge status enum (1=OFF, 2=EMPTY, 3=DISCHARGING, 4=CHARGING, 5=FULL, 6=HOLDING, 7=TESTING) |
| `discharge_rate_pct` | float | % | Discharge rate (% of WDisChaMax) |
| `charge_rate_pct` | float | % | Charge rate (% of WChaMax) |
| `min_reserve_pct` | float | % | Minimum reserve percentage |
| `available_storage_ah` | float | Ah | Available storage capacity |
| `charge_ramp_rate` | float | %/s | Charge ramp rate (% WChaMax/sec) |
| `discharge_ramp_rate` | float | %/s | Discharge ramp rate (% WChaMax/sec) |
| `grid_charging_code` | float | - | Grid charging setting (0=PV, 1=GRID) |

---

## Measurement: `fronius_meter`

Data from Fronius Smart Meters (SunSpec Models 201-204).

### Tags

| Tag | Type | Description | Example |
|-----|------|-------------|---------|
| `device_id` | string | Modbus unit ID | `240` |
| `device_type` | string | Always `meter` | `meter` |
| `model` | string | Device model name | `Fronius Smart Meter TS 5kA-3` |
| `serial_number` | string | Device serial number | `40112233` |

### Fields - Power

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `power_total` | float | W | Total active power (+ export, - import) |
| `power_a` | float | W | Phase A active power |
| `power_b` | float | W | Phase B active power |
| `power_c` | float | W | Phase C active power |
| `va_total` | float | VA | Total apparent power |
| `va_a` | float | VA | Phase A apparent power |
| `va_b` | float | VA | Phase B apparent power |
| `va_c` | float | VA | Phase C apparent power |
| `var_total` | float | var | Total reactive power |
| `var_a` | float | var | Phase A reactive power |
| `var_b` | float | var | Phase B reactive power |
| `var_c` | float | var | Phase C reactive power |

### Fields - Voltage

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `voltage_ln_avg` | float | V | Line-to-neutral average voltage |
| `voltage_an` | float | V | Phase A-N voltage |
| `voltage_bn` | float | V | Phase B-N voltage |
| `voltage_cn` | float | V | Phase C-N voltage |
| `voltage_ll_avg` | float | V | Line-to-line average voltage |
| `voltage_ab` | float | V | Phase A-B voltage |
| `voltage_bc` | float | V | Phase B-C voltage |
| `voltage_ca` | float | V | Phase C-A voltage |

### Fields - Current

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `current_total` | float | A | Total current |
| `current_a` | float | A | Phase A current |
| `current_b` | float | A | Phase B current |
| `current_c` | float | A | Phase C current |

### Fields - Frequency & Power Factor

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `frequency` | float | Hz | Grid frequency |
| `pf_avg` | float | - | Average power factor (-1.0 to 1.0) |
| `pf_a` | float | - | Phase A power factor |
| `pf_b` | float | - | Phase B power factor |
| `pf_c` | float | - | Phase C power factor |

### Fields - Energy (Cumulative)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `energy_exported` | float | Wh | Total energy exported to grid |
| `energy_exported_a` | float | Wh | Phase A energy exported |
| `energy_exported_b` | float | Wh | Phase B energy exported |
| `energy_exported_c` | float | Wh | Phase C energy exported |
| `energy_imported` | float | Wh | Total energy imported from grid |
| `energy_imported_a` | float | Wh | Phase A energy imported |
| `energy_imported_b` | float | Wh | Phase B energy imported |
| `energy_imported_c` | float | Wh | Phase C energy imported |

### Installed Meters

| device_id | Model | Type |
|-----------|-------|------|
| 240 | Fronius Smart Meter TS 5kA-3 | Three-phase |

---

## Example Flux Queries

### Current inverter power by device
```flux
from(bucket: "fronius")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "fronius_inverter")
  |> filter(fn: (r) => r._field == "ac_power")
```

### Total solar production (all inverters)
```flux
from(bucket: "fronius")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "fronius_inverter")
  |> filter(fn: (r) => r._field == "ac_power")
  |> aggregateWindow(every: 5m, fn: mean)
  |> group(columns: ["_field"])
  |> sum()
```

### Grid import/export
```flux
from(bucket: "fronius")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "fronius_meter")
  |> filter(fn: (r) => r._field == "power_total")
  |> aggregateWindow(every: 5m, fn: mean)
```

### Active fault events
```flux
from(bucket: "fronius")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "fronius_inverter")
  |> filter(fn: (r) => r._field == "events_json")
```

### Throttling detection
```flux
from(bucket: "fronius")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "fronius_inverter" and r._field == "ac_power")
  |> filter(fn: (r) => r.status == "I_STATUS_THROTTLED" or r.status == "I_STATUS_FAULT")
  |> group(columns: ["device_id", "status"])
```

### MPPT string comparison
```flux
from(bucket: "fronius")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "fronius_inverter")
  |> filter(fn: (r) => r._field == "string1_power" or r._field == "string2_power")
  |> group(columns: ["device_id", "_field"])
```

### Inverter temperatures
```flux
from(bucket: "fronius")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "fronius_inverter")
  |> filter(fn: (r) => r._field =~ /^temp_/)
  |> group(columns: ["device_id", "_field"])
```

### Per-phase voltage balance (meter)
```flux
from(bucket: "fronius")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "fronius_meter")
  |> filter(fn: (r) => r._field == "voltage_an" or r._field == "voltage_bn" or r._field == "voltage_cn")
  |> aggregateWindow(every: 1m, fn: mean)
```

### Inverter status timeline
```flux
from(bucket: "fronius")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "fronius_inverter" and r._field == "status_code")
  |> group(columns: ["device_id", "status"])
```

---

## Write Configuration

| Parameter | Value | Description |
|-----------|-------|-------------|
| `batch_size` | 100 | Points per batch |
| `flush_interval` | 10,000 ms | Max time before flush |
| `jitter_interval` | 2,000 ms | Random delay to spread writes |
| `retry_interval` | 5,000 ms | Retry delay on failure |
| `max_retries` | 3 | Max retry attempts per batch |
| `write_interval` | 5s (default) | Min interval between writes per device |
| `publish_mode` | `changed` | Only write when values change (configurable) |

## Connection Resilience

- Startup: 10 retry attempts with exponential backoff (2s to 60s)
- Background reconnection thread if initial connection fails
- Automatic reconnection every 30s until successful
- Write errors trigger automatic reconnection detection
