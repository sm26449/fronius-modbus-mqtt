"""Single-writer guard + source-tag tests (OV-redesign A3).

The pv-stack-ov-protection node is the SOLE external writer of the inverter
power limit; every other origin must be rejected so two writers can't fight over
one Modbus register.

Run (inside the collector container / venv, where deps are present):
  PYTHONPATH=/app python tests/test_write_guard.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fronius.modbus_client import OV_ALLOWED_SOURCES, PowerLimitCommand


def test_guard_membership():
    assert OV_ALLOWED_SOURCES == frozenset({"nodered-ov", "auto_revert", "shutdown"})
    for src in ("nodered-ov", "auto_revert", "shutdown"):
        assert src in OV_ALLOWED_SOURCES, src
    for src in ("mqtt", "monitoring_ui", "dynamic-grid", ""):
        assert src not in OV_ALLOWED_SOURCES, src   # foreign writers rejected


def test_cmd_default_source_is_not_allowed():
    # A bare/legacy command defaults to a source that the guard rejects, so an
    # un-tagged publisher can never write.
    assert PowerLimitCommand(device_id=1, limit_pct=50).source == "mqtt"
    assert "mqtt" not in OV_ALLOWED_SOURCES


if __name__ == "__main__":
    test_guard_membership()
    test_cmd_default_source_is_not_allowed()
    print("write-guard tests passed")
