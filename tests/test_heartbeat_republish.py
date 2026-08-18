"""Heartbeat republish (general.heartbeat_interval).

In 'changed' mode an unchanged value is normally never republished, so a
sleeping inverter sits on 0.0 W all night and downstream freshness
watchdogs (NR policy controllers, staleSec=120) flag PV stale every
night. With heartbeat_interval set, _should_publish returns True for an
unchanged value once its last successful publish is older than the
interval — and the interval is measured from the last *publish*, not the
last poll.
"""

from unittest.mock import patch

import pytest

from fronius.config import GeneralConfig, ConfigValidationError
from fronius.mqtt_publisher import MQTTPublisher
from fronius.config import MQTTConfig


def make_publisher(heartbeat=0, mode='changed'):
    cfg = MQTTConfig(enabled=False)   # no client/socket — logic only
    return MQTTPublisher(cfg, mode, heartbeat_interval=heartbeat)


class TestHeartbeatRepublish:

    def test_disabled_by_default_unchanged_never_republished(self):
        p = make_publisher(heartbeat=0)
        p._confirm_publish('t/w', 0.0)
        with patch('fronius.mqtt_publisher.time.monotonic',
                   return_value=1e9):
            assert p._should_publish('t/w', 0.0) is False

    def test_unchanged_value_republished_after_interval(self):
        p = make_publisher(heartbeat=60)
        with patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p._confirm_publish('t/w', 0.0)
            mono.return_value = 1059.9
            assert p._should_publish('t/w', 0.0) is False
            mono.return_value = 1060.0
            assert p._should_publish('t/w', 0.0) is True

    def test_interval_measured_from_last_publish(self):
        p = make_publisher(heartbeat=60)
        with patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p._confirm_publish('t/w', 0.0)
            mono.return_value = 1070.0
            p._confirm_publish('t/w', 0.0)   # heartbeat publish confirmed
            mono.return_value = 1100.0       # only 30s after last publish
            assert p._should_publish('t/w', 0.0) is False

    def test_changed_value_still_published_immediately(self):
        p = make_publisher(heartbeat=60)
        with patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p._confirm_publish('t/w', 0.0)
            mono.return_value = 1001.0
            assert p._should_publish('t/w', 5.0) is True

    def test_nan_never_heartbeats(self):
        p = make_publisher(heartbeat=60)
        with patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p._confirm_publish('t/w', float('nan'))
            mono.return_value = 2000.0
            assert p._should_publish('t/w', float('nan')) is False

    def test_per_topic_isolation(self):
        p = make_publisher(heartbeat=60)
        with patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p._confirm_publish('t/a', 1.0)
            mono.return_value = 1050.0
            p._confirm_publish('t/b', 2.0)
            mono.return_value = 1065.0
            assert p._should_publish('t/a', 1.0) is True
            assert p._should_publish('t/b', 2.0) is False


class TestHeartbeatConfig:

    def test_default_off(self):
        assert GeneralConfig().heartbeat_interval == 0

    def test_valid_range(self):
        assert GeneralConfig(heartbeat_interval=60).heartbeat_interval == 60

    @pytest.mark.parametrize('bad', [5, 9, 3601])
    def test_out_of_range_rejected(self, bad):
        with pytest.raises(ConfigValidationError):
            GeneralConfig(heartbeat_interval=bad)


class TestNightZeros:
    """App-level night keep-alive (_publish_night_zeros)."""

    @staticmethod
    def make_app_stub(night=True, skip=True, enabled=True, publisher=True):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        stub = SimpleNamespace()
        stub.config = SimpleNamespace(
            modbus=SimpleNamespace(
                night_mode_enabled=enabled,
                night_skip_inverters=skip,
                night_start_hour=21,
                night_end_hour=6),
            devices=SimpleNamespace(inverters=[1, 2, 3, 4]))
        stub.mqtt_publisher = MagicMock() if publisher else None
        stub._night = night
        return stub

    def call(self, stub):
        import fronius_modbus_mqtt as app_mod
        with patch.object(app_mod, 'is_night_time', return_value=stub._night):
            app_mod.FroniusModbusMQTT._publish_night_zeros(stub)

    def test_emits_zeros_for_every_configured_inverter_at_night(self):
        stub = self.make_app_stub(night=True)
        self.call(stub)
        calls = [c.args[0] for c in stub.mqtt_publisher.publish_inverter_offline.call_args_list]
        assert calls == ['1', '2', '3', '4']

    def test_never_runs_in_daylight(self):
        stub = self.make_app_stub(night=False)
        self.call(stub)
        stub.mqtt_publisher.publish_inverter_offline.assert_not_called()

    def test_respects_night_skip_disabled(self):
        stub = self.make_app_stub(night=True, skip=False)
        self.call(stub)
        stub.mqtt_publisher.publish_inverter_offline.assert_not_called()

    def test_no_publisher_no_crash(self):
        stub = self.make_app_stub(night=True, publisher=False)
        self.call(stub)   # must not raise


class TestOfflineZerosHeartbeat:
    """publish_inverter_offline × heartbeat: repeated night calls re-emit
    each zeroed field once per heartbeat window, not once per call."""

    def test_offline_zeros_paced_by_heartbeat(self):
        p = make_publisher(heartbeat=60)
        published = []
        p.client = object()   # pass the "if not self.client" guard
        with patch.object(p, 'publish', side_effect=lambda t, v, r=None: (published.append(t), True)[1]), \
             patch('fronius.mqtt_publisher.time.monotonic') as mono:
            mono.return_value = 1000.0
            p.publish_inverter_offline('1')
            n_fields = len(published)
            assert n_fields == len(MQTTPublisher.INVERTER_OFFLINE_ZERO_FIELDS)
            p.publish_inverter_offline('1')          # same instant: deduped
            assert len(published) == n_fields
            mono.return_value = 1060.0               # heartbeat window elapsed
            p.publish_inverter_offline('1')
            assert len(published) == 2 * n_fields
