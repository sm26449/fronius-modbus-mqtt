# Monitoring Dashboard — Reference

The collector ships with a built-in HTTP server that exposes:

- A dark-theme **HTML dashboard** with auto-refresh and an interactive
  control modal per inverter.
- A **JSON API** for programmatic consumers (pv-stack-ui, scripts, etc.).
- **Control endpoints** for per-inverter power limit writes (covered in
  detail in [POWER_LIMIT_CONTROL.md](./POWER_LIMIT_CONTROL.md)).

The server runs as a daemon thread inside the main process — it shares
its lifetime with the collector and dies on shutdown.

![Dashboard](monitoring-dashboard.png)

---

## 1. Enabling

```yaml
monitoring:
  enabled: true
  port: 8080            # In-container port; Docker maps this to the host
```

Environment overrides: `MONITORING_ENABLED=true`, `MONITORING_PORT=8080`.

Default Docker port mappings (set in `docker-compose.production.yml`):

```yaml
services:
  fronius-inverters:
    ports: ["8082:8080"]
  fronius-meter:
    ports: ["8083:8080"]
```

---

## 2. Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML dashboard. Auto-refreshes every 30 s (paused while the control modal is open). |
| `GET` | `/?view=json` | Full JSON state — legacy alias kept for backwards compatibility. |
| `GET` | `/api/data` | Identical payload to `?view=json` (preferred for new clients). |
| `POST` | `/api/inverter/{id}/power_limit` | Queue a power-limit write. Only registered when `write.enabled: true`. See [POWER_LIMIT_CONTROL.md §3](./POWER_LIMIT_CONTROL.md#3-the-dashboard-modal). |
| `POST` | `/api/inverter/{id}/restore` | Queue a restore-to-100 % write. Same enablement rules as above. |

There is **no authentication**. The server binds on `0.0.0.0` inside
the container — protect it at the network layer (Docker network, firewall,
reverse proxy) if you expose it beyond the host.

---

## 3. JSON payload shape

```json
{
  "app": {
    "version": "1.8.0",
    "uptime_seconds": 12345,
    "uptime_formatted": "3h25m",
    "container_type": "inverter",
    "hostname": "ee47163db2a5",
    "pid": 1,
    "timestamp": "2026-05-15 19:30:00"
  },
  "modbus": {
    "host": "192.168.88.240",
    "port": 502,
    "connected": true,
    "in_sleep_mode": false,
    "is_night_time": false,
    "successful_reads": 1234,
    "failed_reads": 5
  },
  "devices": [
    {
      "device_id": 1,
      "device_type": "inverter",
      "manufacturer": "Fronius",
      "model": "Symo Advanced 20.0-3-M",
      "serial_number": "34559971",
      "model_id": 103,
      "status": "online",
      "last_seen": "2026-05-15T19:30:00.123456",
      "read_errors": 0,
      "consecutive_errors": 0,
      "power_limit_pct": 100.0,
      "power_limit_enabled": false,
      "controls_updated_at": 1715794200.12,
      "active_override": null
    }
  ],
  "mqtt": {
    "enabled": true,
    "connected": true,
    "broker": "mosquitto",
    "port": 1883,
    "messages_published": 1206,
    "messages_skipped": 1053,
    "disconnection_count": 0
  },
  "influxdb": {
    "enabled": true,
    "connected": true,
    "url": "http://influxdb:8086",
    "bucket": "fronius",
    "writes_total": 47,
    "writes_failed": 0,
    "disconnection_count": 0
  },
  "write": {
    "enabled": true,
    "writes_total": 0,
    "writes_failed": 0,
    "active_limits": {},
    "queue_size": 0,
    "min_power_limit_pct": 10,
    "max_power_limit_pct": 100,
    "rate_limit_seconds": 30,
    "auto_revert_seconds": 3600
  },
  "system": {
    "memory_rss_mb": 72.6,
    "memory_vms_mb": 583.3,
    "threads": 8,
    "pid": 1
  },
  "logs": [
    "2026-05-15 19:30:00 - INFO - …",
    "..."
  ]
}
```

### Inverter-specific fields

| Field | Type | Meaning |
|---|---|---|
| `power_limit_pct` | `float | null` | Last cached read of `WMaxLim_Pct`. `null` if Model 123 hasn't been read yet (e.g. very recent startup or device offline). |
| `power_limit_enabled` | `bool | null` | `WMaxLim_Ena` — `false` is normal when the inverter is at 100 % unconstrained. |
| `controls_updated_at` | `float | null` | Unix timestamp of the last successful Model 123 read for this inverter. Used by the dashboard to render a `(read N s ago)` staleness note when the modal is opened — important during night/sleep mode when the cache is not refreshed. |
| `active_override` | `object | null` | Present only when a non-100 % limit is *currently tracked* by this collector. Cleared on restore. Shape: `{ "limit_pct": 70.0, "set_at": 1715794200.12, "source": "monitoring_ui" }`. |

---

## 4. UI feature reference

### Header
- Version badge, hostname, container type (inverter / meter), uptime,
  auto-refresh status, JSON link.

### Cards (top row)
- **Modbus TCP** — host:port, connection state, successful / failed
  read counters, sleep-mode indicator.
- **MQTT** — broker, connection state, publish / skip counters,
  disconnection count.
- **InfluxDB** — URL, connection state, write counters, disconnection
  count.
- **Modbus Write** *(only when `write.enabled=true`)* — totals, queue
  depth, active limits summary.
- **System** — RSS / VMS memory, thread count, PID.

### Devices table

| Column | Notes |
|---|---|
| ID | Modbus unit id. |
| Type | `inverter` / `meter`. |
| Model | SunSpec common-model `Md` string. |
| Serial | SunSpec common-model `SN`. |
| Status | `online` (green), `offline` (red), or `unknown` (amber, pre-first-poll). |
| Last Seen | `HH:MM:SS` of the last successful poll. |
| Errors | Read error counter across the device's lifetime. |
| **Limit** | Inverters only. Live `power_limit_pct` with colour coding (green ≥ 99.5, amber ≥ 50, red < 50). `OVR` badge when an override is tracked locally. `(disabled)` annotation when `WMaxLim_Ena=0`. |
| **Control** | Inverters only. **Set** button opens the [power limit modal](./POWER_LIMIT_CONTROL.md#3-the-dashboard-modal). Disabled (`disabled` text) when `write.enabled=false`. Always disabled for meters. |

### Log viewer
- Reads the per-device log file (`logs/inverter.log` or `logs/meter.log`).
- Newest first, capped at 200 displayed lines, 500 read, last 24 h.

---

## 5. Operational notes

- The dashboard's auto-refresh is **client-side JavaScript** (was a
  `<meta http-equiv="refresh">` until v1.8.0). It pauses while the
  control modal is open or a `POST` is in flight, so a write in
  progress is never interrupted by a page reload.
- `/api/data` returns a **fresh snapshot on every call** — there is no
  intermediate cache. Each request collects:
  - Modbus / MQTT / InfluxDB live stats from each publisher.
  - Per-device runtime state from `DevicePoller`.
  - Process metrics via `psutil`.
  - Recent log lines from disk.
- The log viewer's "last 24 h" filter is timestamp-based; lines without
  a parseable timestamp (e.g. multi-line tracebacks) are kept verbatim.
- The HTTP server uses `log_level="warning"` and `access_log=False` —
  successful requests are *not* logged. Failed requests still log at
  WARNING. To debug a request, hit `/api/data` and inspect the response.

---

## 6. Integration with pv-stack-ui

`pv-stack-ui` consumes `/api/data` directly (no authentication required
on the internal Docker network). The Control sub-tab in pv-stack-ui
uses the **MQTT path** (see [POWER_LIMIT_CONTROL.md §4](./POWER_LIMIT_CONTROL.md#4-mqtt-command-topics))
rather than the HTTP `POST` endpoints, so that other listeners (Node-RED
OV protection, Home Assistant, etc.) can observe the result stream.

In short:

- **Diagnostics / one-off manual override** → dashboard modal.
- **Automation / control loops** → MQTT.
- **Programmatic state inspection** → `GET /api/data`.
