"""Modbus TCP Client with simple sequential polling for Fronius devices

Architecture:
- DevicePoller: Single thread that polls all inverters and meters sequentially
- Dedicated Modbus connection per poller
- Night/Sleep mode detection for Fronius DataManager
"""

import time
import logging
import queue
import threading
import subprocess
import platform
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Callable

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .config import ModbusConfig, DevicesConfig, DebugConfig, WriteConfig
from .register_parser import RegisterParser
from .logging_setup import get_logger


@dataclass
class DeviceRuntimeState:
    """Runtime state tracking for a single device."""
    device_id: int
    device_type: str  # 'inverter' or 'meter'
    status: str = "offline"
    last_seen: Optional[datetime] = None
    read_errors: int = 0
    consecutive_errors: int = 0
    model_id: Optional[int] = None
    model_id_verified_at: Optional[datetime] = None
    backoff_until: Optional[float] = None  # timestamp when to retry


@dataclass
class PowerLimitCommand:
    """Command to set inverter power limit via Modbus write."""
    device_id: int
    limit_pct: float          # 0-100 percentage
    revert_timeout: int = 0   # WMaxLim_RvrtTms (0 = no auto-revert at inverter level)
    ramp_time: int = 0        # WMaxLim_RmpTms (seconds for gradual transition)
    source: str = "mqtt"      # "mqtt" or "auto_revert"
    timestamp: float = field(default_factory=time.time)


# Suppress pymodbus exception logging
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def ping_host(host: str, timeout: int = 2) -> bool:
    """
    Check if host is reachable via ICMP ping.

    Args:
        host: Hostname or IP address
        timeout: Timeout in seconds

    Returns:
        True if host responds to ping
    """
    try:
        # Platform-specific ping command
        # Windows: -w timeout in milliseconds
        # macOS: -W timeout in milliseconds
        # Linux: -W timeout in seconds
        system = platform.system().lower()
        if system == 'windows':
            cmd = ['ping', '-n', '1', '-w', str(timeout * 1000), host]
        elif system == 'darwin':  # macOS
            cmd = ['ping', '-c', '1', '-W', str(timeout * 1000), host]
        else:  # Linux and other Unix-like systems
            cmd = ['ping', '-c', '1', '-W', str(timeout), host]

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        logging.debug(f"Ping check failed for {host}: {e}")
        return False


def is_night_time(start_hour: int = 21, end_hour: int = 6) -> bool:
    """
    Check if current time is within night hours.

    Args:
        start_hour: Hour when night starts (e.g., 21 for 9 PM)
        end_hour: Hour when night ends (e.g., 6 for 6 AM)

    Returns:
        True if current time is night time
    """
    current_hour = datetime.now().hour

    if start_hour > end_hour:
        # Night spans midnight (e.g., 21:00 - 06:00)
        return current_hour >= start_hour or current_hour < end_hour
    else:
        # Night within same day (e.g., 23:00 - 04:00)
        return start_hour <= current_hour < end_hour


class ModbusConnection:
    """Shared Modbus TCP connection with thread-safe access."""

    SUNSPEC_ID = 0x53756E53  # 'SunS'
    INVERTER_MODELS = [101, 102, 103]
    METER_MODELS = [201, 202, 203, 204]
    STORAGE_MODEL = 124  # Basic Storage Controls

    def __init__(self, config: ModbusConfig, parser: RegisterParser):
        self.config = config
        self.parser = parser
        self.log = get_logger()
        self.client: ModbusTcpClient = None
        self.connected = False
        self.lock = threading.Lock()
        self.successful_reads = 0
        self.failed_reads = 0
        self.last_unit_id = None  # Track last unit ID to detect changes

    def connect(self) -> bool:
        """Establish Modbus TCP connection."""
        try:
            self.client = ModbusTcpClient(
                host=self.config.host,
                port=self.config.port,
                timeout=self.config.timeout
            )
            self.connected = self.client.connect()
            if self.connected:
                self.log.info(f"Modbus connected to {self.config.host}:{self.config.port}")
            return self.connected
        except Exception as e:
            self.log.error(f"Modbus connection error: {e}")
            return False

    def disconnect(self):
        """Close Modbus connection."""
        with self.lock:
            if self.client:
                self.client.close()
            self.connected = False
            self.log.info("Modbus disconnected")

    def read_registers(self, address: int, count: int, unit_id: int) -> Optional[List[int]]:
        """Read holding registers with thread-safe access."""
        with self.lock:
            # Reconnect if unit ID changed (Fronius DataManager has buffering issues)
            if self.last_unit_id is not None and self.last_unit_id != unit_id:
                if self.client and self.connected:
                    self.client.close()
                    self.connected = False
                    time.sleep(0.1)  # Brief pause before reconnect

            for attempt in range(self.config.retry_attempts):
                try:
                    # Reconnect if needed
                    if not self.connected or not self.client.is_socket_open():
                        # Close old client to prevent socket leak
                        if self.client:
                            try:
                                self.client.close()
                            except Exception:
                                pass
                        self.client = ModbusTcpClient(
                            host=self.config.host,
                            port=self.config.port,
                            timeout=self.config.timeout
                        )
                        self.connected = self.client.connect()
                        if not self.connected:
                            time.sleep(0.1)
                            continue

                    result = self.client.read_holding_registers(
                        address=address - 1,  # pymodbus is 0-indexed
                        count=count,
                        device_id=unit_id
                    )

                    if not result.isError() and result.registers:
                        self.successful_reads += 1
                        self.last_unit_id = unit_id
                        return result.registers
                    elif result.isError():
                        if attempt < self.config.retry_attempts - 1:
                            time.sleep(self.config.retry_delay)

                except Exception as e:
                    self.log.debug(f"Unit {unit_id}: read error - {e}")
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)

            self.failed_reads += 1
            return None

    def write_registers(self, address: int, values: List[int], unit_id: int) -> bool:
        """Write holding registers with thread-safe access.

        Args:
            address: SunSpec 1-indexed register address
            values: List of 16-bit register values to write
            unit_id: Modbus device ID

        Returns:
            True on success
        """
        with self.lock:
            try:
                if not self.connected or not self.client.is_socket_open():
                    if self.client:
                        try:
                            self.client.close()
                        except Exception:
                            pass
                    self.client = ModbusTcpClient(
                        host=self.config.host,
                        port=self.config.port,
                        timeout=self.config.timeout
                    )
                    self.connected = self.client.connect()
                    if not self.connected:
                        self.log.error(f"Unit {unit_id}: write failed — cannot connect")
                        return False

                result = self.client.write_registers(
                    address=address - 1,  # pymodbus is 0-indexed
                    values=values,
                    device_id=unit_id
                )

                if result.isError():
                    self.log.error(f"Unit {unit_id}: write error at {address}: {result}")
                    return False

                self.last_unit_id = unit_id
                self.log.warning(
                    f"Unit {unit_id}: wrote {len(values)} registers at {address}: "
                    f"{[f'0x{v:04X}' for v in values]}"
                )
                return True

            except Exception as e:
                self.log.error(f"Unit {unit_id}: write exception at {address}: {e}")
                self.connected = False
                return False

    def identify_device(self, unit_id: int) -> Optional[Dict]:
        """Identify a device by reading SunSpec registers."""
        regs = self.read_registers(40001, 69, unit_id)
        if not regs or len(regs) < 69:
            return None

        # Verify SunSpec header
        sunspec_id = (regs[0] << 16) | regs[1]
        if sunspec_id != self.SUNSPEC_ID:
            return None

        device_info = {
            'device_id': unit_id,
            'manufacturer': self.parser.decode_string(regs[4:20]),
            'model': self.parser.decode_string(regs[20:36]),
            'version': self.parser.decode_string(regs[44:52]),
            'serial_number': self.parser.decode_string(regs[52:68]),
        }

        # Force TCP reconnection to clear DataManager buffer before reading model ID
        # Without this, the buffer may contain residual SunSpec header data (e.g., 0x5365 = "Se")
        self.connected = False
        time.sleep(0.3)

        # Read model ID with retry on invalid value
        valid_models = self.INVERTER_MODELS + self.METER_MODELS
        model_id = None

        for attempt in range(3):
            model_regs = self.read_registers(40070, 1, unit_id)
            if model_regs:
                model_id = model_regs[0]
                if model_id in valid_models:
                    break
                # Invalid model_id (e.g., 21365 = 0x5365 = "Se" from SunSpec header)
                self.log.debug(f"Device {unit_id}: invalid model_id {model_id} (0x{model_id:04X}), retry {attempt + 1}/3")
                self.connected = False
                time.sleep(0.3)
                model_id = None

        if model_id:
            device_info['model_id'] = model_id
            if model_id in self.INVERTER_MODELS:
                device_info['device_type'] = 'inverter'
                device_info['inverter_type'] = self.parser.detect_inverter_type(device_info['model'])
            elif model_id in self.METER_MODELS:
                device_info['device_type'] = 'meter'

        self.log.info(f"Device {unit_id}: {device_info['manufacturer']} {device_info['model']} model_id={model_id} (SN: {device_info['serial_number']})")
        return device_info

    def check_storage_support(self, unit_id: int) -> bool:
        """
        Check if an inverter supports storage (Model 124) by reading
        the model ID at address 40341 (Int+SF format: model header before 40343).

        Returns True if storage model 124 is found.
        """
        time.sleep(0.1)
        # Read model header at 40341 (2 registers: ID + Length)
        model_regs = self.read_registers(40341, 2, unit_id)
        if model_regs and len(model_regs) >= 2:
            model_id = model_regs[0]
            if model_id == self.STORAGE_MODEL:
                self.log.info(f"Device {unit_id}: Storage support detected (Model 124)")
                return True
        return False


class DevicePoller(threading.Thread):
    """
    Single polling thread for all devices (inverters + meters).

    Uses a single Modbus connection to avoid conflicts on Fronius DataManager
    which cannot handle multiple simultaneous TCP connections properly.

    Features:
    - Night/sleep mode detection when DataManager is unavailable
    - Ping check before attempting Modbus connection
    - Exponential backoff during night hours
    """

    ACTIVE_STATUS_CODES = [4, 5]
    STORAGE_ADDRESS = 40343  # Model 124 data starts here (Int+SF format)
    STORAGE_LENGTH = 24      # Model 124 has 24 registers
    CONTROLS_POLL_INTERVAL = 60  # Read Model 123 every 60 seconds

    def __init__(self, modbus_config: ModbusConfig, inverters: List[Dict],
                 meters: List[Dict], poll_delay: float, read_delay_ms: int,
                 parser: RegisterParser, publish_callback: Callable,
                 debug_config: DebugConfig = None,
                 write_config: WriteConfig = None,
                 command_result_callback: Callable = None):
        super().__init__(daemon=True, name="DevicePoller")
        self.modbus_config = modbus_config
        self.inverters = inverters
        self.meters = meters
        self.poll_delay = poll_delay
        self.read_delay = read_delay_ms / 1000.0
        self.parser = parser
        self.publish_callback = publish_callback
        self.log = get_logger()
        self.running = False
        self._stop_event = threading.Event()
        self.debug_config = debug_config or DebugConfig()
        self.write_config = write_config
        self._command_result_callback = command_result_callback

        # Single connection for all devices
        self.connection = ModbusConnection(modbus_config, parser)

        # Track last controls read time per inverter
        self._last_controls_read: Dict[int, float] = {}

        # Per-device runtime tracking
        self._device_runtime: Dict[str, DeviceRuntimeState] = {}
        self._runtime_lock = threading.Lock()  # Thread safety for runtime dict
        self._model_id_verify_interval = 3600  # Re-verify every hour
        self._model_id_verify_on_errors = 5    # Or after N consecutive errors

        # Night/sleep mode tracking
        self._consecutive_failures = 0
        self._in_sleep_mode = False
        self._last_successful_poll = time.time()
        self._sleep_mode_start = None

        # Data validation state (for buffer corruption detection)
        self._last_valid_data: Dict[int, Dict] = {}   # {unit_id: last non-corrupted data}
        self._corruption_count: Dict[int, int] = {}     # {unit_id: corruption count}
        self._last_status: Dict[int, int] = {}           # {unit_id: last status_code}
        self._night_skip_logged = False                  # Log night skip message once

        # Write command processing
        self._command_queue: queue.Queue = queue.Queue(maxsize=10)
        self._last_write_time: Dict[int, float] = {}     # Rate limiting per device
        self._active_limits: Dict[int, dict] = {}         # Tracks active power limits
        self._auto_revert_timers: Dict[int, float] = {}   # Timestamp when to auto-revert
        self._write_count = 0
        self._write_failures = 0

    def _runtime_key(self, device_id: int, device_type: str) -> str:
        """Generate unique key for device runtime tracking."""
        return f"{device_type}_{device_id}"

    def _init_runtime_state(self, device_info: Dict, device_type: str) -> DeviceRuntimeState:
        """Initialize or get runtime state for a device. Must be called with _runtime_lock held."""
        key = self._runtime_key(device_info['device_id'], device_type)
        if key not in self._device_runtime:
            model_id = device_info.get('model_id')
            self.log.debug(
                f"{device_type.title()} {device_info['device_id']}: "
                f"initializing runtime state with model_id={model_id}"
            )
            self._device_runtime[key] = DeviceRuntimeState(
                device_id=device_info['device_id'],
                device_type=device_type,
                model_id=model_id
            )
        return self._device_runtime[key]

    def _get_backoff_delay(self, consecutive_errors: int) -> int:
        """Calculate backoff delay based on consecutive errors."""
        if consecutive_errors < 3:
            return 0
        # Cap extra_errors to prevent overflow (2**50 = PB range)
        extra_errors = min(consecutive_errors - 3, 6)  # max 2^6=64 -> 640s before cap
        delay = min(10 * (2 ** extra_errors), 60)  # max 60s
        return delay

    def _update_runtime_on_success(self, device_info: Dict, device_type: str):
        """Update runtime state after successful read."""
        with self._runtime_lock:
            state = self._init_runtime_state(device_info, device_type)
            state.status = "online"
            state.last_seen = datetime.now()
            state.consecutive_errors = 0
            state.backoff_until = None

        # Maybe verify model_id periodically (outside lock - involves I/O)
        # Note: state reference is safe here because DeviceRuntimeState objects
        # are never replaced in _device_runtime dict, only modified in-place
        self._maybe_verify_model_id(device_info, device_type, state)

    def _update_runtime_on_failure(self, device_info: Dict, device_type: str):
        """Update runtime state after failed read."""
        should_verify_model = False
        with self._runtime_lock:
            state = self._init_runtime_state(device_info, device_type)
            state.read_errors += 1
            state.consecutive_errors += 1

            # Device becomes offline after 3 consecutive errors
            if state.consecutive_errors >= 3:
                if state.status != "offline":
                    self.log.warning(
                        f"{device_type.title()} {device_info['device_id']}: "
                        f"marked offline after {state.consecutive_errors} consecutive errors"
                    )
                state.status = "offline"

                # Set backoff for offline device
                delay = self._get_backoff_delay(state.consecutive_errors)
                if delay > 0:
                    state.backoff_until = time.time() + delay
                    self.log.debug(
                        f"{device_type.title()} {device_info['device_id']}: "
                        f"backoff {delay}s after {state.consecutive_errors} errors"
                    )

            # Check if model_id verification needed
            if state.consecutive_errors >= self._model_id_verify_on_errors:
                should_verify_model = True

        # Trigger model_id verification outside lock (involves I/O)
        # Note: state reference is safe - see comment in _update_runtime_on_success
        if should_verify_model:
            self._verify_model_id(device_info, device_type, state, reason="consecutive errors")

    def _maybe_verify_model_id(self, device_info: Dict, device_type: str, state: DeviceRuntimeState):
        """Check if model_id needs periodic re-verification."""
        now = datetime.now()
        should_verify = False

        with self._runtime_lock:
            if state.model_id_verified_at is None:
                state.model_id_verified_at = now
                return

            elapsed = (now - state.model_id_verified_at).total_seconds()
            if elapsed >= self._model_id_verify_interval:
                should_verify = True

        if should_verify:
            self._verify_model_id(device_info, device_type, state, reason="periodic check")

    def _verify_model_id(self, device_info: Dict, device_type: str,
                         state: DeviceRuntimeState, reason: str = ""):
        """Re-read and verify model_id from device."""
        unit_id = device_info['device_id']

        # Force connection reset to clear DataManager buffer before reading model_id
        self.connection.connected = False
        time.sleep(0.3)

        model_regs = self.connection.read_registers(40070, 1, unit_id)

        if model_regs:
            new_model_id = model_regs[0]

            # Validate model_id is in expected range (SunSpec inverter/meter models)
            # Invalid values like 21365 (0x5365) are residual buffer data
            valid_models = [101, 102, 103, 111, 112, 113, 201, 202, 203, 204]
            if new_model_id not in valid_models:
                self.log.debug(
                    f"{device_type.title()} {unit_id}: ignoring invalid model_id {new_model_id} "
                    f"(buffer residue), keeping {state.model_id}"
                )
                with self._runtime_lock:
                    state.model_id_verified_at = datetime.now()
                return

            with self._runtime_lock:
                old_model_id = state.model_id

                if old_model_id is not None and new_model_id != old_model_id:
                    self.log.warning(
                        f"{device_type.title()} {unit_id}: model_id changed from "
                        f"{old_model_id} to {new_model_id} ({reason})"
                    )
                    # Update device_info with new model_id
                    device_info['model_id'] = new_model_id

                state.model_id = new_model_id
                state.model_id_verified_at = datetime.now()

            self.log.debug(f"{device_type.title()} {unit_id}: model_id verified = {new_model_id}")

    def _is_device_in_backoff(self, device_info: Dict, device_type: str) -> bool:
        """Check if device is currently in backoff period."""
        key = self._runtime_key(device_info['device_id'], device_type)
        with self._runtime_lock:
            state = self._device_runtime.get(key)
            if state and state.backoff_until and time.time() < state.backoff_until:
                return True
        return False

    def _calc_aggregate_status(self, online: int, total: int) -> str:
        """Calculate aggregate status from online/total counts."""
        if total == 0:
            return "offline"
        if online == total:
            return "online"
        if online == 0:
            return "offline"
        return "partial"

    def get_runtime_stats(self) -> Dict:
        """
        Get runtime statistics for all devices.

        Returns dict with:
        - inverter_status: aggregate status for inverters
        - meter_status: aggregate status for meters
        - devices: dict of device runtime info
        """
        inverter_online = 0
        inverter_total = 0
        meter_online = 0
        meter_total = 0
        devices = {}

        with self._runtime_lock:
            for key, state in self._device_runtime.items():
                device_data = {
                    'status': state.status,
                    'last_seen': state.last_seen.isoformat() if state.last_seen else None,
                    'read_errors': state.read_errors,
                    'model_id': state.model_id,
                }
                devices[key] = device_data

                if state.device_type == 'inverter':
                    inverter_total += 1
                    if state.status == 'online':
                        inverter_online += 1
                elif state.device_type == 'meter':
                    meter_total += 1
                    if state.status == 'online':
                        meter_online += 1

        return {
            'inverter_status': self._calc_aggregate_status(inverter_online, inverter_total),
            'meter_status': self._calc_aggregate_status(meter_online, meter_total),
            'inverter_online': inverter_online,
            'inverter_total': inverter_total,
            'meter_online': meter_online,
            'meter_total': meter_total,
            'devices': devices,
        }

    def _validate_and_reconcile(self, data: dict, unit_id: int) -> dict:
        """Detect DataManager buffer corruption and reconcile using MPPT data.

        The Fronius DataManager TCP server has a known buffer retention issue where
        Model 103 registers return stale/zero data, while MPPT Model 160 data
        (read after a fresh connection reset) remains correct.

        Detection strategies:
        1. Model 103 all-zero but MPPT shows production
        2. Impossible status_code for time of day (MPPT/STARTING at night)
        3. FAULT status but MPPT strings producing power

        Reconciliation:
        - DC power/voltage/current → MPPT sums (ground truth)
        - AC power → DC × 0.97 estimate (better than 0)
        - AC voltage, temps, lifetime_energy → None (skip publish)
        - Status code → restore from last valid or set SLEEPING for night
        """
        mppt = data.get('mppt', {})
        modules = mppt.get('modules', [])
        mppt_dc_power = sum(m.get('dc_power', 0) or 0 for m in modules)
        mppt_dc_current = sum(m.get('dc_current', 0) or 0 for m in modules)
        mppt_dc_voltage = max((m.get('dc_voltage', 0) or 0 for m in modules), default=0)

        # Sanity check MPPT values before trusting as ground truth
        # Fronius max: 75kWp system, ~1000V string voltage, ~100A per string
        if mppt_dc_power > 100000 or mppt_dc_voltage > 1000 or mppt_dc_current > 200:
            self.log.warning(
                f"Inverter {unit_id}: MPPT values out of range "
                f"(P={mppt_dc_power:.0f}W V={mppt_dc_voltage:.0f}V I={mppt_dc_current:.1f}A), "
                f"skipping validation"
            )
            data['_corrupted'] = True
            data['_corruption_reason'] = f'MPPT out of range P={mppt_dc_power:.0f}W'
            data['_reconciled'] = False
            return data

        status_code = data.get('status_code', 0)
        corruption_detected = False
        reason = ''

        # Strategy 1: MPPT shows production but Model 103 shows all zeros
        model103_all_zero = (
            (data.get('ac_power') is not None and data['ac_power'] == 0) and
            (data.get('dc_power') is not None and data['dc_power'] == 0) and
            (data.get('dc_voltage') is not None and data['dc_voltage'] == 0)
        )
        if model103_all_zero and mppt_dc_power > 100:
            corruption_detected = True
            reason = f'Model103 all-zero but MPPT={mppt_dc_power:.0f}W'

        # Strategy 2: impossible status_code for time of day
        hour = datetime.now().hour
        is_night = is_night_time(
            self.modbus_config.night_start_hour,
            self.modbus_config.night_end_hour
        )
        # Only flag MPPT(4) and THROTTLED(5) — STARTING(3) is legitimate at dawn
        if is_night and status_code in (4, 5):
            corruption_detected = True
            reason = f'impossible status {status_code} at night ({hour:02d}:xx)'

        # Strategy 3: FAULT (7) with Model 103 all-zero but MPPT producing
        # Only flag corruption when Model 103 returned garbage (all zeros).
        # A real FAULT with non-zero Model 103 data is genuine.
        if status_code == 7 and model103_all_zero and mppt_dc_power > 100:
            corruption_detected = True
            reason = f'FAULT+all-zero but MPPT producing {mppt_dc_power:.0f}W'

        if not corruption_detected:
            data['_corrupted'] = False
            data['_corruption_reason'] = ''
            data['_reconciled'] = False
            return data

        # --- Reconciliation ---
        prev_raw = self._last_valid_data.get(unit_id, {})
        # Expire stale cached data (>5 minutes old)
        cached_at = prev_raw.get('_cached_at', 0)
        prev = prev_raw if (time.time() - cached_at) < 300 else {}
        reconciled = {}

        # DC side: use MPPT as ground truth (always correct after connection reset)
        if mppt_dc_power > 0:
            if data.get('dc_power', 0) == 0:
                data['dc_power'] = mppt_dc_power
                reconciled['dc_power'] = mppt_dc_power
            if data.get('dc_voltage', 0) == 0:
                data['dc_voltage'] = mppt_dc_voltage
                reconciled['dc_voltage'] = mppt_dc_voltage
            if data.get('dc_current', 0) == 0:
                data['dc_current'] = mppt_dc_current
                reconciled['dc_current'] = mppt_dc_current
            # Estimate ac_power from DC (better than 0 for PV aggregation)
            if data.get('ac_power', 0) == 0:
                data['ac_power'] = round(mppt_dc_power * 0.97, 1)
                reconciled['ac_power'] = data['ac_power']

        # Unreliable fields: null out (can't derive from MPPT)
        # None values won't be published by MQTT/InfluxDB (already filtered)
        for field in ['ac_voltage_an', 'ac_voltage_bn', 'ac_voltage_cn',
                      'ac_current', 'ac_current_a', 'ac_current_b', 'ac_current_c',
                      'temp_cabinet', 'temp_heatsink', 'temp_transformer', 'temp_other',
                      'lifetime_energy']:
            if data.get(field) is not None and data[field] == 0:
                data[field] = None
                reconciled[field] = None

        # Fix status_code from last known valid read
        if status_code == 7 and prev.get('status_code') in (4, 5):
            data['status_code'] = prev['status_code']
            data['status'] = self.parser.parse_status(prev['status_code'])
            reconciled['status_code'] = prev['status_code']
        elif is_night and status_code not in (0, 1, 2):
            data['status_code'] = 2  # SLEEPING (most likely at night)
            data['status'] = self.parser.parse_status(2)
            reconciled['status_code'] = 2

        # Tag data for downstream consumers (InfluxDB tags, MQTT metadata)
        data['_corrupted'] = True
        data['_corruption_reason'] = reason
        data['_reconciled'] = bool(reconciled)
        data['_reconciled_fields'] = reconciled

        # Track corruption stats
        self._corruption_count[unit_id] = self._corruption_count.get(unit_id, 0) + 1

        if self.debug_config.log_reconciliation:
            self.log.warning(
                f"Inverter {unit_id}: buffer corruption detected ({reason}), "
                f"reconciled {len(reconciled)} fields, "
                f"total corruptions: {self._corruption_count[unit_id]}"
            )

        return data

    def _poll_inverter(self, device_info: Dict, max_retries: int = 3) -> bool:
        """Poll a single inverter with retry on failure."""
        unit_id = device_info['device_id']

        # Check if device is in backoff period (offline with exponential delay)
        if self._is_device_in_backoff(device_info, 'inverter'):
            return False

        # Force connection reset before reading to ensure fresh DataManager buffer
        # Both Model 103 and MPPT Model 160 are then read on the same clean connection
        self.connection.connected = False
        time.sleep(0.3)

        # Read main registers (40072-40120) with retry
        regs = None
        for attempt in range(max_retries):
            regs = self.connection.read_registers(40072, 49, unit_id)

            if regs and len(regs) >= 49:
                break  # Success

            if attempt < max_retries - 1:
                self.log.debug(f"Inverter {unit_id}: main register read failed, retry {attempt + 1}/{max_retries}")
                time.sleep(0.5)
            else:
                self.log.warning(f"Inverter {unit_id}: main register read failed after {max_retries} attempts")
                # Force reconnect on next read to clear any buffer issues
                self.connection.connected = False
                self._update_runtime_on_failure(device_info, 'inverter')
                return False

        # Parse data
        model_id = device_info.get('model_id', 103)
        data = self.parser.parse_inverter_measurements(regs, model_id)

        if not data:
            return False

        # Add device info
        data['device_id'] = unit_id
        data['serial_number'] = device_info.get('serial_number', '')
        data['model'] = device_info.get('model', '')
        data['manufacturer'] = device_info.get('manufacturer', '')

        # Parse status + vendor status (e.g., Fronius 475 = Isolation Error)
        data['status'] = self.parser.parse_status(data.get('status_code', 0))
        vendor_code = data.get('status_vendor', 0)
        data['status']['vendor_code'] = vendor_code
        data['status']['vendor_name'], data['status']['vendor_description'] = \
            self.parser.parse_vendor_status(vendor_code)
        data['is_active'] = data.get('status_code', 0) in self.ACTIVE_STATUS_CODES

        # Parse events
        inverter_type = device_info.get('inverter_type', 'all')
        data['events'] = self.parser.parse_event_flags(
            data.get('evt_vnd1', 0),
            data.get('evt_vnd2', 0),
            data.get('evt_vnd3', 0),
            data.get('evt_vnd4', 0),
            inverter_type
        )

        # Read MPPT Model 160 on the same fresh connection (no reset needed)
        mppt_data = self._read_mppt_data(unit_id)
        if mppt_data and mppt_data.get('modules'):
            data['mppt'] = mppt_data
            for i, mod in enumerate(mppt_data['modules']):
                self.log.debug(f"Inverter {unit_id} MPPT{i+1}: "
                               f"V={mod.get('dc_voltage', 0):.1f}V, "
                               f"I={mod.get('dc_current', 0):.2f}A, "
                               f"P={mod.get('dc_power', 0):.0f}W")

        # Validate and reconcile data against MPPT ground truth
        if self.debug_config.validate_data:
            data = self._validate_and_reconcile(data, unit_id)

        # Read Model 123 - Immediate Controls (power limit, PF, connection status)
        # Only read every CONTROLS_POLL_INTERVAL seconds (controls don't change often)
        now = time.time()
        last_read = self._last_controls_read.get(unit_id, 0)
        if now - last_read >= self.CONTROLS_POLL_INTERVAL:
            controls_data = self._read_immediate_controls(unit_id)
            if controls_data:
                data['controls'] = controls_data
                self._last_controls_read[unit_id] = now
                self.log.debug(f"Inverter {unit_id}: Controls - "
                              f"Conn={controls_data.get('connected')}, "
                              f"WMaxLim={controls_data.get('power_limit_pct')}%, "
                              f"PF={controls_data.get('power_factor')}")

        # Try to read storage registers if device has storage support
        if device_info.get('has_storage'):
            time.sleep(self.read_delay)
            storage_regs = self.connection.read_registers(
                self.STORAGE_ADDRESS, self.STORAGE_LENGTH, unit_id
            )
            if storage_regs and len(storage_regs) >= self.STORAGE_LENGTH:
                storage_data = self.parser.parse_storage_measurements(storage_regs)
                if storage_data:
                    data['storage'] = storage_data
                    self.publish_callback(unit_id, 'storage', storage_data)
            else:
                self.log.debug(f"Inverter {unit_id}: storage read failed")

        # Track status transitions (after reconciliation, so we log the corrected status)
        prev_status = self._last_status.get(unit_id)
        curr_status = data.get('status_code')
        self._last_status[unit_id] = curr_status
        if self.debug_config.log_status_transitions and prev_status is not None and prev_status != curr_status:
            prev_name = self.parser.parse_status(prev_status).get('name', '?')
            curr_name = data.get('status', {}).get('name', '?')
            self.log.warning(f"Inverter {unit_id}: {prev_name}({prev_status}) -> {curr_name}({curr_status})")

        # Cache last valid data for status recovery during corruption (TTL: 5 minutes)
        if not data.get('_corrupted'):
            self._last_valid_data[unit_id] = {
                'status_code': data.get('status_code'),
                'ac_power': data.get('ac_power'),
                'ac_voltage_an': data.get('ac_voltage_an'),
                '_cached_at': time.time(),
            }

        # Publish to MQTT
        self.publish_callback(unit_id, 'inverter', data)
        self._update_runtime_on_success(device_info, 'inverter')
        self.log.debug(f"Inverter {unit_id}: published (W={data.get('ac_power', 0)})")
        return True

    def _read_mppt_data(self, unit_id: int, max_retries: int = 3) -> Optional[Dict]:
        """
        Read MPPT Model 160 data in a single query with retry on failure.

        Reads 40254-40301 (48 registers) as per SunSpec Model 160 spec:
        - Header (40254-40255): 2 registers
        - Scale factors (40256-40259): 4 registers
        - Global data (40260-40263): 4 registers
        - Module 1 (40264-40283): 20 registers
        - Module 2 (40284-40301): 18 registers (partial, up to Tmp)

        Total: 48 registers - within Fronius limit of ~50-55
        """
        for attempt in range(max_retries):
            regs = self.connection.read_registers(40254, 48, unit_id)
            if not regs or len(regs) < 48:
                if attempt < max_retries - 1:
                    self.log.debug(f"Inverter {unit_id}: MPPT read failed, retry {attempt + 1}/{max_retries}")
                    time.sleep(1.0)  # Wait 1s before retry
                    continue
                self.log.warning(f"Inverter {unit_id}: MPPT read failed after {max_retries} attempts")
                return None

            # Verify model header (offset 0-1)
            model_id = regs[0]
            if model_id != 160:
                if attempt < max_retries - 1:
                    self.log.debug(f"Inverter {unit_id}: MPPT model mismatch (got {model_id}), retry {attempt + 1}/{max_retries}")
                    time.sleep(1.0)  # Wait 1s before retry
                    continue
                self.log.debug(f"Inverter {unit_id}: MPPT model mismatch (got {model_id}, expected 160) after {max_retries} attempts")
                return None

            # Success - break out of retry loop
            break

        # Extract scale factors (offset 2-5, i.e., 40256-40259)
        sf_dca = regs[2] if regs[2] < 32768 else regs[2] - 65536
        sf_dcv = regs[3] if regs[3] < 32768 else regs[3] - 65536
        sf_dcw = regs[4] if regs[4] < 32768 else regs[4] - 65536
        sf_dcwh = regs[5] if regs[5] < 32768 else regs[5] - 65536

        # Validate scale factors (SunSpec range: -10 to +10)
        # Corrupted scale factors produce astronomical values used as "ground truth"
        for name, sf in [('DCA', sf_dca), ('DCV', sf_dcv), ('DCW', sf_dcw), ('DCWH', sf_dcwh)]:
            if sf < -10 or sf > 10:
                self.log.warning(f"Inverter {unit_id}: MPPT {name} scale factor out of range: {sf}")
                return None

        # Extract global data (offset 6-9, i.e., 40260-40263)
        # Evt at offset 6-7, N at offset 8, TmsPer at offset 9
        num_modules = regs[8]
        self.log.debug(f"Inverter {unit_id}: MPPT has {num_modules} module(s)")

        modules = []

        # Module 1 data starts at offset 10 (40264 - 40254 = 10)
        m1_regs = regs[10:30]  # 20 registers
        if len(m1_regs) >= 17:
            module1 = self._parse_mppt_module_optimized(m1_regs, 1, sf_dca, sf_dcv, sf_dcw, sf_dcwh)
            if module1:
                modules.append(module1)

        # Module 2 data starts at offset 30 (40284 - 40254 = 30)
        if num_modules >= 2:
            m2_regs = regs[30:48]  # 18 registers (up to Tmp at offset 16)
            if len(m2_regs) >= 17:
                module2 = self._parse_mppt_module_optimized(m2_regs, 2, sf_dca, sf_dcv, sf_dcw, sf_dcwh)
                if module2:
                    modules.append(module2)

        if not modules:
            return None

        return {
            'num_modules': num_modules,
            'modules': modules
        }

    def _parse_mppt_module_optimized(self, regs: List[int], module_id: int,
                                       sf_dca: int, sf_dcv: int, sf_dcw: int, sf_dcwh: int) -> Optional[Dict]:
        """
        Parse a single MPPT module's registers (optimized version without DCSt).

        Offsets within module block:
        0: ID, 1-8: IDStr, 9: DCA, 10: DCV, 11: DCW, 12-13: DCWH, 14-15: Tms, 16: Tmp
        """
        if len(regs) < 17:
            return None

        dca_raw = regs[9]
        dcv_raw = regs[10]
        dcw_raw = regs[11]
        dcwh_raw = (regs[12] << 16) | regs[13]
        tmp_raw = regs[16] if regs[16] < 32768 else regs[16] - 65536

        # Check for not-implemented values (0xFFFF for uint16)
        if dcv_raw == 0xFFFF:
            return None

        # Apply scale factors (round negative SF to eliminate IEEE 754 float noise)
        def _scale(raw, sf, not_impl=0xFFFF):
            if raw == not_impl:
                return None
            result = raw * (10 ** sf)
            return round(result, -sf) if sf < 0 else result

        dc_current = _scale(dca_raw, sf_dca)
        dc_voltage = _scale(dcv_raw, sf_dcv)
        dc_power = _scale(dcw_raw, sf_dcw)
        dc_energy = _scale(dcwh_raw, sf_dcwh, not_impl=0xFFFFFFFF)
        temperature = tmp_raw if tmp_raw != -32768 else None

        return {
            'id': module_id,
            'dc_current': dc_current,
            'dc_voltage': dc_voltage,
            'dc_power': dc_power,
            'dc_energy': dc_energy,
            'temperature': temperature
        }

    def _read_immediate_controls(self, unit_id: int, max_retries: int = 3) -> Optional[Dict]:
        """
        Read Model 123 - Immediate Controls with retry on failure.

        Reads inverter control settings:
        - Connection status
        - Power limit percentage
        - Power factor settings
        - Reactive power settings

        Returns dict with control values, ready for future write operations.
        """
        # Force connection reset before Model 123 to clear DataManager's buffer
        # This prevents getting stale Model 160 data
        self.connection.connected = False
        time.sleep(0.5)  # Brief pause before reconnect

        for attempt in range(max_retries):
            regs = self.connection.read_registers(40228, 26, unit_id)

            if not regs or len(regs) < 26:
                if attempt < max_retries - 1:
                    self.log.debug(f"Inverter {unit_id}: Model 123 read failed, retry {attempt + 1}/{max_retries}")
                    time.sleep(1.0)  # Wait 1s before retry
                    continue
                self.log.warning(f"Inverter {unit_id}: Model 123 read failed after {max_retries} attempts")
                return None

            # Verify model ID
            model_id = regs[0]
            if model_id != 123:
                if attempt < max_retries - 1:
                    self.log.debug(f"Inverter {unit_id}: Model 123 mismatch (got {model_id}), retry {attempt + 1}/{max_retries}")
                    time.sleep(1.0)  # Wait 1s before retry
                    continue
                self.log.warning(f"Inverter {unit_id}: Model 123 mismatch (got {model_id}) after {max_retries} attempts")
                return None

            # Success - break out of retry loop
            break

        # Extract scale factors (at end of block)
        sf_wmax = regs[23] if regs[23] < 32768 else regs[23] - 65536  # WMaxLimPct_SF
        sf_pf = regs[24] if regs[24] < 32768 else regs[24] - 65536    # OutPFSet_SF
        sf_var = regs[25] if regs[25] < 32768 else regs[25] - 65536   # VArPct_SF

        # Connection control
        conn_win_tms = regs[2]
        conn_rvrt_tms = regs[3]
        conn = regs[4]

        # Power limit
        wmax_lim_pct_raw = regs[5]
        wmax_lim_pct = wmax_lim_pct_raw * (10 ** sf_wmax) if wmax_lim_pct_raw != 0xFFFF else None
        wmax_win_tms = regs[6]
        wmax_rvrt_tms = regs[7]
        wmax_rmp_tms = regs[8]
        wmax_ena = regs[9]

        # Power factor
        # Fronius devices report PF inconsistently:
        # - Symo: percentage format (0-100), SF may be 0 or -2
        # - Primo: higher precision (0-10000), SF=-2 but needs -4
        pf_raw = regs[10] if regs[10] < 32768 else regs[10] - 65536
        if regs[10] != 0xFFFF and abs(pf_raw) > 100:
            sf_pf = -4  # High precision format - force correct scale
        elif sf_pf == 0:
            sf_pf = -2  # Zero SF with low precision value
        pf = pf_raw * (10 ** sf_pf) if regs[10] != 0xFFFF else None
        pf_win_tms = regs[11]
        pf_rvrt_tms = regs[12]
        pf_rmp_tms = regs[13]
        pf_ena = regs[14]

        # Reactive power
        var_wmax_pct_raw = regs[15] if regs[15] < 32768 else regs[15] - 65536
        var_max_pct_raw = regs[16] if regs[16] < 32768 else regs[16] - 65536
        var_aval_pct_raw = regs[17] if regs[17] < 32768 else regs[17] - 65536
        var_win_tms = regs[18]
        var_rvrt_tms = regs[19]
        var_rmp_tms = regs[20]
        var_mod = regs[21]
        var_ena = regs[22]

        return {
            # Connection
            'connected': conn == 1,
            'conn_state': conn,
            'conn_win_tms': conn_win_tms,
            'conn_rvrt_tms': conn_rvrt_tms,

            # Power limit
            'power_limit_pct': wmax_lim_pct,
            'power_limit_pct_raw': wmax_lim_pct_raw,
            'power_limit_enabled': wmax_ena == 1,
            'power_limit_win_tms': wmax_win_tms,
            'power_limit_rvrt_tms': wmax_rvrt_tms,
            'power_limit_rmp_tms': wmax_rmp_tms,

            # Power factor
            'power_factor': pf,
            'power_factor_raw': regs[10],
            'power_factor_enabled': pf_ena == 1,
            'power_factor_win_tms': pf_win_tms,
            'power_factor_rvrt_tms': pf_rvrt_tms,
            'power_factor_rmp_tms': pf_rmp_tms,

            # Reactive power (VAR)
            'var_wmax_pct': var_wmax_pct_raw * (10 ** sf_var) if var_wmax_pct_raw != -32768 else None,
            'var_max_pct': var_max_pct_raw * (10 ** sf_var) if var_max_pct_raw != -32768 else None,
            'var_aval_pct': var_aval_pct_raw * (10 ** sf_var) if var_aval_pct_raw != -32768 else None,
            'var_mode': var_mod,
            'var_enabled': var_ena == 1,
            'var_win_tms': var_win_tms,
            'var_rvrt_tms': var_rvrt_tms,
            'var_rmp_tms': var_rmp_tms,

            # Scale factors (needed for future write operations)
            '_sf_wmax': sf_wmax,
            '_sf_pf': sf_pf,
            '_sf_var': sf_var
        }

    def queue_power_limit_command(self, cmd: PowerLimitCommand) -> bool:
        """Validate and enqueue a power limit command.

        Called from MQTT thread — must be thread-safe.

        Returns:
            True if command was queued successfully
        """
        if not self.write_config or not self.write_config.enabled:
            self.log.warning(f"Inverter {cmd.device_id}: write rejected — writes disabled")
            return False

        # Validate device exists
        inv_ids = [inv['device_id'] for inv in self.inverters]
        if cmd.device_id not in inv_ids:
            self.log.warning(f"Inverter {cmd.device_id}: write rejected — unknown device")
            return False

        # Validate range
        min_pct = self.write_config.min_power_limit_pct
        max_pct = self.write_config.max_power_limit_pct
        if not (min_pct <= cmd.limit_pct <= max_pct):
            self.log.warning(
                f"Inverter {cmd.device_id}: write rejected — "
                f"limit {cmd.limit_pct}% outside range [{min_pct}, {max_pct}]"
            )
            return False

        try:
            self._command_queue.put_nowait(cmd)
            self.log.info(
                f"Inverter {cmd.device_id}: power limit command queued "
                f"({cmd.limit_pct}%, source={cmd.source})"
            )
            return True
        except queue.Full:
            self.log.warning(f"Inverter {cmd.device_id}: command queue full, rejecting")
            return False

    def _process_pending_commands(self):
        """Process one pending write command from the queue.

        Called from the poller thread between poll cycles — serialized with reads.
        Only processes one command per cycle to avoid blocking polling for too long
        (each write takes ~5s: reset + pre-read + write + stabilization + readback).
        """
        try:
            cmd = self._command_queue.get_nowait()
        except queue.Empty:
            return

        result = self._execute_power_limit_write(cmd)

        # Re-queue rate-limited commands (will retry next poll cycle)
        if result.get('status') == 'rate_limited':
            try:
                self._command_queue.put_nowait(cmd)
            except queue.Full:
                pass  # Drop if queue is full
            return  # Don't publish rate_limited as a result — it's transient

        # Publish result via callback
        if self._command_result_callback:
            try:
                self._command_result_callback(
                    str(cmd.device_id), "set_power_limit", result
                )
            except Exception as e:
                self.log.error(f"Inverter {cmd.device_id}: result callback error: {e}")

    def _execute_power_limit_write(self, cmd: PowerLimitCommand) -> dict:
        """Execute the 11-step safety protocol for power limit write.

        Returns:
            Result dict with status, before/after values
        """
        device_id = cmd.device_id
        result = {
            'command': 'set_power_limit',
            'device_id': device_id,
            'requested_pct': cmd.limit_pct,
            'source': cmd.source,
            'timestamp': time.time(),
        }

        # Step 1: Rate limit check (auto_revert/shutdown bypass — safety critical)
        now = time.time()
        last_write = self._last_write_time.get(device_id, 0)
        if cmd.source not in ("auto_revert", "shutdown") and now - last_write < self.write_config.rate_limit_seconds:
            remaining = int(self.write_config.rate_limit_seconds - (now - last_write))
            self.log.warning(
                f"Inverter {device_id}: write rate limited — "
                f"wait {remaining}s"
            )
            result['status'] = 'rate_limited'
            result['reason'] = f'rate limited, retry in {remaining}s'
            return result

        # Step 2: Validate range (already checked in queue, but double-check)
        min_pct = self.write_config.min_power_limit_pct
        max_pct = self.write_config.max_power_limit_pct
        if not (min_pct <= cmd.limit_pct <= max_pct):
            result['status'] = 'rejected'
            result['reason'] = f'limit {cmd.limit_pct}% outside [{min_pct}, {max_pct}]'
            self._write_failures += 1
            return result

        # Step 3: Force connection reset (clear DataManager buffer)
        self.connection.connected = False
        time.sleep(0.5)

        # Step 4: Pre-write read — read Model 123 to verify and get current SF
        pre_regs = self.connection.read_registers(40228, 26, device_id)
        if not pre_regs or len(pre_regs) < 26:
            self.log.error(f"Inverter {device_id}: pre-write Model 123 read failed")
            result['status'] = 'error'
            result['reason'] = 'pre-write read failed'
            self._write_failures += 1
            return result

        # Step 5: Verify model_id=123
        model_id = pre_regs[0]
        if model_id != 123:
            self.log.error(
                f"Inverter {device_id}: Model 123 verification failed "
                f"(got {model_id})"
            )
            result['status'] = 'error'
            result['reason'] = f'model verification failed (got {model_id})'
            self._write_failures += 1
            return result

        # Get current scale factor and values
        sf_wmax = pre_regs[23] if pre_regs[23] < 32768 else pre_regs[23] - 65536
        before_raw = pre_regs[5]
        before_pct = before_raw * (10 ** sf_wmax) if before_raw != 0xFFFF else None
        before_ena = pre_regs[9]

        result['before_pct'] = before_pct
        result['before_enabled'] = before_ena == 1
        result['scale_factor'] = sf_wmax

        self.log.warning(
            f"Inverter {device_id}: pre-write state — "
            f"WMaxLimPct={before_pct}% (raw={before_raw}, SF={sf_wmax}), "
            f"enabled={before_ena}"
        )

        # Step 6: Calculate raw register value
        raw_limit = int(cmd.limit_pct / (10 ** sf_wmax))

        # Step 7: Write 5 registers atomically at 40233-40237
        # [WMaxLimPct, WMaxLimPct_WinTms, WMaxLimPct_RvrtTms, WMaxLimPct_RmpTms, WMaxLim_Ena]
        enable_val = 1  # Enable power limiting
        write_values = [raw_limit, 0, cmd.revert_timeout, cmd.ramp_time, enable_val]

        self.log.warning(
            f"Inverter {device_id}: writing Model 123 registers 40233-40237: "
            f"[{raw_limit} ({cmd.limit_pct}%), 0, {cmd.revert_timeout}, "
            f"{cmd.ramp_time}, {enable_val}]"
        )

        if not self.connection.write_registers(40233, write_values, device_id):
            self.log.error(f"Inverter {device_id}: register write failed")
            result['status'] = 'error'
            result['reason'] = 'register write failed'
            self._write_failures += 1
            return result

        # Step 8: Wait stabilization delay
        self.log.info(
            f"Inverter {device_id}: waiting {self.write_config.stabilization_delay}s "
            f"for DataManager stabilization"
        )
        time.sleep(self.write_config.stabilization_delay)

        # Step 9: Read-back verification — force reset and re-read Model 123
        self.connection.connected = False
        time.sleep(0.5)

        post_regs = self.connection.read_registers(40228, 26, device_id)
        if not post_regs or len(post_regs) < 26:
            self.log.warning(f"Inverter {device_id}: post-write read-back failed (write may have succeeded)")
            result['status'] = 'unverified'
            result['reason'] = 'read-back failed'
        else:
            post_model = post_regs[0]
            if post_model != 123:
                self.log.warning(
                    f"Inverter {device_id}: read-back Model 123 mismatch "
                    f"(got {post_model}), write may have succeeded"
                )
                result['status'] = 'unverified'
                result['reason'] = f'read-back model mismatch (got {post_model})'
            else:
                post_sf = post_regs[23] if post_regs[23] < 32768 else post_regs[23] - 65536
                post_raw = post_regs[5]
                post_pct = post_raw * (10 ** post_sf) if post_raw != 0xFFFF else None
                post_ena = post_regs[9]

                result['after_pct'] = post_pct
                result['after_enabled'] = post_ena == 1

                # Verify: compare with tolerance (scale factor rounding)
                if post_pct is not None and abs(post_pct - cmd.limit_pct) < 1.0:
                    result['status'] = 'success'
                    self.log.warning(
                        f"Inverter {device_id}: power limit set to {post_pct}% "
                        f"(was {before_pct}%)"
                    )
                else:
                    result['status'] = 'mismatch'
                    result['reason'] = f'expected ~{cmd.limit_pct}%, read-back {post_pct}%'
                    self.log.error(
                        f"Inverter {device_id}: read-back mismatch — "
                        f"expected ~{cmd.limit_pct}%, got {post_pct}%"
                    )

        # Step 10: Update tracking
        self._last_write_time[device_id] = time.time()
        self._write_count += 1

        if result.get('status') in ('success', 'unverified'):
            self._active_limits[device_id] = {
                'limit_pct': cmd.limit_pct,
                'set_at': time.time(),
                'source': cmd.source,
            }
            # Set auto-revert timer (only for non-100% limits)
            if (self.write_config.auto_revert_seconds > 0
                    and cmd.limit_pct < 100.0
                    and cmd.source != "auto_revert"):
                self._auto_revert_timers[device_id] = (
                    time.time() + self.write_config.auto_revert_seconds
                )
                self.log.info(
                    f"Inverter {device_id}: auto-revert to 100% in "
                    f"{self.write_config.auto_revert_seconds}s"
                )
            else:
                # Clear auto-revert timer for 100% or auto_revert commands
                self._auto_revert_timers.pop(device_id, None)
                self._active_limits.pop(device_id, None)

        # Step 11: Force controls re-read on next poll cycle
        self._last_controls_read.pop(device_id, None)

        return result

    def _check_auto_revert(self):
        """Check for expired auto-revert timers and queue restore commands."""
        now = time.time()
        expired = [
            dev_id for dev_id, expires_at in self._auto_revert_timers.items()
            if now >= expires_at
        ]
        for device_id in expired:
            active = self._active_limits.get(device_id, {})
            self.log.warning(
                f"Inverter {device_id}: auto-reverting power limit to 100% "
                f"(was {active.get('limit_pct', '?')}%, "
                f"set {int(now - active.get('set_at', now))}s ago)"
            )
            cmd = PowerLimitCommand(
                device_id=device_id,
                limit_pct=100.0,
                source="auto_revert",
            )
            try:
                self._command_queue.put_nowait(cmd)
            except queue.Full:
                self.log.error(f"Inverter {device_id}: auto-revert failed — queue full")
            # Remove timer regardless (will be re-checked next cycle)
            self._auto_revert_timers.pop(device_id, None)

    def get_write_stats(self) -> Dict:
        """Get write operation statistics."""
        return {
            'writes_total': self._write_count,
            'writes_failed': self._write_failures,
            'active_limits': {
                str(k): v for k, v in self._active_limits.items()
            },
            'queue_size': self._command_queue.qsize(),
        }

    def _poll_meter(self, device_info: Dict, max_retries: int = 3) -> bool:
        """Poll a single meter with retry on failure."""
        unit_id = device_info['device_id']

        # Check if device is in backoff period (offline with exponential delay)
        if self._is_device_in_backoff(device_info, 'meter'):
            return False

        # Force connection reset to flush DataManager TCP buffer
        # (same issue as inverters: stale data in shared buffer)
        self.connection.connected = False
        time.sleep(0.3)

        regs = None
        for attempt in range(max_retries):
            regs = self.connection.read_registers(40072, 53, unit_id)

            if regs and len(regs) >= 53:
                break  # Success

            if attempt < max_retries - 1:
                self.log.debug(f"Meter {unit_id}: read failed, retry {attempt + 1}/{max_retries}")
                time.sleep(0.5)
            else:
                self.log.warning(f"Meter {unit_id}: read failed after {max_retries} attempts")
                self._update_runtime_on_failure(device_info, 'meter')
                return False

        data = self.parser.parse_meter_measurements(regs)
        if not data:
            self.log.warning(f"Meter {unit_id}: parse returned empty data")
            self._update_runtime_on_failure(device_info, 'meter')
            return False

        data['device_id'] = unit_id
        data['serial_number'] = device_info.get('serial_number', '')
        data['model'] = device_info.get('model', '')

        self.publish_callback(unit_id, 'meter', data)
        self._update_runtime_on_success(device_info, 'meter')
        self.log.debug(f"Meter {unit_id}: published (W={data.get('power_total', 0)})")
        return True

    def _check_host_available(self) -> bool:
        """Check if DataManager is reachable via ping."""
        if not self.modbus_config.ping_check_enabled:
            return True

        return ping_host(self.modbus_config.host, timeout=2)

    def _is_night_time(self) -> bool:
        """Check if current time is within configured night hours."""
        if not self.modbus_config.night_mode_enabled:
            return False

        return is_night_time(
            self.modbus_config.night_start_hour,
            self.modbus_config.night_end_hour
        )

    def _enter_sleep_mode(self, reason: str):
        """Enter sleep mode - reduce polling frequency."""
        if not self._in_sleep_mode:
            self._in_sleep_mode = True
            self._sleep_mode_start = time.time()
            self.log.info(f"DevicePoller: Entering sleep mode - {reason}")
            self.log.info(f"DevicePoller: Will poll every {self.modbus_config.night_poll_interval}s")

    def _exit_sleep_mode(self):
        """Exit sleep mode - resume normal polling."""
        if self._in_sleep_mode:
            duration = time.time() - self._sleep_mode_start if self._sleep_mode_start else 0
            self.log.info(f"DevicePoller: Exiting sleep mode after {int(duration)}s")
            self._in_sleep_mode = False
            self._sleep_mode_start = None
            self._consecutive_failures = 0

    def _get_poll_interval(self) -> float:
        """Get current polling interval based on mode."""
        if self._in_sleep_mode:
            return self.modbus_config.night_poll_interval
        return self.poll_delay

    def get_status(self) -> Dict:
        """Get current poller status for health reporting."""
        return {
            'in_sleep_mode': self._in_sleep_mode,
            'consecutive_failures': self._consecutive_failures,
            'last_successful_poll': self._last_successful_poll,
            'is_night_time': self._is_night_time(),
            'connected': self.connection.connected if self.connection else False
        }

    def run(self):
        self.running = True
        inv_ids = [inv['device_id'] for inv in self.inverters]
        meter_ids = [m['device_id'] for m in self.meters]
        self.log.info(f"DevicePoller: started for inverters {inv_ids}, meters {meter_ids}")
        self.log.info(f"DevicePoller: {self.poll_delay}s delay between devices")

        if self.modbus_config.night_mode_enabled:
            self.log.info(f"DevicePoller: Night mode enabled ({self.modbus_config.night_start_hour}:00-{self.modbus_config.night_end_hour}:00)")

        while self.running:
            # Check if host is available (ping check)
            if self.modbus_config.ping_check_enabled:
                if not self._check_host_available():
                    if self._is_night_time():
                        self._enter_sleep_mode("DataManager not responding (night time)")
                    else:
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= self.modbus_config.consecutive_failures_for_sleep:
                            self._enter_sleep_mode(f"DataManager not responding ({self._consecutive_failures} failures)")

                    # Sleep and retry
                    self._stop_event.wait(self._get_poll_interval())
                    continue

            # Try to connect if not connected
            if not self.connection.connected:
                if not self.connection.connect():
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self.modbus_config.consecutive_failures_for_sleep:
                        self._enter_sleep_mode(f"Modbus connection failed ({self._consecutive_failures} failures)")
                    self._stop_event.wait(self._get_poll_interval())
                    continue

            # Poll all devices
            poll_success = False

            # Skip inverter polling at night (DataManager returns garbage from sleeping inverters)
            skip_inverters = self._is_night_time() and self.modbus_config.night_skip_inverters
            if skip_inverters:
                if not self._night_skip_logged:
                    self.log.info("DevicePoller: Skipping inverter polling (night mode — DataManager returns stale data)")
                    self._night_skip_logged = True
            else:
                if self._night_skip_logged:
                    self.log.info("DevicePoller: Resuming inverter polling (dawn detected)")
                    self._night_skip_logged = False

            # Poll all inverters (unless night skip)
            if not skip_inverters:
                for device_info in self.inverters:
                    if not self.running:
                        break
                    if self._poll_inverter(device_info):
                        poll_success = True
                    if self._stop_event.wait(self.poll_delay):
                        break

            # Poll all meters (always — meter data is valid at night)
            for device_info in self.meters:
                if not self.running:
                    break
                if self._poll_meter(device_info):
                    poll_success = True
                if self._stop_event.wait(self.poll_delay):
                    break

            # Update status based on poll results
            if poll_success:
                self._last_successful_poll = time.time()
                self._consecutive_failures = 0
                if self._in_sleep_mode:
                    self._exit_sleep_mode()
            else:
                self._consecutive_failures += 1
                self.log.debug(f"DevicePoller: Poll cycle failed ({self._consecutive_failures} consecutive)")

                # Check if we should enter sleep mode
                if self._consecutive_failures >= self.modbus_config.consecutive_failures_for_sleep:
                    if self._is_night_time():
                        self._enter_sleep_mode("No data received (night time)")
                    else:
                        self._enter_sleep_mode(f"No data after {self._consecutive_failures} attempts")

            # Process pending write commands (between poll cycles)
            if self.write_config and self.write_config.enabled:
                self._process_pending_commands()
                self._check_auto_revert()

            # Sleep before next poll cycle (always sleep, just different intervals)
            self._stop_event.wait(self._get_poll_interval())

        self.connection.disconnect()
        self.log.info("DevicePoller: stopped")

    def restore_all_power_limits(self):
        """Restore all active power limits to 100% during shutdown."""
        if not self._active_limits:
            return

        for device_id in list(self._active_limits.keys()):
            active = self._active_limits[device_id]
            self.log.warning(
                f"Inverter {device_id}: shutdown — restoring power limit to 100% "
                f"(was {active.get('limit_pct', '?')}%)"
            )
            cmd = PowerLimitCommand(
                device_id=device_id,
                limit_pct=100.0,
                source="shutdown",
            )
            result = self._execute_power_limit_write(cmd)
            if result.get('status') == 'success':
                self.log.warning(f"Inverter {device_id}: power limit restored to 100%")
            else:
                self.log.error(
                    f"Inverter {device_id}: failed to restore power limit — "
                    f"{result.get('status')}: {result.get('reason', 'unknown')}"
                )

    def stop(self):
        self.running = False
        self._stop_event.set()  # Interrupt sleep immediately


class FroniusModbusClient:
    """Main Modbus client managing connection and pollers."""

    def __init__(self, modbus_config: ModbusConfig, devices_config: DevicesConfig,
                 register_map: Dict, publish_callback: Callable = None,
                 debug_config: DebugConfig = None,
                 write_config: WriteConfig = None,
                 command_result_callback: Callable = None):
        self.modbus_config = modbus_config
        self.devices_config = devices_config
        self.debug_config = debug_config or DebugConfig()
        self.write_config = write_config
        self.command_result_callback = command_result_callback
        self.parser = RegisterParser(register_map, debug_config=self.debug_config)
        self.log = get_logger()

        # Discovery connection (separate from polling connections)
        self.connection = ModbusConnection(modbus_config, self.parser)
        self.publish_callback = publish_callback or (lambda *args: None)

        # Single device poller
        self.device_poller: DevicePoller = None

        self.inverters: List[Dict] = []
        self.meters: List[Dict] = []
        self.connected = False

    def connect(self) -> bool:
        self.connected = self.connection.connect()
        return self.connected

    def disconnect(self):
        # Stop poller (safe to call even if already stopped)
        if self.device_poller:
            self.device_poller.stop()
            if self.device_poller.is_alive():
                self.device_poller.join(timeout=10)

        # Disconnect poller's connection (run() disconnects on exit,
        # but if poller was stopped externally before run() finished, clean up)
        if self.device_poller and self.device_poller.connection:
            try:
                self.device_poller.connection.disconnect()
            except Exception:
                pass

        # Disconnect discovery connection
        self.connection.disconnect()
        self.connected = False

    def discover_devices(self, device_filter: str = 'all') -> tuple:
        """Discover configured devices.

        Args:
            device_filter: 'all', 'inverter', or 'meter' - which device types to discover
        """
        self.inverters = []
        self.meters = []

        self.log.info("Discovering devices...")

        # Discover inverters if filter allows
        if device_filter in ('all', 'inverter'):
            for unit_id in self.devices_config.inverters:
                info = self.connection.identify_device(unit_id)
                if info:
                    # Check if inverter has storage support (Model 124)
                    info['has_storage'] = self.connection.check_storage_support(unit_id)
                    self.inverters.append(info)
                else:
                    self.log.warning(f"No inverter at ID {unit_id}")
                time.sleep(0.5)

        # Discover meters if filter allows
        if device_filter in ('all', 'meter'):
            for unit_id in self.devices_config.meters:
                info = self.connection.identify_device(unit_id)
                if info:
                    self.meters.append(info)
                else:
                    self.log.warning(f"No meter at ID {unit_id}")
                time.sleep(0.5)

        # Count devices with storage
        storage_count = sum(1 for inv in self.inverters if inv.get('has_storage'))
        self.log.info(f"Found: {len(self.inverters)} inverter(s), {len(self.meters)} meter(s), {storage_count} with storage")
        return self.inverters, self.meters

    def start_polling(self):
        """Start single polling thread for all devices."""
        # Close discovery connection before starting poller
        self.connection.disconnect()

        # Start single device poller for all inverters and meters
        if self.inverters or self.meters:
            self.device_poller = DevicePoller(
                modbus_config=self.modbus_config,
                inverters=self.inverters,
                meters=self.meters,
                poll_delay=self.devices_config.inverter_poll_delay,
                read_delay_ms=self.devices_config.inverter_read_delay_ms,
                parser=self.parser,
                publish_callback=self.publish_callback,
                debug_config=self.debug_config,
                write_config=self.write_config,
                command_result_callback=self.command_result_callback,
            )
            self.device_poller.start()
            self.log.info("Started single DevicePoller thread for all devices")

    def get_stats(self) -> Dict:
        # Aggregate stats from all connections
        successful = self.connection.successful_reads
        failed = self.connection.failed_reads

        if self.device_poller and self.device_poller.connection:
            successful += self.device_poller.connection.successful_reads
            failed += self.device_poller.connection.failed_reads

        return {
            'connected': self.connected,
            'successful_reads': successful,
            'failed_reads': failed,
            'inverters': len(self.inverters),
            'meters': len(self.meters),
        }
