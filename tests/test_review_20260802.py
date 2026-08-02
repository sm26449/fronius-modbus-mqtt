"""Regression tests for the 2026-08-02 review fixes.

Covers the pure-logic parts of the incident-area fixes (no Modbus/hardware):
- config env hardening (M21/M23): malformed values fall back loudly, never crash.
- healthcheck honesty (M17): degraded (partial fleet) is a valid exit-0 state,
  distinct from healthy, and reads the fleet-completeness fields.

Run inside the collector container / venv:
  PYTHONPATH=/app python tests/test_review_20260802.py
(or: pytest tests/test_review_20260802.py)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fronius.config import _env_get   # noqa: E402


# ── M23/M21: env parsing hardening ───────────────────────────────────────

def test_env_int_valid():
    os.environ['_T_INT'] = '42'
    assert _env_get('_T_INT', 7, int) == 42


def test_env_int_garbage_falls_back():
    os.environ['_T_INT'] = 'not-a-number'
    assert _env_get('_T_INT', 7, int) == 7   # falls back, does NOT raise


def test_env_list_valid():
    os.environ['_T_LIST'] = '1, 2 ,3'
    assert _env_get('_T_LIST', [], list) == [1, 2, 3]


def test_env_list_corrupt_inverter_ids_falls_back():
    # The exact M23 danger: a corrupt INVERTER_IDS must not silently yield a
    # half-parsed fleet — it falls back to the default (and warns on stderr).
    os.environ['_T_LIST'] = '1,2,x'
    assert _env_get('_T_LIST', [1, 2, 3, 4], list) == [1, 2, 3, 4]


def test_env_missing_uses_default():
    os.environ.pop('_T_MISSING', None)
    assert _env_get('_T_MISSING', 99, int) == 99


def test_env_bool_parsing():
    for v, exp in (('true', True), ('1', True), ('yes', True),
                   ('false', False), ('0', False), ('off', False)):
        os.environ['_T_BOOL'] = v
        assert _env_get('_T_BOOL', False, bool) is exp, v


# ── M17: healthcheck reports partial fleet as degraded (exit 0), not a lie ──

def _run_healthcheck(lines: list[str]) -> int:
    import importlib
    hc = importlib.import_module('healthcheck')
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, 'w') as f:
        f.write("\n".join(lines) + "\n")
    orig = hc.HEALTH_FILE
    hc.HEALTH_FILE = path
    try:
        return hc.check_health()
    finally:
        hc.HEALTH_FILE = orig
        os.unlink(path)


def _base(status: str, extra: list[str]) -> list[str]:
    return [str(int(time.time())), status, 'mqtt:True', 'influxdb:True',
            'influxdb_enabled:True', 'modbus:True', 'sleep_mode:False',
            'night_time:False'] + extra


def test_healthcheck_healthy_full_fleet():
    assert _run_healthcheck(_base('healthy',
        ['inverters_online:4', 'inverters_configured:4'])) == 0


def test_healthcheck_degraded_is_exit_zero():
    # Partial fleet = degraded, but exit 0 (self-heal owns recovery; restart:
    # unless-stopped ignores health anyway). The pre-fix bug reported plain
    # 'healthy' here off socket-open alone.
    assert _run_healthcheck(_base('degraded',
        ['inverters_online:1', 'inverters_configured:4'])) == 0


def test_healthcheck_unhealthy_socket_down():
    lines = [str(int(time.time())), 'unhealthy', 'mqtt:True', 'influxdb:True',
             'influxdb_enabled:True', 'modbus:False', 'sleep_mode:False',
             'night_time:False']
    assert _run_healthcheck(lines) == 1


def test_healthcheck_stale_file_unhealthy():
    old = str(int(time.time()) - 999)
    lines = [old, 'healthy', 'mqtt:True', 'influxdb:True',
             'influxdb_enabled:True', 'modbus:True', 'sleep_mode:False',
             'night_time:False', 'inverters_online:4', 'inverters_configured:4']
    assert _run_healthcheck(lines) == 1


if __name__ == '__main__':
    fns = [g for n, g in sorted(globals().items())
           if n.startswith('test_') and callable(g)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
