# Power Limit Control — Reference

> **CAUTION.** This feature writes to inverter Modbus registers (SunSpec
> Model 123 — *Immediate Controls*). Misuse can affect inverter operation
> and energy yield. Writes are **disabled by default**; you must explicitly
> opt in via `write.enabled: true`.

Fronius Modbus MQTT supports limiting an inverter's active-power output
to a percentage of `WMax` (SunSpec) and restoring it to 100%. Three
control surfaces are available — all of them share the same write
pipeline, so safety, rate limiting and read-back verification are
identical regardless of who issued the command.

---

## 1. Control surfaces

| Surface | Best for | Source tag in logs / `active_limits` |
|---|---|---|
| Built-in dashboard modal (`POST /api/inverter/{id}/...`) | Ad-hoc manual control, diagnostics, demos | `monitoring_ui` |
| MQTT command topics (`fronius/inverter/{id}/cmd/...`) | Automation (Node-RED, scripts, Home Assistant, policy engines) | `mqtt` |
| Internal auto-revert / shutdown restore | Safety net — not user-facing | `auto_revert`, `shutdown` |

All three converge on `DevicePoller.queue_power_limit_command()` →
`_execute_power_limit_write()`.

---

## 2. Configuration

```yaml
write:
  enabled: false                # Master switch (default false)
  min_power_limit_pct: 10       # Safety floor — writes below this are rejected
  max_power_limit_pct: 100      # Safety ceiling
  rate_limit_seconds: 30        # Min interval between writes per device
  auto_revert_seconds: 3600     # Global auto-revert to 100% after this many seconds
                                #   (0 = disabled). Applies regardless of any
                                #   inverter-side revert_timeout supplied per command.
  stabilization_delay: 2.0      # Sleep after write before read-back
  command_queue_size: 50        # Shared queue capacity across all inverters
```

Environment variables override the same fields (`WRITE_ENABLED=true`,
`WRITE_MIN_POWER_LIMIT=20`, `WRITE_RATE_LIMIT=60`,
`WRITE_COMMAND_QUEUE_SIZE=100`, etc.).

---

## 3. The dashboard modal

Open the monitoring dashboard (default `http://<host>:8082/` for the
inverter container) and click **Set** in the Control column of any
inverter row.

![Modal screenshot](monitoring-control-modal-zoom.png)

The modal shows:

- **Live readback** — `WMaxLim_Pct` as last read by the poller (Model 123
  is polled every `CONTROLS_POLL_INTERVAL` seconds, but the cache is
  invalidated immediately after every successful write so the next read
  refreshes within one poll cycle).
- **Active override** — present only when a non-100 % limit was written
  by *this process*. Shows the limit value, source (`monitoring_ui`,
  `mqtt`, …) and how long ago it was set. Disappears once the inverter
  is restored to 100 %.
- **Slider + numeric input** — bounded by `write.min_power_limit_pct` and
  `write.max_power_limit_pct`, kept in sync with each other.
- **Advanced**:
  - **Revert timeout (s)** — inverter-side hardware fallback written into
    `WMaxLim_RvrtTms`. Independent from `write.auto_revert_seconds`; the
    software auto-revert in this collector will still fire whether or
    not you set the hardware revert.
  - **Ramp time (s)** — written into `WMaxLim_RmpTms`; the inverter ramps
    to the new limit over this duration.

`Esc` and clicking the backdrop close the modal. Auto-refresh of the
dashboard is paused while the modal is open or a request is in flight,
so dragging the slider won't get interrupted.

### HTTP endpoints behind the modal

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/inverter/{device_id}/power_limit` | `{"limit_pct": <0–100>, "revert_timeout": <0–3600>, "ramp_time": <0–600>}` | `{"status":"queued","device_id":…,"limit_pct":…,…}` |
| POST | `/api/inverter/{device_id}/restore` | (empty) | same shape as above, `limit_pct = 100` |

Status codes:

- `200` — command accepted and queued (no actual write has happened yet — see §5).
- `400` — validation failed in the queue layer (e.g. queue full, range mismatch).
- `403` — `write.enabled=false`.
- `404` — `device_id` is not a configured inverter in this container.
- `422` — Pydantic validation error (out-of-range field, missing field).
- `503` — poller not ready (container still starting).

These endpoints are only registered when `write.enabled: true`. On the
**meter container** they don't exist at all — the meter container has no
inverters in its configuration.

#### curl examples

```bash
# Set inverter 1 to 70 %, no hardware revert, no ramp
curl -X POST http://localhost:8082/api/inverter/1/power_limit \
  -H 'Content-Type: application/json' \
  -d '{"limit_pct": 70}'

# Set with 30-min hardware revert and a 5 s ramp
curl -X POST http://localhost:8082/api/inverter/1/power_limit \
  -H 'Content-Type: application/json' \
  -d '{"limit_pct": 50, "revert_timeout": 1800, "ramp_time": 5}'

# Restore to 100 %
curl -X POST http://localhost:8082/api/inverter/1/restore
```

---

## 4. MQTT command topics

The MQTT control path is unchanged from earlier releases — it is the
right choice for automation that already speaks MQTT.

| Direction | Topic | Payload (JSON) |
|---|---|---|
| In | `fronius/inverter/{id}/cmd/set_power_limit` | `{"limit_pct": 50, "revert_timeout": 0, "ramp_time": 0}` |
| In | `fronius/inverter/{id}/cmd/restore_power_limit` | `{}` |
| Out | `fronius/inverter/{id}/cmd/result` | result object (see below) |

Commands are subscribed at QoS 1. Field names match the HTTP body
exactly. `revert_timeout` and `ramp_time` default to `0` if omitted.

### Result payload

```json
{
  "command": "set_power_limit",
  "device_id": 1,
  "requested_pct": 50.0,
  "source": "mqtt",
  "timestamp": 1715794200.12,
  "status": "success",
  "before": {"limit_pct": 100.0, "enabled": true},
  "after":  {"limit_pct": 50.0,  "enabled": true}
}
```

Possible `status` values:

| Status | Meaning |
|---|---|
| `success` | Write completed and post-read confirmed the target within tolerance. |
| `unverified` | Write completed but the read-back was missing or unreadable — the inverter *probably* accepted the command, but the collector couldn't confirm. |
| `rejected` | Validation failed before the write (range, unknown device, queue full, writes disabled). |
| `rate_limited` | A previous write to the same device happened less than `rate_limit_seconds` ago — the command is re-queued and will retry on the next poll cycle (no result is published for `rate_limited`, the retry is silent). |
| `failed` | Modbus write returned an error. |

### MQTT usage examples

```bash
# Limit inverter 1 to 50 %
mosquitto_pub -h broker -t 'fronius/inverter/1/cmd/set_power_limit' \
  -m '{"limit_pct": 50}'

# Restore
mosquitto_pub -h broker -t 'fronius/inverter/1/cmd/restore_power_limit' -m '{}'

# Observe the result stream
mosquitto_sub -h broker -t 'fronius/inverter/+/cmd/result' -v
```

---

## 5. What happens on every write (the 11-step protocol)

The same procedure runs for every command, no matter the surface:

1. **Rate-limit check** — reject if `rate_limit_seconds` hasn't elapsed
   since the last write to this device (`auto_revert` and `shutdown`
   bypass this check).
2. **Range validation** — `min_power_limit_pct ≤ limit_pct ≤ max_power_limit_pct`.
3. **Connection reset** — force a fresh TCP connection so we don't write
   on top of a stale DataManager buffer.
4. **Pre-read** — read Model 123 to capture the *before* state. The
   scale factor `WMaxLim_Pct_SF` comes from this same read; there is
   no separate Modbus round-trip for it.
5. **Encode** — apply the scale factor to convert `limit_pct` (float)
   into the integer register value the inverter expects.
6. **Write** — encode `WMaxLim_Pct`, `WMaxLim_Ena`, `WMaxLim_WinTms`,
   `WMaxLim_RvrtTms`, `WMaxLim_RmpTms` and issue a single Modbus write.
7. **Stabilization sleep** — `stabilization_delay` seconds.
8. **Post-read** — re-read Model 123.
9. **Read-back verification** — compare the read value against the
   request; tolerate ±1 %.
10. **Tracking update** — `_active_limits[device_id]` records what was
    set; `_last_write_time[device_id]` is bumped for rate limiting;
    the global auto-revert timer is armed (skipped for 100 % writes
    and `auto_revert` commands).
11. **Cache invalidation** — `_last_controls_read[device_id]` is
    cleared so the next poll cycle refreshes Model 123.

The write **shares the polling Modbus connection** — there are no
parallel TCP sessions to the inverter. The poll thread serialises read
and write operations: at most one write is processed per polling cycle
to avoid stalling reads for too long (each write takes ≈ 5 s end to
end).

---

## 6. Auto-revert behaviours

There are **two independent auto-revert mechanisms** — they coexist by
design.

| Mechanism | Triggered by | What it does | Why |
|---|---|---|---|
| Software (`write.auto_revert_seconds`) | This collector, after the configured number of seconds | Queues a `limit_pct=100` command with `source="auto_revert"` | Defence in depth: if the upstream automation crashes or the network drops, the inverter still comes back to 100 % on its own. |
| Hardware (`revert_timeout` per command, written to `WMaxLim_RvrtTms`) | Inverter firmware itself | Inverter internally restores 100 % after the timer | Defence-in-defence: protects against this collector crashing. |

A `limit_pct=100` write **disables** `WMaxLim_Ena` (sets it to 0). This
prevents a known Fronius quirk where the inverter stays in `THROTTLED`
status even after the limit goes back to 100 %.

On **graceful shutdown** the collector iterates `_active_limits` and
issues `restore_power_limit` with `source="shutdown"`, bypassing the
rate limit. SIGTERM / SIGINT both trigger this path.

---

## 7. Observability

### Dashboard

- **Limit column** in the Devices table shows the live `WMaxLim_Pct`
  with colour coding (green ≥ 99.5 %, amber ≥ 50 %, red < 50 %), plus an
  `OVR` badge when an override is tracked locally.
- **Modbus Write card** shows total writes, failed writes, queue depth
  and active limits.

### MQTT

- `fronius/inverter/{id}/controls/power_limit_pct` — live readback,
  republished after every Model 123 poll (auto-detected on Home
  Assistant via discovery as `Power Limit %`).
- `fronius/inverter/{id}/controls/power_limit_enabled` — `WMaxLim_Ena` flag.
- `fronius/inverter/{id}/cmd/result` — command results stream (§4).

### InfluxDB

The same `power_limit_pct` and `power_limit_enabled` fields are written
to the `fronius_inverter` measurement on every poll cycle that reads
Model 123. See [INFLUXDB_SCHEMA.md](../INFLUXDB_SCHEMA.md).

---

## 8. Troubleshooting

**Command appears to succeed but the inverter stays at 100 %.**
Check `power_limit_enabled` after the write — Fronius needs
`WMaxLim_Ena=1` to honour the limit. The collector sets it correctly
for any `limit_pct < 100`. If you see `Ena=0` after a sub-100 % write,
inspect the logs for read-back warnings (status `unverified` usually
points at a transient buffer corruption — retry).

**Status `rate_limited` keeps coming up.**
Either bump `write.rate_limit_seconds` lower, or check whether an
automation upstream is firing duplicate commands (the cause of the
`v1.8.0` queue-size bump). Inspect `writes_total` vs.
`writes_failed` and the queue depth.

**`OVR` badge stuck on after a successful restore.**
`_active_limits` is cleared whenever a write reaches `limit_pct ≥ 100`.
If the badge stays, the last write probably came back `unverified` —
the override is still tracked because the collector isn't certain the
restore landed. Issue a fresh restore.

**HTTP `403 writes disabled in config`.**
Container started without `WRITE_ENABLED=true` (or the YAML key set to
`false`). Set the env var on the inverter container and recreate.

**HTTP `404 unknown inverter id N`.**
The id is not in `devices.inverters` for that container. Check the
container's `service.yaml` / env (`DEVICES_INVERTERS=1,2,3,4`).
