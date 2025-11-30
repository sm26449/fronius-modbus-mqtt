"""Modbus TCP Client with simple sequential polling for Fronius devices

Architecture:
- MeterPoller: Thread that reads meter, publishes to MQTT, sleeps, repeats
- InverterPoller: Thread that cycles through inverters one by one with pauses
- Single shared Modbus connection
"""

import time
import logging
import threading
from typing import Dict, List, Optional, Callable

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .config import ModbusConfig, DevicesConfig
from .register_parser import RegisterParser
from .logging_setup import get_logger

# Suppress pymodbus exception logging
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


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
                        slave=unit_id
                    )

                    if not result.isError():
                        self.successful_reads += 1
                        self.last_unit_id = unit_id
                        return result.registers
                    else:
                        if attempt < self.config.retry_attempts - 1:
                            time.sleep(self.config.retry_delay)

                except Exception as e:
                    self.log.debug(f"Unit {unit_id}: read error - {e}")
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)

            self.failed_reads += 1
            return None

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

        time.sleep(0.1)

        # Read model ID
        model_regs = self.read_registers(40070, 1, unit_id)
        if model_regs:
            model_id = model_regs[0]
            device_info['model_id'] = model_id
            if model_id in self.INVERTER_MODELS:
                device_info['device_type'] = 'inverter'
                device_info['inverter_type'] = self.parser.detect_inverter_type(device_info['model'])
            elif model_id in self.METER_MODELS:
                device_info['device_type'] = 'meter'

        self.log.info(f"Device {unit_id}: {device_info['manufacturer']} {device_info['model']} (SN: {device_info['serial_number']})")
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
    """

    ACTIVE_STATUS_CODES = [4, 5]
    STORAGE_ADDRESS = 40343  # Model 124 data starts here (Int+SF format)
    STORAGE_LENGTH = 24      # Model 124 has 24 registers
    CONTROLS_POLL_INTERVAL = 60  # Read Model 123 every 60 seconds

    def __init__(self, modbus_config: ModbusConfig, inverters: List[Dict],
                 meters: List[Dict], poll_delay: float, read_delay_ms: int,
                 parser: RegisterParser, publish_callback: Callable):
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

        # Single connection for all devices
        self.connection = ModbusConnection(modbus_config, parser)

        # Track last controls read time per inverter
        self._last_controls_read: Dict[int, float] = {}

    def _poll_inverter(self, device_info: Dict, max_retries: int = 3) -> bool:
        """Poll a single inverter with retry on failure."""
        unit_id = device_info['device_id']

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
                self.log.debug(f"Inverter {unit_id}: main register read failed after {max_retries} attempts")
                # Force reconnect on next read to clear any buffer issues
                self.connection.connected = False
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

        # Parse status
        data['status'] = self.parser.parse_status(data.get('status_code', 0))
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

        # Read MPPT Model 160 in single optimized query
        # Force connection reset to clear DataManager buffer after main registers
        self.connection.connected = False
        time.sleep(0.3)
        mppt_data = self._read_mppt_data(unit_id)
        if mppt_data and mppt_data.get('modules'):
            data['mppt'] = mppt_data
            for i, mod in enumerate(mppt_data['modules']):
                self.log.info(f"Inverter {unit_id} MPPT{i+1}: "
                              f"V={mod.get('dc_voltage', 0):.1f}V, "
                              f"I={mod.get('dc_current', 0):.2f}A, "
                              f"P={mod.get('dc_power', 0):.0f}W")

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

        # Publish to MQTT
        self.publish_callback(unit_id, 'inverter', data)
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
                self.log.debug(f"Inverter {unit_id}: MPPT read failed after {max_retries} attempts")
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

        # Apply scale factors
        dc_current = dca_raw * (10 ** sf_dca) if dca_raw != 0xFFFF else None
        dc_voltage = dcv_raw * (10 ** sf_dcv) if dcv_raw != 0xFFFF else None
        dc_power = dcw_raw * (10 ** sf_dcw) if dcw_raw != 0xFFFF else None
        dc_energy = dcwh_raw * (10 ** sf_dcwh) if dcwh_raw != 0xFFFFFFFF else None
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
                self.log.debug(f"Inverter {unit_id}: Model 123 read failed after {max_retries} attempts")
                return None

            # Verify model ID
            model_id = regs[0]
            if model_id != 123:
                if attempt < max_retries - 1:
                    self.log.debug(f"Inverter {unit_id}: Model 123 mismatch (got {model_id}), retry {attempt + 1}/{max_retries}")
                    time.sleep(1.0)  # Wait 1s before retry
                    continue
                self.log.debug(f"Inverter {unit_id}: Model 123 mismatch (got {model_id}) after {max_retries} attempts")
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
        pf_raw = regs[10] if regs[10] < 32768 else regs[10] - 65536
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

    # Future write methods placeholder:
    # def write_power_limit(self, unit_id: int, limit_pct: float, ...) -> bool:
    # def write_power_factor(self, unit_id: int, pf: float, ...) -> bool:
    # def write_connection(self, unit_id: int, connect: bool, ...) -> bool:

    def _poll_meter(self, device_info: Dict, max_retries: int = 3) -> bool:
        """Poll a single meter with retry on failure."""
        unit_id = device_info['device_id']

        regs = None
        for attempt in range(max_retries):
            regs = self.connection.read_registers(40072, 53, unit_id)

            if regs and len(regs) >= 53:
                break  # Success

            if attempt < max_retries - 1:
                self.log.debug(f"Meter {unit_id}: read failed, retry {attempt + 1}/{max_retries}")
                time.sleep(0.5)
            else:
                self.log.debug(f"Meter {unit_id}: read failed after {max_retries} attempts")
                return False

        data = self.parser.parse_meter_measurements(regs)
        data['device_id'] = unit_id
        data['serial_number'] = device_info.get('serial_number', '')
        data['model'] = device_info.get('model', '')

        self.publish_callback(unit_id, 'meter', data)
        self.log.debug(f"Meter {unit_id}: published (W={data.get('power_total', 0)})")
        return True

    def run(self):
        self.running = True
        inv_ids = [inv['device_id'] for inv in self.inverters]
        meter_ids = [m['device_id'] for m in self.meters]
        self.log.info(f"DevicePoller: started for inverters {inv_ids}, meters {meter_ids}")
        self.log.info(f"DevicePoller: {self.poll_delay}s delay between devices")

        # Connect
        if not self.connection.connect():
            self.log.error("DevicePoller: Failed to connect to Modbus")
            return

        while self.running:
            # Poll all inverters
            for device_info in self.inverters:
                if not self.running:
                    break
                self._poll_inverter(device_info)
                time.sleep(self.poll_delay)

            # Poll all meters
            for device_info in self.meters:
                if not self.running:
                    break
                self._poll_meter(device_info)
                time.sleep(self.poll_delay)

        self.connection.disconnect()
        self.log.info("DevicePoller: stopped")

    def stop(self):
        self.running = False


# Keep old class names for backward compatibility
class InverterPoller(DevicePoller):
    """Backward compatibility wrapper."""
    def __init__(self, modbus_config: ModbusConfig, inverters: List[Dict],
                 poll_delay: float, read_delay_ms: int, parser: RegisterParser,
                 publish_callback: Callable):
        super().__init__(modbus_config, inverters, [], poll_delay, read_delay_ms,
                        parser, publish_callback)


class MeterPoller(DevicePoller):
    """Backward compatibility wrapper."""
    def __init__(self, modbus_config: ModbusConfig, meters: List[Dict],
                 poll_interval: float, parser: RegisterParser, publish_callback: Callable):
        super().__init__(modbus_config, [], meters, poll_interval, 500,
                        parser, publish_callback)


class FroniusModbusClient:
    """Main Modbus client managing connection and pollers."""

    def __init__(self, modbus_config: ModbusConfig, devices_config: DevicesConfig,
                 register_map: Dict, publish_callback: Callable = None):
        self.modbus_config = modbus_config
        self.devices_config = devices_config
        self.parser = RegisterParser(register_map)
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
        # Stop poller
        if self.device_poller:
            self.device_poller.stop()
            self.device_poller.join(timeout=10)

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
                publish_callback=self.publish_callback
            )
            self.device_poller.start()
            self.log.info("Started single DevicePoller thread for all devices")

    def poll_all_devices(self) -> Dict:
        """For compatibility - data is published via callback."""
        return {'inverters': {}, 'meters': {}, 'timestamp': time.time()}

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
