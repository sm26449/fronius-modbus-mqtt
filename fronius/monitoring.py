"""Built-in HTTP monitoring server for runtime observability.

Provides a FastAPI endpoint that serves:
- HTML dashboard (default) with auto-refresh, dark theme, status indicators
- JSON API (?view=json) for programmatic access (pv-stack-ui integration)

Runs as a daemon thread via uvicorn — auto-dies on process exit.
All data is read on each request (no caching).
"""

import json
import os
import socket
import threading
import time
import logging
from datetime import datetime, timedelta
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from fronius import __version__
from fronius.modbus_client import PowerLimitCommand


class PowerLimitRequest(BaseModel):
    """Body for POST /api/inverter/{id}/power_limit."""
    limit_pct: float = Field(..., ge=0, le=100)
    revert_timeout: int = Field(default=0, ge=0, le=3600)
    ramp_time: int = Field(default=0, ge=0, le=600)

logger = logging.getLogger("fronius_modbus_mqtt")


class MonitoringServer:
    """HTTP monitoring server running in a daemon thread."""

    def __init__(self, app_ref: Any, port: int = 8080):
        self._app = app_ref
        self._port = port
        self._server = None

    def start(self):
        """Create FastAPI app, register route, start uvicorn in daemon thread."""
        api = FastAPI(title="Fronius Modbus MQTT Monitor", docs_url=None, redoc_url=None)

        @api.get("/")
        async def index(request: Request):
            data = self._collect_data()
            if request.query_params.get("view") == "json":
                return JSONResponse(data)
            return HTMLResponse(self._render_html(data))

        @api.get("/api/data")
        async def api_data():
            return JSONResponse(self._collect_data())

        # Control endpoints only registered when writes are enabled in config.
        # This keeps the meter container (which has no inverters) and any
        # read-only deployment safely unable to issue commands.
        write_cfg = self._app.config.write
        if write_cfg and write_cfg.enabled:
            @api.post("/api/inverter/{device_id}/power_limit")
            async def set_power_limit(device_id: int, body: PowerLimitRequest):
                return self._issue_power_limit(
                    device_id=device_id,
                    limit_pct=body.limit_pct,
                    revert_timeout=body.revert_timeout,
                    ramp_time=body.ramp_time,
                )

            @api.post("/api/inverter/{device_id}/restore")
            async def restore_power_limit(device_id: int):
                return self._issue_power_limit(
                    device_id=device_id,
                    limit_pct=100.0,
                    revert_timeout=0,
                    ramp_time=0,
                )

        config = uvicorn.Config(
            api,
            host="0.0.0.0",
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None

        thread = threading.Thread(target=self._server.run, name="monitoring-http", daemon=True)
        thread.start()
        logger.info(f"Monitoring server started on port {self._port}")

    def _issue_power_limit(
        self,
        device_id: int,
        limit_pct: float,
        revert_timeout: int,
        ramp_time: int,
    ) -> JSONResponse:
        """Validate and enqueue a power limit command from the monitoring UI.

        Returns a JSON payload with status and reason. Never raises — always
        returns a structured response so the UI can render an actionable
        message.
        """
        app = self._app
        write_cfg = app.config.write
        if not write_cfg or not write_cfg.enabled:
            return JSONResponse(
                {"status": "rejected", "reason": "writes disabled in config"},
                status_code=403,
            )

        if not app.modbus_client or not app.modbus_client.device_poller:
            return JSONResponse(
                {"status": "rejected", "reason": "poller not ready"},
                status_code=503,
            )

        # Validate the device is an inverter known to this container.
        inv_ids = {inv.get("device_id") for inv in app.modbus_client.inverters}
        if device_id not in inv_ids:
            return JSONResponse(
                {"status": "rejected", "reason": f"unknown inverter id {device_id}"},
                status_code=404,
            )

        cmd = PowerLimitCommand(
            device_id=device_id,
            limit_pct=float(limit_pct),
            revert_timeout=int(revert_timeout),
            ramp_time=int(ramp_time),
            source="monitoring_ui",
        )

        try:
            queued = app.modbus_client.device_poller.queue_power_limit_command(cmd)
        except Exception as e:
            logger.exception(f"Monitoring UI: error queuing command for inverter {device_id}")
            return JSONResponse(
                {"status": "error", "reason": str(e)},
                status_code=500,
            )

        if not queued:
            return JSONResponse(
                {"status": "rejected", "reason": "validation failed or queue full"},
                status_code=400,
            )

        logger.info(
            f"Monitoring UI: queued power limit for inverter {device_id} "
            f"({limit_pct}%, revert={revert_timeout}s, ramp={ramp_time}s)"
        )
        return JSONResponse({
            "status": "queued",
            "device_id": device_id,
            "limit_pct": float(limit_pct),
            "revert_timeout": int(revert_timeout),
            "ramp_time": int(ramp_time),
        })

    def _collect_data(self) -> dict:
        """Collect all runtime state from app components."""
        app = self._app
        now = time.time()
        uptime_seconds = int(now - app._start_time)

        # App info
        data = {
            "app": {
                "version": __version__,
                "uptime_seconds": uptime_seconds,
                "uptime_formatted": app._format_uptime(),
                "container_type": app.device_filter,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "modbus": {},
            "devices": [],
            "mqtt": {},
            "influxdb": {},
            "write": {},
            "system": {},
            "logs": [],
        }

        # Modbus
        if app.modbus_client:
            modbus_stats = app.modbus_client.get_stats()
            poller_status = {}
            if app.modbus_client.device_poller:
                poller_status = app.modbus_client.device_poller.get_status()

            data["modbus"] = {
                "host": app.config.modbus.host,
                "port": app.config.modbus.port,
                "connected": poller_status.get("connected", False),
                "in_sleep_mode": poller_status.get("in_sleep_mode", False),
                "is_night_time": poller_status.get("is_night_time", False),
                "successful_reads": modbus_stats.get("successful_reads", 0),
                "failed_reads": modbus_stats.get("failed_reads", 0),
            }

            # Devices
            runtime_stats = {}
            if app.modbus_client.device_poller:
                runtime_stats = app.modbus_client.device_poller.get_runtime_stats()

            devices_runtime = runtime_stats.get("devices", {})

            for inv in app.modbus_client.inverters:
                dev_id = inv.get("device_id", 0)
                rt = devices_runtime.get(f"inverter_{dev_id}", {})
                latest_ctrls = app.modbus_client.device_poller.get_latest_controls(dev_id) or {}
                active_override = app.modbus_client.device_poller.get_active_override(dev_id)
                data["devices"].append({
                    "device_id": dev_id,
                    "device_type": "inverter",
                    "manufacturer": inv.get("manufacturer", ""),
                    "model": inv.get("model", ""),
                    "serial_number": inv.get("serial_number", ""),
                    "model_id": inv.get("model_id"),
                    "status": rt.get("status", "unknown"),
                    "last_seen": rt.get("last_seen"),
                    "read_errors": rt.get("read_errors", 0),
                    "consecutive_errors": rt.get("consecutive_errors", 0),
                    "power_limit_pct": latest_ctrls.get("power_limit_pct"),
                    "power_limit_enabled": latest_ctrls.get("power_limit_enabled"),
                    "controls_updated_at": latest_ctrls.get("updated_at"),
                    "active_override": active_override,
                })

            for mtr in app.modbus_client.meters:
                dev_id = mtr.get("device_id", 0)
                rt = devices_runtime.get(f"meter_{dev_id}", {})
                data["devices"].append({
                    "device_id": dev_id,
                    "device_type": "meter",
                    "manufacturer": mtr.get("manufacturer", ""),
                    "model": mtr.get("model", ""),
                    "serial_number": mtr.get("serial_number", ""),
                    "model_id": mtr.get("model_id"),
                    "status": rt.get("status", "unknown"),
                    "last_seen": rt.get("last_seen"),
                    "read_errors": rt.get("read_errors", 0),
                    "consecutive_errors": rt.get("consecutive_errors", 0),
                })

        # MQTT
        if app.mqtt_publisher:
            mqtt_stats = app.mqtt_publisher.get_stats()
            data["mqtt"] = {
                "enabled": mqtt_stats.get("enabled", False),
                "connected": mqtt_stats.get("connected", False),
                "broker": mqtt_stats.get("broker", ""),
                "port": mqtt_stats.get("port", 0),
                "messages_published": mqtt_stats.get("messages_published", 0),
                "messages_skipped": mqtt_stats.get("messages_skipped", 0),
                "disconnection_count": mqtt_stats.get("disconnection_count", 0),
            }
        else:
            data["mqtt"] = {"enabled": app.config.mqtt.enabled, "connected": False}

        # InfluxDB
        if app.influxdb_publisher:
            idb_stats = app.influxdb_publisher.get_stats()
            data["influxdb"] = {
                "enabled": idb_stats.get("enabled", False),
                "connected": idb_stats.get("connected", False),
                "url": idb_stats.get("url", ""),
                "bucket": idb_stats.get("bucket", ""),
                "writes_total": idb_stats.get("writes_total", 0),
                "writes_failed": idb_stats.get("writes_failed", 0),
                "disconnection_count": idb_stats.get("disconnection_count", 0),
            }
        else:
            data["influxdb"] = {
                "enabled": app.config.influxdb.enabled, "connected": False
            }

        # Write stats
        write_enabled = bool(app.config.write and app.config.write.enabled)
        if write_enabled and app.modbus_client and app.modbus_client.device_poller:
            ws = app.modbus_client.device_poller.get_write_stats()
            wc = app.config.write
            data["write"] = {
                "enabled": True,
                "writes_total": ws.get("writes_total", 0),
                "writes_failed": ws.get("writes_failed", 0),
                "active_limits": ws.get("active_limits", {}),
                "queue_size": ws.get("queue_size", 0),
                "min_power_limit_pct": wc.min_power_limit_pct,
                "max_power_limit_pct": wc.max_power_limit_pct,
                "rate_limit_seconds": wc.rate_limit_seconds,
                "auto_revert_seconds": getattr(wc, "auto_revert_seconds", 0),
            }
        else:
            data["write"] = {"enabled": write_enabled}

        # System
        try:
            proc = psutil.Process(os.getpid())
            mem = proc.memory_info()
            data["system"] = {
                "memory_rss_mb": round(mem.rss / 1024 / 1024, 1),
                "memory_vms_mb": round(mem.vms / 1024 / 1024, 1),
                "threads": proc.num_threads(),
                "pid": os.getpid(),
            }
        except Exception:
            data["system"] = {"pid": os.getpid()}

        # Logs
        data["logs"] = self._read_logs()

        return data

    def _read_logs(self, max_lines: int = 500, max_age_hours: int = 24) -> list:
        """Read recent log lines from the log file."""
        log_file = self._app.config.general.log_file
        if not log_file:
            # Derive from device_filter like the main app does
            return []

        # Apply same device-specific path logic as main app
        if self._app.device_filter != "all":
            from pathlib import Path
            log_path = Path(log_file)
            log_file = str(log_path.parent / f"{self._app.device_filter}.log")

        if not os.path.exists(log_file):
            return []

        try:
            cutoff = datetime.now() - timedelta(hours=max_age_hours)
            lines = []
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                # Read all lines and take the last max_lines
                all_lines = f.readlines()
                recent = all_lines[-max_lines:] if len(all_lines) > max_lines else all_lines

            for line in recent:
                line = line.rstrip("\n")
                if not line:
                    continue
                # Filter by age: parse timestamp from "2026-04-27 18:16:36 - ..."
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if ts < cutoff:
                        continue
                except (ValueError, IndexError):
                    pass  # Include lines we can't parse
                lines.append(line)

            return lines
        except Exception:
            return []

    def _render_html(self, data: dict) -> str:
        """Render HTML dashboard with inline CSS."""
        app_data = data["app"]
        modbus = data["modbus"]
        mqtt = data["mqtt"]
        influxdb = data["influxdb"]
        write = data["write"]
        system = data["system"]
        devices = data["devices"]
        logs = data["logs"]

        def dot(connected):
            color = "#4caf50" if connected else "#f44336"
            return f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};flex-shrink:0"></span>'

        def val(v):
            if v is None:
                return '<span style="color:#666">—</span>'
            return str(v)

        def fmt_ts(iso_str):
            """Format ISO timestamp to readable HH:MM:SS."""
            if not iso_str or iso_str == "—":
                return "—"
            try:
                # Parse "2026-04-27T19:49:30.387639" → "19:49:30"
                dt = datetime.fromisoformat(iso_str)
                return dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                return str(iso_str)

        write_enabled = bool(write.get("enabled"))

        def fmt_limit_cell(d):
            """Render the Limit column for a device row."""
            if d.get("device_type") != "inverter":
                return '<span style="color:#666">—</span>'
            pct = d.get("power_limit_pct")
            enabled = d.get("power_limit_enabled")
            override = d.get("active_override")

            if pct is None:
                return '<span style="color:#666">—</span>'

            color = "#4caf50" if pct >= 99.5 else "#ff9800" if pct >= 50 else "#e94560"
            disabled_note = ""
            if enabled is False:
                disabled_note = ' <span title="WMaxLim_Ena=0" style="color:#888;font-size:0.75em">(disabled)</span>'
            override_badge = ""
            if override:
                src = override.get("source", "?")
                override_badge = (
                    f' <span class="badge" title="Override by {src}">'
                    f'OVR</span>'
                )
            return (
                f'<span style="color:{color};font-weight:bold">{pct:.0f}%</span>'
                f'{override_badge}{disabled_note}'
            )

        def fmt_control_cell(d):
            """Render the Control column."""
            if d.get("device_type") != "inverter":
                return '<span style="color:#666">—</span>'
            if not write_enabled:
                return '<span title="Writes disabled in config" style="color:#666">disabled</span>'
            return (
                f'<button class="ctrl-btn" data-device-id="{d["device_id"]}" '
                f'data-current-pct="{d.get("power_limit_pct") if d.get("power_limit_pct") is not None else ""}" '
                f'type="button">Set</button>'
            )

        # Devices table rows
        dev_rows = ""
        for d in devices:
            status_color = "#4caf50" if d["status"] == "online" else "#f44336" if d["status"] == "offline" else "#ff9800"
            last_seen = fmt_ts(d.get("last_seen"))
            dev_rows += f"""<tr>
                <td>{d['device_id']}</td>
                <td>{d['device_type']}</td>
                <td>{val(d.get('model'))}</td>
                <td>{val(d.get('serial_number'))}</td>
                <td><span style="color:{status_color};font-weight:bold">{d['status']}</span></td>
                <td>{last_seen}</td>
                <td>{d.get('read_errors', 0)}</td>
                <td>{fmt_limit_cell(d)}</td>
                <td>{fmt_control_cell(d)}</td>
            </tr>"""

        # Write section
        write_html = ""
        if write.get("enabled"):
            active = write.get("active_limits", {})
            active_str = ", ".join(f"dev {k}: {v}" for k, v in active.items()) if active else "none"
            write_html = f"""
            <div class="card">
                <h2>Modbus Write</h2>
                <div class="grid">
                    <div class="stat"><span class="label">Writes</span><span class="value">{write.get('writes_total', 0)}</span></div>
                    <div class="stat"><span class="label">Failed</span><span class="value">{write.get('writes_failed', 0)}</span></div>
                    <div class="stat"><span class="label">Queue</span><span class="value">{write.get('queue_size', 0)}</span></div>
                </div>
                <p style="margin-top:8px;color:#aaa">Active limits: {active_str}</p>
            </div>"""

        # Log lines
        log_html = "\n".join(
            f'<div class="log-line">{line}</div>' for line in reversed(logs[-200:])
        )

        # Payload consumed by the control modal JS. Embedded as a JSON island so
        # we don't have to fight f-string brace escaping inside the JS source.
        inv_payload = {}
        for d in devices:
            if d.get("device_type") != "inverter":
                continue
            inv_payload[str(d["device_id"])] = {
                "power_limit_pct": d.get("power_limit_pct"),
                "power_limit_enabled": d.get("power_limit_enabled"),
                "controls_updated_at": d.get("controls_updated_at"),
                "active_override": d.get("active_override"),
            }
        monitor_payload = json.dumps({
            "write": {
                "enabled": bool(write.get("enabled")),
                "min_pct": write.get("min_power_limit_pct", 10.0),
                "max_pct": write.get("max_power_limit_pct", 100.0),
                "rate_limit_seconds": write.get("rate_limit_seconds", 30),
                "auto_revert_seconds": write.get("auto_revert_seconds", 0),
            },
            "inverters": inv_payload,
        # Defence in depth: neutralise any `</script>` sequence that could
        # appear in future-added string fields and break out of the JSON island.
        }).replace("</", "<\\/")

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fronius Monitor — {app_data['container_type']}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#1a1a2e; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace; padding:16px; }}
h1 {{ color:#e94560; margin-bottom:4px; font-size:1.4em; }}
h2 {{ color:#0f3460; background:#16213e; padding:8px 12px; border-radius:6px 6px 0 0; margin:-16px -16px 12px -16px; font-size:1em; color:#e94560; }}
.header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:8px; }}
.header-right {{ color:#888; font-size:0.85em; }}
.header-right a {{ color:#e94560; text-decoration:none; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:12px; margin-bottom:16px; }}
.card {{ background:#16213e; border-radius:8px; padding:16px; border:1px solid #0f3460; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); gap:8px; }}
.stat {{ display:flex; flex-direction:column; overflow:hidden; min-width:0; }}
.stat-wide {{ grid-column: 1 / -1; }}
.label {{ font-size:0.75em; color:#888; text-transform:uppercase; }}
.value {{ font-size:1.1em; font-weight:bold; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.value-status {{ display:flex; align-items:center; gap:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:0.9em; }}
th {{ text-align:left; padding:6px 8px; border-bottom:2px solid #0f3460; color:#888; font-size:0.8em; text-transform:uppercase; }}
td {{ padding:6px 8px; border-bottom:1px solid #0f3460; }}
.logs {{ background:#0d1117; border-radius:8px; padding:12px; max-height:700px; overflow-y:auto; font-size:0.8em; font-family:monospace; border:1px solid #0f3460; }}
.log-line {{ padding:1px 0; white-space:pre-wrap; word-break:break-all; }}
.sleep {{ color:#ff9800; }}
.ctrl-btn {{ background:#0f3460; color:#e94560; border:1px solid #e94560; border-radius:4px; padding:4px 10px; font-size:0.8em; font-weight:bold; cursor:pointer; font-family:inherit; }}
.ctrl-btn:hover {{ background:#e94560; color:#fff; }}
.ctrl-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.badge {{ display:inline-block; background:#e94560; color:#fff; border-radius:3px; padding:1px 5px; font-size:0.65em; font-weight:bold; vertical-align:middle; letter-spacing:0.5px; }}
.modal {{ position:fixed; inset:0; display:flex; align-items:center; justify-content:center; z-index:1000; padding:16px; }}
.modal[hidden] {{ display:none; }}
.modal-backdrop {{ position:absolute; inset:0; background:rgba(0,0,0,0.65); backdrop-filter:blur(2px); }}
.modal-content {{ position:relative; background:#16213e; border:1px solid #0f3460; border-radius:8px; padding:20px; width:100%; max-width:480px; box-shadow:0 10px 40px rgba(0,0,0,0.5); }}
.modal-content h2 {{ background:none; padding:0; margin:0 0 12px 0; font-size:1.1em; }}
.modal-section {{ background:#0d1117; border-radius:6px; padding:10px 12px; margin-bottom:12px; font-size:0.85em; }}
.modal-section .row {{ display:flex; justify-content:space-between; padding:2px 0; }}
.modal-section .row span:first-child {{ color:#888; }}
.modal-section .row span:last-child {{ font-weight:bold; }}
.form-row {{ margin-bottom:12px; }}
.form-row label {{ display:block; color:#aaa; font-size:0.8em; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.5px; }}
.slider-row {{ display:flex; gap:10px; align-items:center; }}
.slider-row input[type=range] {{ flex:1; accent-color:#e94560; }}
.slider-row input[type=number] {{ width:70px; }}
input[type=number] {{ background:#0d1117; color:#e0e0e0; border:1px solid #0f3460; border-radius:4px; padding:6px 8px; font-family:inherit; font-size:0.9em; }}
input[type=number]:focus {{ outline:none; border-color:#e94560; }}
details.advanced {{ background:#0d1117; border-radius:6px; padding:8px 12px; margin-bottom:12px; }}
details.advanced summary {{ cursor:pointer; color:#aaa; font-size:0.85em; user-select:none; }}
details.advanced[open] summary {{ margin-bottom:8px; }}
.modal-actions {{ display:flex; gap:8px; justify-content:flex-end; margin-top:8px; }}
.btn {{ border:none; border-radius:4px; padding:8px 16px; font-family:inherit; font-size:0.9em; font-weight:bold; cursor:pointer; }}
.btn:disabled {{ opacity:0.5; cursor:wait; }}
.btn-primary {{ background:#e94560; color:#fff; }}
.btn-primary:hover:not(:disabled) {{ background:#ff5470; }}
.btn-warning {{ background:#0f3460; color:#ff9800; border:1px solid #ff9800; }}
.btn-warning:hover:not(:disabled) {{ background:#ff9800; color:#16213e; }}
.btn-ghost {{ background:transparent; color:#888; }}
.btn-ghost:hover:not(:disabled) {{ color:#e0e0e0; }}
.modal-msg {{ padding:8px 10px; border-radius:4px; font-size:0.85em; margin-bottom:10px; }}
.modal-msg[hidden] {{ display:none; }}
.modal-msg.success {{ background:rgba(76,175,80,0.15); color:#4caf50; border:1px solid #4caf50; }}
.modal-msg.error {{ background:rgba(244,67,54,0.15); color:#f44336; border:1px solid #f44336; }}
.toast {{ position:fixed; bottom:20px; right:20px; background:#16213e; border:1px solid #0f3460; border-left:4px solid #4caf50; color:#e0e0e0; padding:12px 18px; border-radius:6px; box-shadow:0 4px 20px rgba(0,0,0,0.4); z-index:2000; font-size:0.9em; max-width:360px; }}
.toast.error {{ border-left-color:#f44336; }}
.toast[hidden] {{ display:none; }}
</style>
</head>
<body>
<div class="header">
    <div>
        <h1>Fronius Modbus MQTT <span style="font-size:0.7em;color:#888">v{app_data['version']}</span></h1>
        <span style="color:#888;font-size:0.85em">{app_data['hostname']} &middot; {app_data['container_type']} &middot; uptime {app_data['uptime_formatted']}</span>
    </div>
    <div class="header-right">
        {app_data['timestamp']} &middot; <a href="?view=json">JSON</a> &middot; auto-refresh 30s
    </div>
</div>

<div class="cards">
    <div class="card">
        <h2>Modbus TCP</h2>
        <div class="grid">
            <div class="stat stat-wide"><span class="label">Host</span><span class="value">{modbus.get('host', '—')}:{modbus.get('port', '—')}</span></div>
            <div class="stat"><span class="label">Status</span><span class="value value-status">{dot(modbus.get('connected'))}{('sleep' if modbus.get('in_sleep_mode') else 'connected' if modbus.get('connected') else 'disconnected')}</span></div>
            <div class="stat"><span class="label">Reads OK</span><span class="value">{modbus.get('successful_reads', 0)}</span></div>
            <div class="stat"><span class="label">Reads Fail</span><span class="value">{modbus.get('failed_reads', 0)}</span></div>
        </div>
    </div>

    <div class="card">
        <h2>MQTT</h2>
        <div class="grid">
            <div class="stat stat-wide"><span class="label">Broker</span><span class="value">{mqtt.get('broker', '—')}:{mqtt.get('port', '—')}</span></div>
            <div class="stat"><span class="label">Status</span><span class="value value-status">{dot(mqtt.get('connected'))}{('connected' if mqtt.get('connected') else 'disconnected')}</span></div>
            <div class="stat"><span class="label">Published</span><span class="value">{mqtt.get('messages_published', 0)}</span></div>
            <div class="stat"><span class="label">Skipped</span><span class="value">{mqtt.get('messages_skipped', 0)}</span></div>
            <div class="stat"><span class="label">Disconnections</span><span class="value">{mqtt.get('disconnection_count', 0)}</span></div>
        </div>
    </div>

    <div class="card">
        <h2>InfluxDB</h2>
        <div class="grid">
            <div class="stat stat-wide"><span class="label">URL</span><span class="value">{influxdb.get('url', '—') or '—'}</span></div>
            <div class="stat"><span class="label">Status</span><span class="value value-status">{dot(influxdb.get('connected'))}{('connected' if influxdb.get('connected') else 'disabled' if not influxdb.get('enabled') else 'disconnected')}</span></div>
            <div class="stat"><span class="label">Writes</span><span class="value">{influxdb.get('writes_total', 0)}</span></div>
            <div class="stat"><span class="label">Failed</span><span class="value">{influxdb.get('writes_failed', 0)}</span></div>
            <div class="stat"><span class="label">Disconnections</span><span class="value">{influxdb.get('disconnection_count', 0)}</span></div>
        </div>
    </div>

    {write_html}

    <div class="card">
        <h2>System</h2>
        <div class="grid">
            <div class="stat"><span class="label">Memory RSS</span><span class="value">{system.get('memory_rss_mb', '—')} MB</span></div>
            <div class="stat"><span class="label">Memory VMS</span><span class="value">{system.get('memory_vms_mb', '—')} MB</span></div>
            <div class="stat"><span class="label">Threads</span><span class="value">{system.get('threads', '—')}</span></div>
            <div class="stat"><span class="label">PID</span><span class="value">{system.get('pid', '—')}</span></div>
        </div>
    </div>
</div>

<div class="card" style="margin-bottom:16px">
    <h2>Devices ({len(devices)})</h2>
    <table>
        <tr><th>ID</th><th>Type</th><th>Model</th><th>Serial</th><th>Status</th><th>Last Seen</th><th>Errors</th><th>Limit</th><th>Control</th></tr>
        {dev_rows if dev_rows else '<tr><td colspan="9" style="color:#666;text-align:center">No devices discovered</td></tr>'}
    </table>
</div>

<div class="card">
    <h2>Logs (last 24h, newest first)</h2>
    <div class="logs">
        {log_html if log_html else '<div style="color:#666">No log entries</div>'}
    </div>
</div>

<div id="ctrlModal" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="ctrlModalTitle">
    <div class="modal-backdrop" data-close></div>
    <div class="modal-content">
        <h2 id="ctrlModalTitle">Set Power Limit — Inverter <span id="ctrlDevId"></span></h2>
        <div class="modal-section">
            <div class="row"><span>Live readback</span><span id="ctrlLiveValue">—</span></div>
            <div class="row" id="ctrlOverrideRow" hidden><span>Active override</span><span id="ctrlOverrideValue">—</span></div>
        </div>
        <div class="form-row">
            <label for="ctrlSlider">Power limit (%)</label>
            <div class="slider-row">
                <input type="range" id="ctrlSlider" min="10" max="100" value="100" step="1">
                <input type="number" id="ctrlSliderNum" min="10" max="100" value="100" step="1">
            </div>
        </div>
        <details class="advanced">
            <summary>Advanced — auto-revert timer &amp; ramp</summary>
            <div class="form-row" style="margin-top:8px">
                <label for="ctrlRevert">Revert timeout (s) — 0 = no inverter-side auto-revert</label>
                <input type="number" id="ctrlRevert" min="0" max="3600" value="0" step="1">
            </div>
            <div class="form-row" style="margin-bottom:4px">
                <label for="ctrlRamp">Ramp time (s) — gradual transition</label>
                <input type="number" id="ctrlRamp" min="0" max="600" value="0" step="1">
            </div>
            <div id="ctrlAutoRevertHint" style="color:#888;font-size:0.75em;margin-top:6px"></div>
        </details>
        <div id="ctrlMessage" class="modal-msg" hidden></div>
        <div class="modal-actions">
            <button id="ctrlCancel" class="btn btn-ghost" type="button" data-close>Cancel</button>
            <button id="ctrlRestore" class="btn btn-warning" type="button">Restore 100%</button>
            <button id="ctrlApply" class="btn btn-primary" type="button">Apply</button>
        </div>
    </div>
</div>

<div id="toast" class="toast" hidden></div>

<script id="monitor-data" type="application/json">{monitor_payload}</script>
<script>
(function() {{
    var data;
    try {{
        data = JSON.parse(document.getElementById('monitor-data').textContent);
    }} catch (e) {{
        console.error('Failed to parse monitor payload', e);
        data = {{ write: {{ enabled: false }}, inverters: {{}} }};
    }}

    var wcfg = data.write || {{}};
    var modal = document.getElementById('ctrlModal');
    var slider = document.getElementById('ctrlSlider');
    var sliderNum = document.getElementById('ctrlSliderNum');
    var revertInput = document.getElementById('ctrlRevert');
    var rampInput = document.getElementById('ctrlRamp');
    var devIdSpan = document.getElementById('ctrlDevId');
    var liveSpan = document.getElementById('ctrlLiveValue');
    var ovrRow = document.getElementById('ctrlOverrideRow');
    var ovrSpan = document.getElementById('ctrlOverrideValue');
    var msgBox = document.getElementById('ctrlMessage');
    var applyBtn = document.getElementById('ctrlApply');
    var restoreBtn = document.getElementById('ctrlRestore');
    var cancelBtn = document.getElementById('ctrlCancel');
    var autoRevertHint = document.getElementById('ctrlAutoRevertHint');
    var toast = document.getElementById('toast');
    var currentDeviceId = null;
    var inFlight = false;

    if (slider && wcfg.enabled) {{
        slider.min = wcfg.min_pct;
        slider.max = wcfg.max_pct;
        sliderNum.min = wcfg.min_pct;
        sliderNum.max = wcfg.max_pct;
    }}

    if (autoRevertHint && wcfg.auto_revert_seconds) {{
        autoRevertHint.textContent = 'Note: a global auto-revert to 100% will fire after ' +
            wcfg.auto_revert_seconds + 's regardless of the revert timeout above.';
    }}

    function showToast(msg, type) {{
        toast.textContent = msg;
        toast.className = 'toast' + (type === 'error' ? ' error' : '');
        toast.hidden = false;
        setTimeout(function() {{ toast.hidden = true; }}, 4000);
    }}

    function setMsg(text, type) {{
        if (!text) {{ msgBox.hidden = true; return; }}
        msgBox.textContent = text;
        msgBox.className = 'modal-msg ' + (type || 'success');
        msgBox.hidden = false;
    }}

    function syncSlider(src) {{
        var v = parseInt(src.value, 10);
        if (isNaN(v)) v = 100;
        if (v < parseInt(slider.min, 10)) v = parseInt(slider.min, 10);
        if (v > parseInt(slider.max, 10)) v = parseInt(slider.max, 10);
        slider.value = v;
        sliderNum.value = v;
    }}
    slider.addEventListener('input', function() {{ syncSlider(slider); }});
    sliderNum.addEventListener('input', function() {{ syncSlider(sliderNum); }});

    function renderInverterState(inv) {{
        var live = inv.power_limit_pct;
        var staleNote = '';
        if (inv.controls_updated_at) {{
            var ageS = Math.max(0, Math.round(Date.now() / 1000 - inv.controls_updated_at));
            if (ageS >= 60) staleNote = ' (read ' + Math.round(ageS / 60) + 'm ago)';
            else if (ageS >= 5) staleNote = ' (read ' + ageS + 's ago)';
        }}
        liveSpan.textContent = (live == null) ? '— (not yet read)' :
            (Math.round(live * 10) / 10) + '%' +
            (inv.power_limit_enabled === false ? ' (WMaxLim_Ena=0)' : '') +
            staleNote;
        if (inv.active_override) {{
            var ovr = inv.active_override;
            var ageSec = ovr.set_at ? Math.max(0, Math.round(Date.now() / 1000 - ovr.set_at)) : null;
            ovrSpan.textContent = ovr.limit_pct + '% via ' + ovr.source +
                (ageSec != null ? ' (' + ageSec + 's ago)' : '');
            ovrRow.hidden = false;
        }} else {{
            ovrRow.hidden = true;
        }}
    }}

    function openModal(devId) {{
        currentDeviceId = devId;
        devIdSpan.textContent = devId;
        var inv = (data.inverters || {{}})[String(devId)] || {{}};
        renderInverterState(inv);
        var initial = (inv.power_limit_pct != null) ? Math.round(inv.power_limit_pct) : 100;
        slider.value = initial;
        sliderNum.value = initial;
        revertInput.value = 0;
        rampInput.value = 0;
        setMsg(null);
        applyBtn.disabled = false;
        restoreBtn.disabled = false;
        modal.hidden = false;
        setTimeout(function() {{ sliderNum.focus(); sliderNum.select(); }}, 50);

        // Refresh live + override values in case page payload is stale (modal
        // could have been opened minutes after page load).
        fetch('/api/data').then(function(r) {{ return r.json(); }}).then(function(d) {{
            if (currentDeviceId !== devId) return;  // user moved on
            var freshInv = null;
            for (var i = 0; i < (d.devices || []).length; i++) {{
                var dev = d.devices[i];
                if (dev.device_type === 'inverter' && String(dev.device_id) === String(devId)) {{
                    freshInv = dev; break;
                }}
            }}
            if (freshInv) {{
                data.inverters[String(devId)] = freshInv;
                renderInverterState(freshInv);
            }}
        }}).catch(function() {{ /* ignore refresh failure — modal still usable */ }});
    }}

    function closeModal() {{
        modal.hidden = true;
        currentDeviceId = null;
        setMsg(null);
    }}

    document.querySelectorAll('.ctrl-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
            openModal(btn.dataset.deviceId);
        }});
    }});

    modal.querySelectorAll('[data-close]').forEach(function(el) {{
        el.addEventListener('click', function() {{ if (!inFlight) closeModal(); }});
    }});

    document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape' && !modal.hidden && !inFlight) closeModal();
    }});

    function postJSON(url, body) {{
        return fetch(url, {{
            method: 'POST',
            headers: body ? {{ 'Content-Type': 'application/json' }} : {{}},
            body: body ? JSON.stringify(body) : undefined,
        }}).then(function(r) {{
            return r.json().then(function(j) {{ return {{ ok: r.ok, status: r.status, body: j }}; }});
        }});
    }}

    function extractReason(res) {{
        var b = res.body;
        if (!b) return 'HTTP ' + res.status;
        if (b.reason) return b.reason;
        // FastAPI/Pydantic 422 returns a `detail` array of validation issues.
        if (Array.isArray(b.detail) && b.detail.length) {{
            return b.detail.map(function(d) {{
                var loc = Array.isArray(d.loc) ? d.loc.slice(-1)[0] : '';
                return (loc ? loc + ': ' : '') + (d.msg || JSON.stringify(d));
            }}).join('; ');
        }}
        if (typeof b.detail === 'string') return b.detail;
        return 'HTTP ' + res.status;
    }}

    function handleResult(res, successVerb) {{
        if (res.ok && res.body.status === 'queued') {{
            setMsg('Command queued: ' + successVerb + ' to ' + res.body.limit_pct + '%. Reloading…', 'success');
            showToast('Command queued — inverter ' + res.body.device_id + ' → ' + res.body.limit_pct + '%');
            setTimeout(function() {{ location.reload(); }}, 1500);
        }} else {{
            var reason = extractReason(res);
            setMsg('Rejected: ' + reason, 'error');
            showToast('Command rejected: ' + reason, 'error');
            applyBtn.disabled = false;
            restoreBtn.disabled = false;
            inFlight = false;
        }}
    }}

    applyBtn.addEventListener('click', function() {{
        if (inFlight || currentDeviceId == null) return;
        inFlight = true;
        applyBtn.disabled = true;
        restoreBtn.disabled = true;
        setMsg('Sending…', 'success');
        var pct = parseFloat(sliderNum.value);
        var body = {{
            limit_pct: pct,
            revert_timeout: parseInt(revertInput.value, 10) || 0,
            ramp_time: parseInt(rampInput.value, 10) || 0,
        }};
        postJSON('/api/inverter/' + currentDeviceId + '/power_limit', body)
            .then(function(res) {{ handleResult(res, 'set limit'); }})
            .catch(function(err) {{
                setMsg('Network error: ' + err, 'error');
                applyBtn.disabled = false;
                restoreBtn.disabled = false;
                inFlight = false;
            }});
    }});

    restoreBtn.addEventListener('click', function() {{
        if (inFlight || currentDeviceId == null) return;
        inFlight = true;
        applyBtn.disabled = true;
        restoreBtn.disabled = true;
        setMsg('Sending restore…', 'success');
        postJSON('/api/inverter/' + currentDeviceId + '/restore', null)
            .then(function(res) {{ handleResult(res, 'restore'); }})
            .catch(function(err) {{
                setMsg('Network error: ' + err, 'error');
                applyBtn.disabled = false;
                restoreBtn.disabled = false;
                inFlight = false;
            }});
    }});

    // Auto-refresh every 30s, but only while modal is closed and no request is in flight
    setInterval(function() {{
        if (modal.hidden && !inFlight) location.reload();
    }}, 30000);
}})();
</script>
</body>
</html>"""
        return html
