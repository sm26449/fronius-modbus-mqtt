# Fronius Modbus reference

Vendor documentation for the Fronius SunSpec Modbus interface, kept here so the
collector's register/state/event decoding can be traced back to an authoritative
source. These files are the basis for `config/registers.json` and
`config/FroniusEventFlags.json`.

> Provenance: collected from `sm26449/diysolar-toolkit` (`docs/fronius/modbus/`),
> originally derived from Fronius' official *Operating Instructions – Modbus TCP &
> RTU* and *State Codes & Event Flags* documents.

## Files

| File | What it is | Used by |
|------|------------|---------|
| `Fronius_Modbus_TCP_RTU.pdf` | Official Fronius register map (SunSpec models 1/101/103/120s/160 + Fronius-specific blocks, immediate controls, storage). The master reference. | `config/registers.json`, `register_parser.py` |
| `Fronius_State_Codes_and_Event_Flags_1.0.xlsx` | Master spreadsheet of all state codes + event-flag bit maps across inverter families. | source for the CSV/JSON below |
| `maps_registry.xlsx` | Cross-model register map registry. | reference |
| `St.csv` | SunSpec **operating state** `St` (reg 40108): codes 1–8. | `registers.json → status_codes.St`, `parse_status()` |
| `StVnd.csv` | Fronius **vendor operating state** `StVnd` (reg 40109): codes 1–13 (1–8 mirror `St`; **9–13 are Fronius extensions**: NoSolarNet, NoCommInv, SN-Overcurrent, Bootload, **AFCI**). | `registers.json → status_codes.StVnd`, `parse_vendor_status()` |
| `Symo_State_Codes.csv` | Numeric **STATE codes** shown on the display / Solar.web (102 = AC voltage too high, 240 = Arc Detected, 447/475/502 = isolation, …). | `registers.json → state_codes`, `decode_state_codes()` |
| `EvtVnd1-4_Symo.csv` | **Authoritative** Symo event-flag bit map: each `EvtVnd1..4` bit (regs 40116–40121) → the STATE codes + error class it represents. More complete than `symo.json`. | `config/FroniusEventFlags.json → devices[].symo`, `parse_event_flags()` |
| `symo.json` | Symo event-flag bit map in JSON (the numeric-code subset of the CSV — **incomplete**, kept for reference; the CSV is the source of truth). | reference |
| `Meter_Register_Map_Float_v1.0.xlsx` | Smart-meter register map (float encoding). | meter decoding (future) |
| `Meter_Register_Map_Int_SF_v1.0.xlsx` | Smart-meter register map (int + scale-factor encoding). | meter decoding (future) |

## How the collector consumes this

```
SunSpec Modbus (model 103, base 40072)
  ├─ St    (40108)  → status_codes.St    → parse_status()         → operating state 1–8
  ├─ StVnd (40109)  → status_codes.StVnd → parse_vendor_status()  → vendor state 1–13
  └─ EvtVnd1..4 (40116–40121, uint32 bitfields)
                    → FroniusEventFlags.json[<variant>]           → parse_event_flags()
                       each set bit → STATE codes → decode_state_codes() (state_codes catalog)
```

The event-flag **variant** is chosen per inverter by `RegisterParser.model_to_variant(model)`
(`"Symo …"` → `symo`, else `primo`/`galvo`/`igplus`/`all`). The Symo map carries the
DC-insulation and AFCI bits, so using the right variant matters.

### Worked examples

- **`StVnd = 10`** → `I_STATUS_NO_COMM_INV` "No communication with inverter" (the
  DataManager lost contact with the inverter power stage).
- **`StVnd = 13`** → `I_STATUS_AFCI` "AFCI Event".
- **`EvtVnd1` bit `0x1`** → class *DC Insulation fault* → STATE codes `447, 475, 502`
  → "Isolation Error / Isolation Too Low Error / Warning – Isolation Too Low".
- **`EvtVnd2` bit `0x1000`** → class *Arc Detected* → STATE code `240` "Arc Detected".

## Notes / gotchas

- **Temperatures are NOT available over SunSpec on the Symo Advanced 20.0-3-M.** The
  model-103 temperature registers `Tmp_Cab/Snk/Trns/Oth` (40103–40106) and `Tmp_SF`
  all read `0x8000` (NOT_IMPLEMENTED). Per-inverter temperature requires a Fronius
  proprietary register or the Solar API — not in this SunSpec map.
- **`StVnd` is an operating STATE (1–13), not a fault code.** Do not look it up in the
  numeric STATE-code catalog; the fault codes live in the `EvtVnd*` bitfields.
- `config/FroniusEventFlags.json`'s `symo` variant is rebuilt from `EvtVnd1-4_Symo.csv`
  (the complete 32/32/4-bit map), not from `symo.json`.
