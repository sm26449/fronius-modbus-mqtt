"""Register value parsing and scale factor handling for SunSpec Modbus"""

import struct
import json
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from .logging_setup import get_logger


class RegisterParser:
    """
    Parse Modbus register values with SunSpec conventions.

    Features:
    - Scale factor application
    - Data type conversion (int16, uint16, int32, uint32, string, etc.)
    - Event flag bitmask parsing
    - State code translation
    """

    # Special values indicating "not implemented"
    NOT_IMPLEMENTED_UINT16 = 0xFFFF
    NOT_IMPLEMENTED_INT16 = 0x8000
    NOT_IMPLEMENTED_UINT32 = 0xFFFFFFFF
    NOT_IMPLEMENTED_INT32 = 0x80000000

    def __init__(self, register_map: Dict):
        """
        Initialize parser with register map.

        Args:
            register_map: Register definitions loaded from registers.json
        """
        self.register_map = register_map
        self.log = get_logger()
        self.event_flags = self._load_event_flags()
        self.status_codes = register_map.get('status_codes', {})
        self.state_codes = register_map.get('state_codes', {})

    def _load_event_flags(self) -> Dict:
        """Load event flags from FroniusEventFlags.json"""
        try:
            event_flags_path = Path(__file__).parent.parent / 'config' / 'FroniusEventFlags.json'
            with open(event_flags_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.log.warning(f"Could not load event flags: {e}")
            return {}

    def decode_string(self, registers: List[int]) -> str:
        """
        Decode null-terminated ASCII string from registers.

        Args:
            registers: List of uint16 register values

        Returns:
            Decoded string with null bytes and trailing whitespace removed
        """
        bytes_data = b''
        for reg in registers:
            bytes_data += struct.pack('>H', reg)
        return bytes_data.decode('ascii', errors='ignore').rstrip('\x00 ')

    def decode_int16(self, value: int) -> Optional[int]:
        """
        Convert uint16 to signed int16.

        Args:
            value: Unsigned 16-bit value

        Returns:
            Signed value or None if not implemented
        """
        if value == self.NOT_IMPLEMENTED_INT16:
            return None
        if value >= 0x8000:
            return value - 0x10000
        return value

    def decode_uint16(self, value: int) -> Optional[int]:
        """
        Decode uint16, checking for not-implemented value.

        Args:
            value: Unsigned 16-bit value

        Returns:
            Value or None if not implemented
        """
        if value == self.NOT_IMPLEMENTED_UINT16:
            return None
        return value

    def decode_uint32(self, registers: List[int]) -> Optional[int]:
        """
        Decode 32-bit unsigned integer from two registers (big-endian).

        Args:
            registers: Two uint16 values [high_word, low_word]

        Returns:
            32-bit unsigned value or None if not implemented
        """
        if len(registers) < 2:
            return None
        value = (registers[0] << 16) | registers[1]
        if value == self.NOT_IMPLEMENTED_UINT32:
            return None
        return value

    def decode_int32(self, registers: List[int]) -> Optional[int]:
        """
        Decode 32-bit signed integer from two registers.

        Args:
            registers: Two uint16 values [high_word, low_word]

        Returns:
            32-bit signed value or None if not implemented
        """
        value = self.decode_uint32(registers)
        if value is None:
            return None
        if value == self.NOT_IMPLEMENTED_INT32:
            return None
        if value >= 0x80000000:
            return value - 0x100000000
        return value

    def decode_acc32(self, registers: List[int]) -> Optional[int]:
        """
        Decode 32-bit accumulator (always unsigned, wrap-around counter).

        Args:
            registers: Two uint16 values [high_word, low_word]

        Returns:
            32-bit unsigned accumulator value
        """
        return self.decode_uint32(registers)

    def decode_sunssf(self, value: int) -> Optional[int]:
        """
        Decode SunSpec scale factor.

        Args:
            value: Scale factor register value

        Returns:
            Scale factor as signed integer or None
        """
        return self.decode_int16(value)

    def apply_scale_factor(self, value: Any, scale_factor: int) -> Optional[float]:
        """
        Apply scale factor to a value.

        Args:
            value: Raw integer value
            scale_factor: Power of 10 to multiply by

        Returns:
            Scaled float value or None if input is None
        """
        if value is None or scale_factor is None:
            return None
        # Validate scale factor range (typically -10 to +10)
        if scale_factor < -10 or scale_factor > 10:
            return None
        try:
            return float(value) * (10 ** scale_factor)
        except (OverflowError, ValueError):
            return None

    def parse_inverter_measurements(self, registers: List[int], model_id: int = 103) -> Dict:
        """
        Parse inverter measurement registers with scale factors.

        Args:
            registers: Raw register values starting at address 40072
            model_id: SunSpec model ID (101=single, 102=split, 103=three-phase)

        Returns:
            Dictionary of parsed measurements with units
        """
        data = {}

        # Validate minimum register count
        if len(registers) < 49:
            self.log.warning(f"Inverter data incomplete: got {len(registers)} registers, expected 49")
            return data

        # Extract scale factors (relative offsets from 40072)
        sf_a = self.decode_sunssf(registers[4])      # A_SF at 40076
        sf_v = self.decode_sunssf(registers[11])     # V_SF at 40083
        sf_w = self.decode_sunssf(registers[13])     # W_SF at 40085
        sf_hz = self.decode_sunssf(registers[15])    # Hz_SF at 40087
        sf_va = self.decode_sunssf(registers[17])    # VA_SF at 40089
        sf_var = self.decode_sunssf(registers[19])   # VAr_SF at 40091
        sf_pf = self.decode_sunssf(registers[21])    # PF_SF at 40093
        sf_wh = self.decode_sunssf(registers[24])    # WH_SF at 40096
        sf_dca = self.decode_sunssf(registers[26])   # DCA_SF at 40098
        sf_dcv = self.decode_sunssf(registers[28])   # DCV_SF at 40100
        sf_dcw = self.decode_sunssf(registers[30])   # DCW_SF at 40102
        sf_tmp = self.decode_sunssf(registers[35])   # Tmp_SF at 40107

        # AC Current
        data['ac_current'] = self.apply_scale_factor(self.decode_uint16(registers[0]), sf_a)
        data['ac_current_a'] = self.apply_scale_factor(self.decode_uint16(registers[1]), sf_a)
        if model_id in [102, 103]:
            data['ac_current_b'] = self.apply_scale_factor(self.decode_uint16(registers[2]), sf_a)
        if model_id == 103:
            data['ac_current_c'] = self.apply_scale_factor(self.decode_uint16(registers[3]), sf_a)

        # AC Voltage
        data['ac_voltage_ab'] = self.apply_scale_factor(self.decode_uint16(registers[5]), sf_v)
        if model_id == 103:
            data['ac_voltage_bc'] = self.apply_scale_factor(self.decode_uint16(registers[6]), sf_v)
            data['ac_voltage_ca'] = self.apply_scale_factor(self.decode_uint16(registers[7]), sf_v)
        data['ac_voltage_an'] = self.apply_scale_factor(self.decode_uint16(registers[8]), sf_v)
        if model_id in [102, 103]:
            data['ac_voltage_bn'] = self.apply_scale_factor(self.decode_uint16(registers[9]), sf_v)
        if model_id == 103:
            data['ac_voltage_cn'] = self.apply_scale_factor(self.decode_uint16(registers[10]), sf_v)

        # AC Power
        data['ac_power'] = self.apply_scale_factor(self.decode_int16(registers[12]), sf_w)

        # AC Frequency
        data['ac_frequency'] = self.apply_scale_factor(self.decode_uint16(registers[14]), sf_hz)

        # Apparent/Reactive Power
        data['apparent_power'] = self.apply_scale_factor(self.decode_int16(registers[16]), sf_va)
        data['reactive_power'] = self.apply_scale_factor(self.decode_int16(registers[18]), sf_var)
        data['power_factor'] = self.apply_scale_factor(self.decode_int16(registers[20]), sf_pf)

        # Lifetime Energy
        data['lifetime_energy'] = self.apply_scale_factor(
            self.decode_acc32(registers[22:24]), sf_wh
        )

        # DC Side
        data['dc_current'] = self.apply_scale_factor(self.decode_uint16(registers[25]), sf_dca)
        data['dc_voltage'] = self.apply_scale_factor(self.decode_uint16(registers[27]), sf_dcv)
        data['dc_power'] = self.apply_scale_factor(self.decode_int16(registers[29]), sf_dcw)

        # Temperatures
        data['temp_cabinet'] = self.apply_scale_factor(self.decode_int16(registers[31]), sf_tmp)
        data['temp_heatsink'] = self.apply_scale_factor(self.decode_int16(registers[32]), sf_tmp)
        data['temp_transformer'] = self.apply_scale_factor(self.decode_int16(registers[33]), sf_tmp)
        data['temp_other'] = self.apply_scale_factor(self.decode_int16(registers[34]), sf_tmp)

        # Operating State
        data['status_code'] = registers[36]
        data['status_vendor'] = registers[37]

        # Event Flags
        data['evt1'] = self.decode_uint32(registers[38:40])
        data['evt2'] = self.decode_uint32(registers[40:42])
        data['evt_vnd1'] = self.decode_uint32(registers[42:44])
        data['evt_vnd2'] = self.decode_uint32(registers[44:46])
        data['evt_vnd3'] = self.decode_uint32(registers[46:48])
        data['evt_vnd4'] = self.decode_uint32(registers[48:50]) if len(registers) > 48 else 0

        return data

    def parse_mppt_measurements(self, registers: List[int]) -> Dict:
        """
        Parse MPPT (Multi-MPPT) measurement registers (Model 160).
        Model 160 starts at 40254 for Fronius inverters.

        SunSpec Model 160 structure (offsets from 40254):
        0: ID (160)
        1: L (model length)
        2: DCA_SF
        3: DCV_SF
        4: DCW_SF
        5: DCWH_SF
        6-7: Evt (global events, 32-bit)
        8: N (number of modules)
        9: TmsPer (timestamp period)
        10+: Module repeating blocks (20 registers each):
            +0: ID
            +1-8: IDStr (8 registers string)
            +9: DCA
            +10: DCV
            +11: DCW
            +12-13: DCWH (32-bit)
            +14-15: Tms (32-bit timestamp)
            +16: Tmp
            +17: DCSt
            +18-19: DCEvt (32-bit)

        Args:
            registers: Raw register values starting at address 40254

        Returns:
            Dictionary of parsed MPPT measurements
        """
        data = {}

        if len(registers) < 10:
            return data

        model_id = registers[0]
        if model_id != 160:
            # Not an MPPT model, may be end marker or different model
            self.log.debug(f"MPPT: Expected model 160, got {model_id}")
            return data

        model_length = registers[1]
        self.log.debug(f"MPPT: Model 160 found, length={model_length}")

        # Extract scale factors (offsets 2-5)
        sf_dca = self.decode_sunssf(registers[2])
        sf_dcv = self.decode_sunssf(registers[3])
        sf_dcw = self.decode_sunssf(registers[4])
        sf_dcwh = self.decode_sunssf(registers[5])

        self.log.debug(f"MPPT scale factors: DCA={sf_dca}, DCV={sf_dcv}, DCW={sf_dcw}, DCWH={sf_dcwh}")

        # Global MPPT data
        data['dc_events'] = self.decode_uint32(registers[6:8])
        data['num_modules'] = self.decode_uint16(registers[8])
        data['timestamp_period'] = self.decode_uint16(registers[9])

        # Per-module data starts at offset 10
        # Each module has 20 registers
        module_offset = 10
        module_size = 20
        modules = []

        num_modules = data.get('num_modules', 0) or 0
        self.log.debug(f"MPPT: num_modules={num_modules}")

        for i in range(min(num_modules, 4)):  # Max 4 MPPT strings typically
            mod_start = module_offset + (i * module_size)
            if mod_start + module_size > len(registers):
                self.log.debug(f"MPPT: Not enough registers for module {i+1}, need {mod_start + module_size}, have {len(registers)}")
                break

            # Module structure:
            # +0: ID
            # +1-8: IDStr (string, 8 registers)
            # +9: DCA
            # +10: DCV
            # +11: DCW
            # +12-13: DCWH
            # +14-15: Tms
            # +16: Tmp
            # +17: DCSt
            # +18-19: DCEvt
            module = {
                'id': registers[mod_start],
                'dc_current': self.apply_scale_factor(
                    self.decode_uint16(registers[mod_start + 9]), sf_dca
                ),
                'dc_voltage': self.apply_scale_factor(
                    self.decode_uint16(registers[mod_start + 10]), sf_dcv
                ),
                'dc_power': self.apply_scale_factor(
                    self.decode_uint16(registers[mod_start + 11]), sf_dcw
                ),
                'dc_energy': self.apply_scale_factor(
                    self.decode_acc32(registers[mod_start + 12:mod_start + 14]), sf_dcwh
                ),
                'operating_state': self.decode_uint16(registers[mod_start + 17])
            }
            modules.append(module)
            self.log.debug(f"MPPT Module {i+1}: V={module['dc_voltage']}, I={module['dc_current']}, P={module['dc_power']}")

        data['modules'] = modules

        return data

    def parse_meter_measurements(self, registers: List[int]) -> Dict:
        """
        Parse meter measurement registers (int + scale factor format).

        Args:
            registers: Raw register values starting at address 40072

        Returns:
            Dictionary of parsed measurements with units
        """
        data = {}

        # Validate minimum register count
        if len(registers) < 53:
            self.log.warning(f"Meter data incomplete: got {len(registers)} registers, expected 53")
            return data

        # Extract scale factors
        sf_a = self.decode_sunssf(registers[4])      # A_SF at 40076
        sf_v = self.decode_sunssf(registers[13])     # V_SF at 40085
        sf_hz = self.decode_sunssf(registers[15])    # Hz_SF at 40087
        sf_w = self.decode_sunssf(registers[20])     # W_SF at 40092
        sf_va = self.decode_sunssf(registers[25])    # VA_SF at 40097
        sf_var = self.decode_sunssf(registers[30])   # VAR_SF at 40102
        sf_pf = self.decode_sunssf(registers[35])    # PF_SF at 40107
        sf_wh = self.decode_sunssf(registers[52])    # TotWh_SF at 40124

        # Currents
        data['current_total'] = self.apply_scale_factor(self.decode_int16(registers[0]), sf_a)
        data['current_a'] = self.apply_scale_factor(self.decode_int16(registers[1]), sf_a)
        data['current_b'] = self.apply_scale_factor(self.decode_int16(registers[2]), sf_a)
        data['current_c'] = self.apply_scale_factor(self.decode_int16(registers[3]), sf_a)

        # Voltages LN
        data['voltage_ln_avg'] = self.apply_scale_factor(self.decode_int16(registers[5]), sf_v)
        data['voltage_an'] = self.apply_scale_factor(self.decode_int16(registers[6]), sf_v)
        data['voltage_bn'] = self.apply_scale_factor(self.decode_int16(registers[7]), sf_v)
        data['voltage_cn'] = self.apply_scale_factor(self.decode_int16(registers[8]), sf_v)

        # Voltages LL
        data['voltage_ll_avg'] = self.apply_scale_factor(self.decode_int16(registers[9]), sf_v)
        data['voltage_ab'] = self.apply_scale_factor(self.decode_int16(registers[10]), sf_v)
        data['voltage_bc'] = self.apply_scale_factor(self.decode_int16(registers[11]), sf_v)
        data['voltage_ca'] = self.apply_scale_factor(self.decode_int16(registers[12]), sf_v)

        # Frequency
        data['frequency'] = self.apply_scale_factor(self.decode_int16(registers[14]), sf_hz)

        # Power
        data['power_total'] = self.apply_scale_factor(self.decode_int16(registers[16]), sf_w)
        data['power_a'] = self.apply_scale_factor(self.decode_int16(registers[17]), sf_w)
        data['power_b'] = self.apply_scale_factor(self.decode_int16(registers[18]), sf_w)
        data['power_c'] = self.apply_scale_factor(self.decode_int16(registers[19]), sf_w)

        # Apparent Power
        data['va_total'] = self.apply_scale_factor(self.decode_int16(registers[21]), sf_va)
        data['va_a'] = self.apply_scale_factor(self.decode_int16(registers[22]), sf_va)
        data['va_b'] = self.apply_scale_factor(self.decode_int16(registers[23]), sf_va)
        data['va_c'] = self.apply_scale_factor(self.decode_int16(registers[24]), sf_va)

        # Reactive Power
        data['var_total'] = self.apply_scale_factor(self.decode_int16(registers[26]), sf_var)
        data['var_a'] = self.apply_scale_factor(self.decode_int16(registers[27]), sf_var)
        data['var_b'] = self.apply_scale_factor(self.decode_int16(registers[28]), sf_var)
        data['var_c'] = self.apply_scale_factor(self.decode_int16(registers[29]), sf_var)

        # Power Factor
        data['pf_avg'] = self.apply_scale_factor(self.decode_int16(registers[31]), sf_pf)
        data['pf_a'] = self.apply_scale_factor(self.decode_int16(registers[32]), sf_pf)
        data['pf_b'] = self.apply_scale_factor(self.decode_int16(registers[33]), sf_pf)
        data['pf_c'] = self.apply_scale_factor(self.decode_int16(registers[34]), sf_pf)

        # Energy (exported = to grid, imported = from grid)
        data['energy_exported'] = self.apply_scale_factor(
            self.decode_acc32(registers[36:38]), sf_wh
        )
        data['energy_exported_a'] = self.apply_scale_factor(
            self.decode_acc32(registers[38:40]), sf_wh
        )
        data['energy_exported_b'] = self.apply_scale_factor(
            self.decode_acc32(registers[40:42]), sf_wh
        )
        data['energy_exported_c'] = self.apply_scale_factor(
            self.decode_acc32(registers[42:44]), sf_wh
        )

        data['energy_imported'] = self.apply_scale_factor(
            self.decode_acc32(registers[44:46]), sf_wh
        )
        data['energy_imported_a'] = self.apply_scale_factor(
            self.decode_acc32(registers[46:48]), sf_wh
        )
        data['energy_imported_b'] = self.apply_scale_factor(
            self.decode_acc32(registers[48:50]), sf_wh
        )
        data['energy_imported_c'] = self.apply_scale_factor(
            self.decode_acc32(registers[50:52]), sf_wh
        )

        return data

    def decode_state_codes(self, codes_str: str) -> List[Dict]:
        """
        Decode comma-separated state codes to their descriptions.

        Args:
            codes_str: Comma-separated state codes (e.g., "307,522,523")

        Returns:
            List of dicts with code and description
        """
        if not codes_str:
            return []

        decoded = []
        for code in codes_str.split(','):
            code = code.strip()
            if code:
                description = self.state_codes.get(code, f"Unknown code {code}")
                decoded.append({
                    'code': int(code) if code.isdigit() else code,
                    'description': description
                })
        return decoded

    def parse_event_flags(self, evt_vnd1: int, evt_vnd2: int, evt_vnd3: int,
                          evt_vnd4: int = 0, inverter_type: str = 'all') -> List[Dict]:
        """
        Parse vendor event flags into human-readable format.

        Args:
            evt_vnd1-4: Event flag register values
            inverter_type: Model type for event lookup (symo, primo, galvo, igplus, all)

        Returns:
            List of active event dictionaries with class, codes, and decoded descriptions
        """
        events = []

        # Get event flag definitions
        devices = self.event_flags.get('devices', [])
        evt_def = None

        # Find matching device definition
        for device in devices:
            if inverter_type in device:
                evt_def = device[inverter_type]
                break
            elif 'all' in device:
                evt_def = device['all']

        if not evt_def:
            return events

        # Parse each EvtVnd register
        for evt_name, evt_value in [('EvtVnd1', evt_vnd1), ('EvtVnd2', evt_vnd2),
                                     ('EvtVnd3', evt_vnd3), ('EvtVnd4', evt_vnd4)]:
            if evt_value is None or evt_value == 0:
                continue

            flags = evt_def.get(evt_name, [])
            for flag in flags:
                if evt_value & flag['dec']:
                    codes_str = flag.get('codes', '')
                    events.append({
                        'register': evt_name,
                        'bit_value': flag['dec'],
                        'codes': codes_str,
                        'codes_decoded': self.decode_state_codes(codes_str),
                        'class': flag.get('class', 'Unknown'),
                        'hex': flag.get('hex', 0)
                    })

        return events

    def parse_status(self, status_value: int) -> Dict:
        """
        Parse operating status code to human-readable format.

        Args:
            status_value: Raw status register value

        Returns:
            Dictionary with code, name, description, and alarm flag
        """
        st_codes = self.status_codes.get('St', {})
        status_str = str(status_value)

        if status_str in st_codes:
            info = st_codes[status_str]
            return {
                'code': status_value,
                'name': info['name'],
                'description': info['description'],
                'alarm': info.get('alarm', False)
            }

        return {
            'code': status_value,
            'name': 'UNKNOWN',
            'description': f'Unknown status code: {status_value}',
            'alarm': True
        }

    def detect_inverter_type(self, model_name: str) -> str:
        """
        Detect inverter type from model name string.

        Args:
            model_name: Model string from device (e.g., "Fronius Symo 10.0-3-M")

        Returns:
            Inverter type: 'symo', 'primo', 'galvo', 'igplus', or 'all'
        """
        model_lower = model_name.lower()

        if 'symo' in model_lower:
            return 'symo'
        elif 'primo' in model_lower:
            return 'primo'
        elif 'galvo' in model_lower:
            return 'galvo'
        elif 'ig plus' in model_lower or 'igplus' in model_lower:
            return 'igplus'

        return 'all'

    def parse_storage_measurements(self, registers: list) -> dict:
        """
        Parse storage (battery) control registers (Model 124).
        Registers start at 40343 for Int+SF format (after header).

        Model 124 - Basic Storage Controls (24 registers):
        Offset 0: WChaMax - Maximum charge power (W)
        Offset 1: WChaGra - Charge ramp rate (% WChaMax/sec)
        Offset 2: WDisChaGra - Discharge ramp rate (% WChaMax/sec)
        Offset 3: StorCtl_Mod - Storage control mode (bitfield16)
        Offset 4: VAChaMax - Maximum charging VA
        Offset 5: MinRsvPct - Minimum reserve percentage
        Offset 6: ChaState - Charge state (% of capacity)
        Offset 7: StorAval - Available storage (AH)
        Offset 8: InBatV - Internal battery voltage (V)
        Offset 9: ChaSt - Charge status (enum)
        Offset 10: OutWRte - Discharge rate (% WDisChaMax)
        Offset 11: InWRte - Charge rate (% WChaMax)
        Offset 12: InOutWRte_WinTms - Time window (sec)
        Offset 13: InOutWRte_RvrtTms - Revert timeout (sec)
        Offset 14: InOutWRte_RmpTms - Ramp time (sec)
        Offset 15: ChaGriSet - Charging grid setting (enum)
        Offset 16: WChaMax_SF - Scale factor
        Offset 17: WChaDisChaGra_SF - Scale factor
        Offset 18: VAChaMax_SF - Scale factor
        Offset 19: MinRsvPct_SF - Scale factor
        Offset 20: ChaState_SF - Scale factor
        Offset 21: StorAval_SF - Scale factor
        Offset 22: InBatV_SF - Scale factor
        Offset 23: InOutWRte_SF - Scale factor

        Args:
            registers: Raw register values (24 registers)

        Returns:
            Dictionary of parsed storage measurements
        """
        data = {}

        if len(registers) < 24:
            self.log.warning(f"Storage data incomplete: got {len(registers)} registers, expected 24")
            return data

        # Extract scale factors (offsets 16-23)
        sf_wcha_max = self.decode_sunssf(registers[16])
        sf_wcha_gra = self.decode_sunssf(registers[17])
        sf_vacha_max = self.decode_sunssf(registers[18])
        sf_min_rsv = self.decode_sunssf(registers[19])
        sf_cha_state = self.decode_sunssf(registers[20])
        sf_stor_aval = self.decode_sunssf(registers[21])
        sf_in_bat_v = self.decode_sunssf(registers[22])
        sf_in_out_w_rte = self.decode_sunssf(registers[23])

        # Control/Setpoint registers (writable)
        data['max_charge_power'] = self.apply_scale_factor(
            self.decode_uint16(registers[0]), sf_wcha_max
        )
        data['charge_ramp_rate'] = self.apply_scale_factor(
            self.decode_uint16(registers[1]), sf_wcha_gra
        )
        data['discharge_ramp_rate'] = self.apply_scale_factor(
            self.decode_uint16(registers[2]), sf_wcha_gra
        )

        # Storage control mode (bitfield)
        stor_ctl_mod = self.decode_uint16(registers[3])
        data['storage_control_mode'] = stor_ctl_mod
        data['charge_limit_active'] = bool(stor_ctl_mod & 0x01) if stor_ctl_mod is not None else None
        data['discharge_limit_active'] = bool(stor_ctl_mod & 0x02) if stor_ctl_mod is not None else None

        data['max_charge_va'] = self.apply_scale_factor(
            self.decode_uint16(registers[4]), sf_vacha_max
        )
        data['min_reserve_pct'] = self.apply_scale_factor(
            self.decode_uint16(registers[5]), sf_min_rsv
        )

        # Status registers (read-only)
        data['charge_state_pct'] = self.apply_scale_factor(
            self.decode_uint16(registers[6]), sf_cha_state
        )
        data['available_storage_ah'] = self.apply_scale_factor(
            self.decode_uint16(registers[7]), sf_stor_aval
        )
        data['battery_voltage'] = self.apply_scale_factor(
            self.decode_uint16(registers[8]), sf_in_bat_v
        )

        # Charge status enumeration
        cha_st = self.decode_uint16(registers[9])
        data['charge_status_code'] = cha_st
        data['charge_status'] = self._decode_charge_status(cha_st)

        # Rate setpoints
        data['discharge_rate_pct'] = self.apply_scale_factor(
            self.decode_int16(registers[10]), sf_in_out_w_rte
        )
        data['charge_rate_pct'] = self.apply_scale_factor(
            self.decode_int16(registers[11]), sf_in_out_w_rte
        )

        # Timing parameters
        data['rate_window_secs'] = self.decode_uint16(registers[12])
        data['rate_revert_secs'] = self.decode_uint16(registers[13])
        data['rate_ramp_secs'] = self.decode_uint16(registers[14])

        # Grid charging setting
        cha_gri_set = self.decode_uint16(registers[15])
        data['grid_charging_code'] = cha_gri_set
        data['grid_charging'] = 'GRID' if cha_gri_set == 1 else 'PV' if cha_gri_set == 0 else 'UNKNOWN'

        return data

    def _decode_charge_status(self, status_code: int) -> dict:
        """
        Decode storage charge status enumeration (ChaSt).

        Args:
            status_code: Raw status code value

        Returns:
            Dictionary with status name and description
        """
        status_map = {
            1: {'name': 'OFF', 'description': 'Storage is off'},
            2: {'name': 'EMPTY', 'description': 'Storage is empty'},
            3: {'name': 'DISCHARGING', 'description': 'Storage is discharging'},
            4: {'name': 'CHARGING', 'description': 'Storage is charging'},
            5: {'name': 'FULL', 'description': 'Storage is full'},
            6: {'name': 'HOLDING', 'description': 'Storage is holding charge'},
            7: {'name': 'TESTING', 'description': 'Storage is in test mode'},
        }

        if status_code is None:
            return {'name': 'UNKNOWN', 'description': 'Status not available'}

        return status_map.get(status_code, {
            'name': 'UNKNOWN',
            'description': f'Unknown status code: {status_code}'
        })
