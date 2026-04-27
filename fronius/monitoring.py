"""Built-in HTTP monitoring server for runtime observability.

Provides a FastAPI endpoint that serves:
- HTML dashboard (default) with auto-refresh, dark theme, status indicators
- JSON API (?view=json) for programmatic access (pv-stack-ui integration)

Runs as a daemon thread via uvicorn — auto-dies on process exit.
All data is read on each request (no caching).
"""

import os
import socket
import threading
import time
import logging
from datetime import datetime, timedelta
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from fronius import __version__

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
            data["write"] = {
                "enabled": True,
                "writes_total": ws.get("writes_total", 0),
                "writes_failed": ws.get("writes_failed", 0),
                "active_limits": ws.get("active_limits", {}),
                "queue_size": ws.get("queue_size", 0),
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

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
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
        <tr><th>ID</th><th>Type</th><th>Model</th><th>Serial</th><th>Status</th><th>Last Seen</th><th>Errors</th></tr>
        {dev_rows if dev_rows else '<tr><td colspan="7" style="color:#666;text-align:center">No devices discovered</td></tr>'}
    </table>
</div>

<div class="card">
    <h2>Logs (last 24h, newest first)</h2>
    <div class="logs">
        {log_html if log_html else '<div style="color:#666">No log entries</div>'}
    </div>
</div>
</body>
</html>"""
        return html
