# LYWSD02MMC BLE Toolkit

Utilities for a Xiaomi/MiaoMiaoCe `LYWSD02MMC` e-ink temperature/humidity clock. The project includes a Python CLI for scripting/export and a Web Bluetooth UI for browser-based control, without requiring the Mi Home app for normal settings.

## What It Can Do

- Read and set clock time, timezone, 12/24-hour display mode, and C/F units.
- Read and update comfort thresholds.
- Stream current temperature/humidity readings.
- Export stored hourly history to CSV.
- Decode encrypted Xiaomi MiBeacon advertisements when you have the device bind key.
- Inspect the known Mijia BLE service and GATT layout.

## Quick Start

Open the browser UI in Chrome or Edge:

```text
https://headdirt.github.io/lywsd/
```

Or use the Python CLI:

Run through `uv`; dependencies are declared in the script header.

```sh
uv run lywsd.py scan
uv run lywsd.py info
uv run lywsd.py dump-mijia
uv run lywsd.py probe
```

Set normal display values:

```sh
uv run lywsd.py set-time
uv run lywsd.py set-time --tz -4
uv run lywsd.py set-time-format 12
uv run lywsd.py set-time-format 24
uv run lywsd.py set-unit C
uv run lywsd.py set-unit F
```

`set-time` measures the Bluetooth round trip and times the write to land on a whole-second boundary (the device only stores whole seconds), then reads the clock back and reports the remaining drift. `info` reports drift to a fraction of a second the same way. The device's own clock drifts over time, so re-run `set-time` now and then; it uses the host clock, so keep that synced (NTP).

Read or update comfort thresholds:

```sh
uv run lywsd.py comfort
uv run lywsd.py comfort --temp-min 19 --temp-max 27 --hum-min 20 --hum-max 85
```

Comfort threshold temperatures are always specified in Celsius, even if the display unit is set to Fahrenheit.

Stream current sensor readings:

```sh
uv run lywsd.py watch
uv run lywsd.py watch --seconds 30
```

Read stored hourly history:

```sh
uv run lywsd.py history --from-index 3803 --count 10
uv run lywsd.py history --from-index 4059 --count 256 --seconds 20 --filter-range --csv history-filtered.csv
```

`--filter-range` skips stale records below the requested start index, which can be useful when reading large history ranges.

Decode advertisements:

```sh
uv run lywsd.py adv --seconds 60
uv run lywsd.py adv --seconds 60 --mac A4:C1:38:F1:50:9D --bindkey 00112233445566778899aabbccddeeff
uv run lywsd.py passive --seconds 60 --bindkey 00112233445566778899aabbccddeeff
uv run lywsd.py decode-adv 585842256d9d50f138c1a4100129050b0000f06da497 --mac A4:C1:38:F1:50:9D --bindkey 00112233445566778899aabbccddeeff
```

## Browser UI

Open `index.html` in Chrome or Edge on a machine with Web Bluetooth support, or visit https://headdirt.github.io/lywsd/.

The UI can:

- Connect to `LYWSD02...` devices.
- Set/read time.
- Set/read 12h or 24h display mode.
- Set/read C/F units.
- Read battery.
- Stream live temperature/humidity.
- Read/apply comfort thresholds.
- Read hourly history by index into CSV text.

On connect, it attempts to populate time format, unit, battery, comfort range, and current history start index.

## Confirmed Payloads

Mijia service:

```text
ebe0ccb0-7a0a-4b0c-8a1a-6ff2997da3a6
```

| UUID suffix | Name | Access | Payload |
| --- | --- | --- | --- |
| `ccb7` | Time | read/write | `<uint32 unix_time><int8 tz><uint8 reserved><uint8 time_format>` |
| `ccb9` | Data Count | read | `<uint32 index_a><uint32 index_b>` |
| `ccba` | Index | read/write | `<uint32 start_index>` |
| `ccbb` | Data Read | read | history record |
| `ccbc` | Data Notify | notify | history record |
| `ccbe` | Temperature Unit | read/write | `0x00` C, `0x01` F |
| `ccc1` | Temperature and Humidity | read/notify | `<int16 temp_c_x100><uint8 rh>[<uint16 vbat_mv>]` |
| `ccc4` | Batt | read | one byte percent, often stuck near `100` |
| `ccd7` | comfortable temp and humi | read/write | comfort range |

Time format:

```text
time_format = 0x00  # 24-hour
time_format = 0xaa  # 12-hour
```

Comfort range:

```text
<int16 temp_max_c_x100><int16 temp_min_c_x100><uint8 hum_max><uint8 hum_min>
```

Comfort temperature thresholds are stored in Celsius.

History record:

```text
<uint32 index><uint32 unix_time><int16 temp_a_x10><uint8 rh_a><int16 temp_b_x10><uint8 rh_b>
```

Live data:

```text
<int16 temp_c_x100><uint8 rh><uint16 vbat_mv>
```

## Other Mapped Characteristics

| UUID suffix | Descriptor / label | Notes |
| --- | --- | --- |
| `ccc2` | Negotiate Notification Size | write-only, not characterized |
| `ccc8` | disconnect | write-only |
| `ccd4` | reset data | write-only, avoid unless intentionally erasing |
| `ccd8` | set conn interval | write-only, not characterized |
| `cca1` | debug fakedata | write-only, avoid |
| `cca2` | debug event cnt | read, not characterized for normal use |
| `cca3` | debug power intervel | read/write, not characterized for normal use |
| `cca4` | debug log enable | read/write, avoid |
| `cca5` | debug write fake time | read/write, debug payload |

The Python CLI intentionally does not expose write commands for the uncharacterized controls above.

## MiBeacon

Advertisements use Xiaomi MiBeacon service data under:

```text
0000fe95-0000-1000-8000-00805f9b34fb
```

Confirmed:

- Product id: `0x2542`.
- MiBeacon version: `5`.
- Object payloads are encrypted.
- Clear-MAC frames and Xiaomi manufacturer data can reveal the public Xiaomi BLE MAC.
- Decryption needs the correct 16-byte Mi bind key.
- LYWSD02MMC/MiaoMiaoce decrypted object ids include:
  - `0x4c01`: temperature float.
  - `0x4c02`: humidity byte.

The `passive` command decodes advertisements and prints a latest-value summary. It requires a bind key; the public Xiaomi MAC is learned from clear FE95/manufacturer frames when possible, or can be supplied with `--mac`:

```sh
uv run lywsd.py passive --seconds 60 --bindkey <32-hex-bindkey>
```

Bind keys can be recovered through Mi Home/Xiaomi Cloud tooling, packet captures during pairing, or Mi Activation tools such as pvvx TelinkMiFlasher.

### Mi Activation Status

Use pvvx TelinkMiFlasher for browser-based Mi Activation and bind-key generation:

```text
https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html
```

`lywsd.py mi-activate --yes` follows the same registration path and can generate a fresh bind key from the CLI. Activation changes the device bind state, so only run it when you intend to replace the current key.

## Risk Notes

Safe, already used:

- Reading services and characteristics.
- Setting time/time format/unit.
- Reading and writing comfort thresholds.
- Reading history.

Use caution:

- MiBeacon decryption with unknown keys.

Avoid casually:

- `reset data`.
- `debug fakedata`.
- `debug log enable`.
- `debug write fake time`.
- `FE95` auth/spec writes unless you intend to rebind the device.
