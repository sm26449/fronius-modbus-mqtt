"""Monitoring-UI (8082) power-limit reroute tests (OV-redesign A2).

The 8082 dashboard used to enqueue a Modbus command directly with
``source="monitoring_ui"`` — which the single-writer guard now refuses
("validation failed or queue full"). It must instead publish a retained
``pv-stack/nodered/ov/<id>/manual_floor`` so the OV node (the sole writer)
folds ``min(OV step, floor)`` and owns the register.

Run (inside the collector container / venv, where FastAPI is present):
  PYTHONPATH=/app python tests/test_monitoring_reroute.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fronius.monitoring import MonitoringServer


class _FakePub:
    def __init__(self):
        self.calls = []

    def publish(self, topic, value, retain=None):
        self.calls.append((topic, value, retain))
        return True


class _FakeWrite:
    enabled = True


class _FakeModbus:
    inverters = [{"device_id": i} for i in (1, 2, 3, 4)]


class _FakeConfig:
    write = _FakeWrite()


class _FakeApp:
    def __init__(self):
        self.config = _FakeConfig()
        self.mqtt_publisher = _FakePub()
        self.modbus_client = _FakeModbus()


def _srv():
    return MonitoringServer(_FakeApp(), port=8080)


def _body(resp):
    return json.loads(resp.body)


def test_set_routes_to_manual_floor():
    srv = _srv()
    resp = srv._issue_power_limit(device_id=1, limit_pct=70, revert_timeout=300, ramp_time=0)
    body = _body(resp)
    assert body["status"] == "queued", body          # JS renders this as success
    assert body["routed_via"] == "ov_manual_floor"
    calls = srv._app.mqtt_publisher.calls
    assert len(calls) == 1, calls
    topic, payload, retain = calls[0]
    assert topic == "pv-stack/nodered/ov/1/manual_floor"
    assert payload["manual_floor_pct"] == 70
    assert payload["expires_at_ms"] > 0
    assert retain is True


def test_restore_clears_the_ceiling():
    srv = _srv()
    resp = srv._issue_power_limit(device_id=2, limit_pct=100, revert_timeout=0, ramp_time=0)
    assert _body(resp)["status"] == "queued"
    topic, payload, _ = srv._app.mqtt_publisher.calls[0]
    assert topic == "pv-stack/nodered/ov/2/manual_floor"
    assert payload["manual_floor_pct"] is None
    assert payload["expires_at_ms"] == 0


def test_below_min_floor_rejected_without_publish():
    # _validFloor accepts only [10,100]; a 5% ceiling must error, not silently
    # drop at the OV node.
    srv = _srv()
    resp = srv._issue_power_limit(device_id=1, limit_pct=5, revert_timeout=0, ramp_time=0)
    assert _body(resp)["status"] == "rejected"
    assert srv._app.mqtt_publisher.calls == []


def test_unknown_inverter_rejected_without_publish():
    srv = _srv()
    resp = srv._issue_power_limit(device_id=9, limit_pct=70, revert_timeout=0, ramp_time=0)
    assert _body(resp)["status"] == "rejected"
    assert srv._app.mqtt_publisher.calls == []


def test_no_revert_self_expires_never_permanent():
    # A forgotten 8082 ceiling must self-expire (the page has no auto-revert
    # safety net of its own) — expires_at_ms is always in the future, not 0.
    srv = _srv()
    srv._issue_power_limit(device_id=1, limit_pct=60, revert_timeout=0, ramp_time=0)
    _, payload, _ = srv._app.mqtt_publisher.calls[0]
    assert payload["manual_floor_pct"] == 60
    assert payload["expires_at_ms"] > 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("monitoring-reroute tests passed")
