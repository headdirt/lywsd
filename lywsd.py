#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22", "cryptography>=42"]
# ///
"""
lywsd.py — talk to a Xiaomi Mijia LYWSD02MMC (e-ink temperature/humidity clock)
over BLE without the Mi Home app.

Usage:
    uv run lywsd.py scan
    uv run lywsd.py info       [--address AA:BB:..]  [--name LYWSD02]
    uv run lywsd.py adv        [--name LYWSD02] [--seconds N] [--mac A4:C1:38:..] [--bindkey HEX]
    uv run lywsd.py passive    --bindkey HEX [--name LYWSD02] [--seconds N]
    uv run lywsd.py decode-adv HEX [--mac A4:C1:38:..] [--bindkey HEX]
    uv run lywsd.py dump-mijia [--address AA:BB:..]  [--name LYWSD02]
    uv run lywsd.py history    [--address AA:BB:..]  [--name LYWSD02] [--from-index N] [--count N]
    uv run lywsd.py set-time   [--address AA:BB:..]  [--tz N]
    uv run lywsd.py set-time-format {12,24}  [--address AA:BB:..]
    uv run lywsd.py set-unit   {C,F}  [--address AA:BB:..]
    uv run lywsd.py comfort    [--address AA:BB:..]  [--temp-min C] [--temp-max C] [--hum-min N] [--hum-max N]
    uv run lywsd.py mi-activate --yes  [--address AA:BB:..]
    uv run lywsd.py watch      [--address AA:BB:..]  [--seconds N]

If --address is omitted, the script scans and picks the first device whose
name starts with --name (default: LYWSD02).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import os
import struct
import sys
import time
from datetime import datetime, timezone

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = BleakScanner = None

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:
    AESCCM = None
    InvalidTag = Exception
    ec = hashes = HKDF = serialization = None

SERVICE = "ebe0ccb0-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_TIME = "ebe0ccb7-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_DATA_COUNT = "ebe0ccb9-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_INDEX = "ebe0ccba-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_DATA_READ = "ebe0ccbb-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_DATA_NOTIFY = "ebe0ccbc-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_UNIT = "ebe0ccbe-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_BATT = "ebe0ccc4-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_DATA = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"
CH_COMFORT = "ebe0ccd7-7a0a-4b0c-8a1a-6ff2997da3a6"

UNIT_C = 0x00
UNIT_F = 0x01
TIME_FORMAT_24 = 0x00
TIME_FORMAT_12 = 0xAA
TIME_FORMAT_LABELS = {TIME_FORMAT_24: "24h", TIME_FORMAT_12: "12h"}
MIJIA_DESCRIPTOR = "00002901-0000-1000-8000-00805f9b34fb"

DEFAULT_NAME_PREFIX = "LYWSD02"
MIJIA_SERVICE = "0000fe95-0000-1000-8000-00805f9b34fb"


class CliError(Exception):
    """User-facing command error."""


def require_bleak() -> None:
    if BleakClient is None or BleakScanner is None:
        raise CliError("this command needs `bleak`; run via `uv run lywsd.py ...` or install `bleak`")


CH_FE95_AUTH = "00000010-0000-1000-8000-00805f9b34fb"
CH_FE95_STANDARD = "00000019-0000-1000-8000-00805f9b34fb"

DEVICE_INFO_CHARS = {
    "00002a23-0000-1000-8000-00805f9b34fb": ("System ID", "hex"),
    "00002a24-0000-1000-8000-00805f9b34fb": ("Model Number", "str"),
    "00002a25-0000-1000-8000-00805f9b34fb": ("Serial Number", "str"),
    "00002a26-0000-1000-8000-00805f9b34fb": ("Firmware Revision", "str"),
    "00002a27-0000-1000-8000-00805f9b34fb": ("Hardware Revision", "str"),
    "00002a28-0000-1000-8000-00805f9b34fb": ("Software Revision", "str"),
    "00002a29-0000-1000-8000-00805f9b34fb": ("Manufacturer", "str"),
}

MIJIA_CHARS = {
    "ebe0ccb7-7a0a-4b0c-8a1a-6ff2997da3a6": "Time",
    "ebe0ccb9-7a0a-4b0c-8a1a-6ff2997da3a6": "Data Count",
    "ebe0ccba-7a0a-4b0c-8a1a-6ff2997da3a6": "Index",
    "ebe0ccbb-7a0a-4b0c-8a1a-6ff2997da3a6": "Data Read",
    "ebe0ccbc-7a0a-4b0c-8a1a-6ff2997da3a6": "Data Notify",
    "ebe0ccbe-7a0a-4b0c-8a1a-6ff2997da3a6": "Temperature Unit",
    "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6": "Temperature and Humidity",
    "ebe0ccc2-7a0a-4b0c-8a1a-6ff2997da3a6": "Negotiate Notification Size",
    "ebe0ccc3-7a0a-4b0c-8a1a-6ff2997da3a6": "Unknown limits",
    "ebe0ccc4-7a0a-4b0c-8a1a-6ff2997da3a6": "Batt",
    "ebe0ccc8-7a0a-4b0c-8a1a-6ff2997da3a6": "disconnect",
    "ebe0ccd1-7a0a-4b0c-8a1a-6ff2997da3a6": "clear data",
    "ebe0ccd2-7a0a-4b0c-8a1a-6ff2997da3a6": "Unknown flag",
    "ebe0ccd3-7a0a-4b0c-8a1a-6ff2997da3a6": "Unknown flag",
    "ebe0ccd4-7a0a-4b0c-8a1a-6ff2997da3a6": "reset data",
    "ebe0ccd7-7a0a-4b0c-8a1a-6ff2997da3a6": "comfortable temp and humi",
    "ebe0ccd8-7a0a-4b0c-8a1a-6ff2997da3a6": "set conn interval",
    "ebe0ccd9-7a0a-4b0c-8a1a-6ff2997da3a6": "para_value_get",
    "ebe0cca1-7a0a-4b0c-8a1a-6ff2997da3a6": "debug fakedata",
    "ebe0cca2-7a0a-4b0c-8a1a-6ff2997da3a6": "debug event cnt",
    "ebe0cca3-7a0a-4b0c-8a1a-6ff2997da3a6": "debug power intervel",
    "ebe0cca4-7a0a-4b0c-8a1a-6ff2997da3a6": "debug log enable",
    "ebe0cca5-7a0a-4b0c-8a1a-6ff2997da3a6": "debug write fake time",
}

TIME_STRUCT = struct.Struct("<Ib")
SENSOR_STRUCT = struct.Struct("<hB")
HISTORY_STRUCT = struct.Struct("<IIhBhB")


def parse_time_payload(data: bytes) -> tuple[int, int, int, int]:
    if len(data) < 4:
        raise ValueError(f"time payload too short: {fmt_bytes(data)}")
    ts = struct.unpack_from("<I", data)[0]
    tz = struct.unpack_from("b", data, 4)[0] if len(data) >= 5 else 0
    reserved = data[5] if len(data) >= 6 else 0
    fmt = data[6] if len(data) >= 7 else TIME_FORMAT_24
    return ts, tz, reserved, fmt


def pack_time_payload(ts: int, tz: int, reserved: int = 0, fmt: int = TIME_FORMAT_24) -> bytes:
    return TIME_STRUCT.pack(ts, tz) + bytes([reserved, fmt])


@contextlib.asynccontextmanager
async def notify_session(client: BleakClient, uuid: str, callback):
    await client.start_notify(uuid, callback)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await client.stop_notify(uuid)


def parse_sensor(data: bytes) -> tuple[float, int]:
    temp, hum = SENSOR_STRUCT.unpack_from(data)
    return temp / 100, hum


def parse_history_record(data: bytes) -> tuple[int, datetime, float, int, float, int]:
    idx, ts, temp_a, hum_a, temp_b, hum_b = HISTORY_STRUCT.unpack_from(data)
    return idx, datetime.fromtimestamp(ts, tz=timezone.utc), temp_a / 10, hum_a, temp_b / 10, hum_b


def parse_comfort(data: bytes) -> tuple[float, float, int, int]:
    if len(data) < 6:
        raise ValueError(f"comfort payload too short: {fmt_bytes(data)}")
    temp_a, temp_b, hum_a, hum_b = struct.unpack_from("<hhBB", data)
    temp_min, temp_max = sorted((temp_a / 100, temp_b / 100))
    hum_min, hum_max = sorted((hum_a, hum_b))
    return temp_min, temp_max, hum_min, hum_max


def pack_comfort(temp_min: float, temp_max: float, hum_min: int, hum_max: int) -> bytes:
    if not -30 <= temp_min <= temp_max <= 60:
        raise ValueError("temperature comfort range must be within -30..60 C and min <= max")
    if not 0 <= hum_min <= hum_max <= 100:
        raise ValueError("humidity comfort range must be within 0..100% and min <= max")
    # Firmware stores the high endpoint first on this unit.
    return struct.pack("<hhBB", round(temp_max * 100), round(temp_min * 100), hum_max, hum_min)


def fmt_bytes(data: bytes) -> str:
    return data.hex(" ") if data else "(empty)"


def strip_nul(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").rstrip("\x00")


def parse_hex_bytes(value: str, expected_len: int | None = None) -> bytes:
    raw = value.replace(":", "").replace("-", "").replace(" ", "")
    try:
        data = bytes.fromhex(raw)
    except ValueError as e:
        raise CliError(f"invalid hex: {value}") from e
    if expected_len is not None and len(data) != expected_len:
        raise CliError(f"expected {expected_len} bytes, got {len(data)}")
    return data


def parse_xiaomi_objects(payload: bytes) -> tuple[list[tuple[int, bytes, str | None]], list[str]]:
    objects: list[tuple[int, bytes, str | None]] = []
    errors: list[str] = []
    pos = 0
    while pos + 3 <= len(payload):
        obj_type = struct.unpack_from("<H", payload, pos)[0]
        obj_len = payload[pos + 2]
        value = payload[pos + 3 : pos + 3 + obj_len]
        if len(value) != obj_len:
            errors.append(f"object 0x{obj_type:04x}: truncated len={obj_len} raw={fmt_bytes(value)}")
            break
        decoded = None
        if obj_type == 0x1004 and obj_len == 2:
            decoded = f"temperature={struct.unpack('<h', value)[0] / 10:.1f}C"
        elif obj_type == 0x1006 and obj_len == 2:
            decoded = f"humidity={struct.unpack('<H', value)[0] / 10:.1f}%"
        elif obj_type == 0x100A and obj_len == 1:
            decoded = f"battery={value[0]}%"
        elif obj_type == 0x100D and obj_len == 4:
            temp, hum = struct.unpack("<hH", value)
            decoded = f"temperature={temp / 10:.1f}C humidity={hum / 10:.1f}%"
        elif obj_type == 0x100F and obj_len == 3:
            decoded = f"battery={value[0]}% voltage={struct.unpack('<H', value[1:])[0]}mV"
        elif obj_type == 0x4C01 and obj_len == 4:
            decoded = f"temperature={struct.unpack('<f', value)[0]:.1f}C"
        elif obj_type == 0x4C02 and obj_len == 1:
            decoded = f"humidity={value[0]}%"
        objects.append((obj_type, bytes(value), decoded))
        pos += 3 + obj_len
    if pos < len(payload):
        errors.append(f"trailing: {fmt_bytes(payload[pos:])}")
    return objects, errors


def decode_xiaomi_objects(payload: bytes) -> list[str]:
    out = []
    objects, errors = parse_xiaomi_objects(payload)
    for obj_type, value, decoded in objects:
        out.append(f"object 0x{obj_type:04x}: {decoded or fmt_bytes(value)}")
    out.extend(errors)
    return out


def parse_mibeacon_payload(data: bytes, *, mac: bytes | None = None, bindkey: bytes | None = None) -> tuple[dict, bytes | None, list[str]]:
    info: dict = {"raw": data}
    warnings: list[str] = []
    if len(data) < 5:
        warnings.append(f"too short for MiBeacon: {fmt_bytes(data)}")
        return info, None, warnings

    frctrl = struct.unpack_from("<H", data)[0]
    product_id = struct.unpack_from("<H", data, 2)[0]
    counter = data[4]
    flags = {
        "mesh": bool((frctrl >> 7) & 1),
        "registered": bool((frctrl >> 8) & 1),
        "solicited": bool((frctrl >> 9) & 1),
        "object": bool((frctrl >> 6) & 1),
        "capability": bool((frctrl >> 5) & 1),
        "mac": bool((frctrl >> 4) & 1),
        "encrypted": bool((frctrl >> 3) & 1),
    }
    info.update(
        {
            "frame_control": frctrl,
            "version": frctrl >> 12,
            "auth_mode": (frctrl >> 10) & 0x03,
            "product_id": product_id,
            "counter": counter,
            "flags": flags,
            "objects": [],
        }
    )

    pos = 5
    xiaomi_mac = mac
    if flags["mac"]:
        if len(data) < pos + 6:
            warnings.append("mac flag set but frame is too short")
            return info, None, warnings
        xiaomi_mac = data[pos : pos + 6][::-1]
        info["mac"] = xiaomi_mac
        pos += 6
    elif xiaomi_mac:
        info["mac"] = xiaomi_mac
        info["mac_provided"] = True

    if flags["capability"]:
        if len(data) < pos + 1:
            warnings.append("capability flag set but frame is too short")
            return info, None, warnings
        capability = data[pos]
        info["capability"] = capability
        pos += 1
        if capability & 0x20:
            if len(data) < pos + 2:
                warnings.append("capability IO flag set but frame is too short")
                return info, None, warnings
            info["capability_io"] = struct.unpack_from("<H", data, pos)[0]
            pos += 2

    if not flags["object"]:
        warnings.append("no object payload")
        return info, None, warnings

    if not flags["encrypted"]:
        payload = data[pos:]
        info["payload"] = payload
        objects, errors = parse_xiaomi_objects(payload)
        info["objects"] = objects
        warnings.extend(errors)
        return info, payload, warnings

    if len(data) < pos + 7:
        warnings.append("encrypted frame too short for payload/counter/mic")
        return info, None, warnings
    encrypted_payload = data[pos:-7]
    ext_counter = data[-7:-4]
    mic = data[-4:]
    info.update({"encrypted_payload": encrypted_payload, "ext_counter": ext_counter, "mic": mic})
    if bindkey is None:
        warnings.append("decrypt: bindkey not supplied")
        return info, None, warnings
    if AESCCM is None:
        warnings.append("decrypt: cryptography is not installed")
        return info, None, warnings
    if xiaomi_mac is None:
        warnings.append("decrypt: MAC is unknown; pass --mac or capture a clear-MAC/manufacturer frame first")
        return info, None, warnings

    nonce = xiaomi_mac[::-1] + data[2:5] + ext_counter
    info["nonce"] = nonce
    try:
        payload = AESCCM(bindkey, tag_length=4).decrypt(nonce, encrypted_payload + mic, b"\x11")
    except InvalidTag:
        warnings.append(f"decrypt: failed authentication nonce={nonce.hex()}")
        return info, None, warnings
    info["payload"] = payload
    objects, errors = parse_xiaomi_objects(payload)
    info["objects"] = objects
    warnings.extend(errors)
    return info, payload, warnings


def format_mac(mac: bytes) -> str:
    return mac.hex(":")


def parse_xiaomi_manufacturer_mac(data: bytes) -> bytes | None:
    # Xiaomi manufacturer frames seen from LYWSD02MMC look like:
    # 10 58 42 25 <counter> <mac little-endian>
    if len(data) >= 11 and data[0] == 0x10 and data[1:4] == b"\x58\x42\x25":
        return data[5:11][::-1]
    return None


def describe_mibeacon_info(info: dict, payload: bytes | None, warnings: list[str]) -> list[str]:
    if "frame_control" not in info:
        return warnings
    flags = info["flags"]
    out = [
        (
            f"mibeacon: frame_control=0x{info['frame_control']:04x} "
            f"version={info['version']} auth={info['auth_mode']} "
            f"product=0x{info['product_id']:04x} counter={info['counter']}"
        ),
        "flags: " + ", ".join(k for k, v in flags.items() if v),
    ]
    if "mac" in info:
        suffix = " (provided)" if info.get("mac_provided") else ""
        out.append("mac: " + format_mac(info["mac"]) + suffix)
    if "capability" in info:
        out.append(f"capability=0x{info['capability']:02x}")
    if "capability_io" in info:
        out.append(f"capability_io=0x{info['capability_io']:04x}")
    if "encrypted_payload" in info:
        out.append(f"encrypted_payload: {fmt_bytes(info['encrypted_payload'])}")
        out.append(f"ext_counter: {fmt_bytes(info['ext_counter'])}")
        out.append(f"mic: {fmt_bytes(info['mic'])}")
    if payload is not None:
        label = "decrypted_payload" if flags["encrypted"] else "payload"
        out.append(f"{label}: {fmt_bytes(payload)}")
        out.extend("  " + line for line in decode_xiaomi_objects(payload))
    out.extend(warnings)
    return out


def describe_mibeacon(data: bytes, *, mac: bytes | None = None, bindkey: bytes | None = None) -> list[str]:
    return describe_mibeacon_info(*parse_mibeacon_payload(data, mac=mac, bindkey=bindkey))


def mibeacon_decrypt_ok(data: bytes, *, mac: bytes | None = None, bindkey: bytes | None = None) -> bool:
    return any(line.startswith("decrypted_payload:") for line in describe_mibeacon(data, mac=mac, bindkey=bindkey))


def random_mi_device_id() -> bytes:
    suffix = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"[b % 62] for b in os.urandom(6))
    return b"\x00" + f"blt.3.129v{suffix}g00".encode("ascii")


def derive_mi_activation(private_key, device_public_key: bytes, device_id: bytes) -> tuple[bytes, bytes, bytes]:
    if AESCCM is None or ec is None or HKDF is None:
        raise RuntimeError("cryptography is required for Mi Activation")

    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device_public_key)
    shared = private_key.exchange(ec.ECDH(), peer)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=None,
        info=b"mible-setup-info",
    ).derive(shared)
    token = derived[:12]
    bind_key = derived[12:28]
    did_key = derived[28:44]
    encrypted_did = AESCCM(did_key, tag_length=4).encrypt(
        b"\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b",
        device_id,
        b"devID",
    )
    return token, bind_key, encrypted_did


def decode_mijia_value(uuid: str, data: bytes) -> str | None:
    uuid = uuid.lower()
    try:
        if uuid == CH_TIME and len(data) >= 4:
            ts, tz, reserved, fmt = parse_time_payload(data)
            decoded = f"{datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()} UTC{tz:+d}"
            if len(data) >= 7:
                fmt_label = TIME_FORMAT_LABELS.get(fmt, f"unknown(0x{fmt:02x})")
                decoded += f", reserved=0x{reserved:02x}, format={fmt_label}"
            return decoded
        if uuid == CH_DATA_COUNT and len(data) >= 8:
            read_index, latest_index = struct.unpack_from("<II", data)
            return f"read_index={read_index}, latest_index={latest_index}"
        if uuid == CH_INDEX and len(data) >= 4:
            return f"index={struct.unpack_from('<I', data)[0]}"
        if uuid in (CH_DATA_READ, CH_DATA_NOTIFY) and len(data) >= HISTORY_STRUCT.size:
            idx, stamp, temp_a, hum_a, temp_b, hum_b = parse_history_record(data)
            return (
                f"index={idx}, time={stamp.isoformat()}, "
                f"sample_a={temp_a:.2f}C/{hum_a}%, sample_b={temp_b:.2f}C/{hum_b}%"
            )
        if uuid == CH_UNIT and len(data) >= 1:
            return f"degrees_{'F' if data[0] == UNIT_F else 'C'} (raw=0x{data[0]:02x})"
        if uuid == CH_DATA and len(data) >= 3:
            temp, hum = parse_sensor(data)
            decoded = f"{temp:.2f}C, {hum}%RH"
            if len(data) >= 5:
                decoded += f", vbat={struct.unpack_from('<H', data, 3)[0]}mV"
            return decoded
        if uuid == CH_BATT and len(data) >= 1:
            return f"{data[0]}% (often stuck near 100%)"
        if uuid == CH_COMFORT and len(data) >= 6:
            temp_min, temp_max, hum_min, hum_max = parse_comfort(data)
            return f"temp={temp_min:.2f}..{temp_max:.2f}C, hum={hum_min}..{hum_max}%"
    except (struct.error, OSError, OverflowError):
        return None
    return None


def local_tz_offset_hours() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 3600) if offset else 0


async def find_device(name_prefix: str, timeout: float = 8.0):
    require_bleak()
    print(f"Scanning for '{name_prefix}…' (up to {timeout:.0f}s)…", file=sys.stderr)
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if d.name and d.name.startswith(name_prefix):
            return d
    return None


async def resolve_device(args):
    require_bleak()
    if args.address:
        device = await BleakScanner.find_device_by_address(args.address, timeout=8.0)
        if not device:
            sys.exit(f"Device {args.address} not found in scan. Is it nearby and advertising?")
        return device
    device = await find_device(args.name)
    if not device:
        sys.exit(f"No device with name starting '{args.name}' found. Try `scan`.")
    print(f"Using {device.name} ({device.address})", file=sys.stderr)
    return device


async def cmd_scan(args):
    require_bleak()
    devices = await BleakScanner.discover(timeout=args.seconds)
    matches = [d for d in devices if d.name and d.name.startswith(args.name)]
    if not matches:
        print(f"No '{args.name}…' devices found.")
        return
    for d in matches:
        print(f"{d.address}\t{d.name}")


async def cmd_adv(args):
    require_bleak()
    seen = {}
    known_macs: dict[str, bytes] = {}
    seen_objects: set[int] = set()
    latest: dict[int, tuple[datetime, str | None, bytes]] = {}
    decrypt_ok = 0
    decrypt_fail = 0
    provided_mac = parse_hex_bytes(args.mac, 6) if args.mac else None
    bindkey = parse_hex_bytes(args.bindkey, 16) if args.bindkey else None
    quiet = getattr(args, "summary", False) and not getattr(args, "verbose", False)

    def cb(device, adv):
        nonlocal decrypt_ok, decrypt_fail
        if args.name and not ((device.name or "").startswith(args.name) or (adv.local_name or "").startswith(args.name)):
            return
        key = device.address
        count = seen.get(key, 0) + 1
        seen[key] = count
        lines: list[str] = [f"\n[{count}] {device.address} {device.name or adv.local_name or ''} RSSI={adv.rssi}"]
        if adv.local_name and not quiet:
            lines.append(f"  local_name: {adv.local_name}")
        if adv.manufacturer_data:
            for company, data in adv.manufacturer_data.items():
                raw = bytes(data)
                if company == 0x038F:
                    mac_from_mfg = parse_xiaomi_manufacturer_mac(raw)
                    if mac_from_mfg:
                        known_macs[key] = mac_from_mfg
                if not quiet:
                    lines.append(f"  manufacturer 0x{company:04x}: {fmt_bytes(raw)}")
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                raw = bytes(data)
                if not quiet:
                    lines.append(f"  service_data {uuid}: {fmt_bytes(raw)}")
                if uuid.lower() == MIJIA_SERVICE:
                    mac_for_frame = provided_mac or known_macs.get(key)
                    info, payload, warnings = parse_mibeacon_payload(raw, mac=mac_for_frame, bindkey=bindkey)
                    if payload is not None:
                        decrypt_ok += 1 if info.get("flags", {}).get("encrypted") else 0
                        for obj_type, value, decoded in info.get("objects", []):
                            seen_objects.add(obj_type)
                            latest[obj_type] = (datetime.now().astimezone(), decoded, value)
                    elif bindkey and any(w.startswith("decrypt: failed authentication") for w in warnings):
                        decrypt_fail += 1
                    if "mac" in info and not info.get("mac_provided"):
                        known_macs[key] = info["mac"]
                    if not quiet:
                        for line in describe_mibeacon_info(info, payload, warnings):
                            lines.append(f"    {line}")
        if adv.service_uuids and not quiet:
            lines.append("  service_uuids: " + ", ".join(adv.service_uuids))
        if not quiet:
            print("\n".join(lines))

    scanner = BleakScanner(cb)
    print(f"Scanning advertisements for '{args.name}…' ({args.seconds:.0f}s)…", file=sys.stderr)
    await scanner.start()
    try:
        await asyncio.sleep(args.seconds)
    finally:
        await scanner.stop()
    if not seen:
        print("No matching advertisements seen.")
    if getattr(args, "summary", False):
        print()
        print("=== Passive summary ===")
        print(f"matching devices: {len(seen)}")
        if known_macs:
            for addr, mac in known_macs.items():
                print(f"{addr} Xiaomi MAC: {format_mac(mac)}")
        if bindkey:
            print(f"decrypted encrypted frames: {decrypt_ok}")
            if decrypt_fail:
                print(f"failed decryptions: {decrypt_fail}")
        if latest:
            for obj_type in sorted(latest):
                stamp, decoded, value = latest[obj_type]
                print(f"latest 0x{obj_type:04x}: {decoded or fmt_bytes(value)} at {stamp.isoformat(timespec='seconds')}")
        if seen_objects:
            print("seen object ids: " + ", ".join(f"0x{x:04x}" for x in sorted(seen_objects)))
            if 0x100A not in seen_objects and 0x100F not in seen_objects:
                print("battery object: not seen")


async def cmd_passive(args):
    args.summary = True
    await cmd_adv(args)


async def cmd_decode_adv(args):
    data = parse_hex_bytes(args.hex)
    mac = parse_hex_bytes(args.mac, 6) if args.mac else None
    bindkey = parse_hex_bytes(args.bindkey, 16) if args.bindkey else None
    for line in describe_mibeacon(data, mac=mac, bindkey=bindkey):
        print(line)


async def cmd_info(args):
    device = await resolve_device(args)
    async with BleakClient(device) as client:
        time_raw, unit_raw, batt_raw = await asyncio.gather(
            client.read_gatt_char(CH_TIME),
            client.read_gatt_char(CH_UNIT),
            client.read_gatt_char(CH_BATT),
        )

        ts, tz, _, _ = parse_time_payload(bytes(time_raw))
        unit = "F" if unit_raw[0] == UNIT_F else "C"
        battery = batt_raw[0]

        device_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        host_dt = datetime.now(tz=timezone.utc)
        rtt, _ = await estimate_read_rtt(client)
        drift = await measure_drift(client, rtt)
        if drift is None:
            drift = (device_dt - host_dt).total_seconds()

        print(f"Address     : {device.address}")
        print(f"Device time : {device_dt.astimezone().isoformat(timespec='seconds')}")
        print(f"Host time   : {host_dt.astimezone().isoformat(timespec='seconds')}")
        print(f"Drift       : {drift:+.2f}s (device - host)")
        print(f"Timezone    : UTC{tz:+d}")
        print(f"Units       : °{unit}")
        print(f"Battery     : {battery}% (firmware quirk; usually stuck near 100%)")

        sensor = await read_one_sensor(client)
        if sensor:
            t, h = sensor
            print(f"Temperature : {t:.2f} °C")
            print(f"Humidity    : {h}%")


async def read_one_sensor(client: BleakClient, timeout: float = 5.0):
    result: list[tuple[float, int]] = []
    done = asyncio.Event()

    def cb(_handle, data: bytearray):
        result.append(parse_sensor(data))
        done.set()

    async with notify_session(client, CH_DATA, cb):
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
            return result[0]
        except asyncio.TimeoutError:
            return None


async def estimate_read_rtt(client: BleakClient, samples: int = 3) -> tuple[float, bytes]:
    """Best-of-N round-trip time for a CH_TIME read, plus the last value read."""
    best = float("inf")
    raw = b""
    for _ in range(samples):
        t0 = time.monotonic()
        raw = bytes(await client.read_gatt_char(CH_TIME))
        best = min(best, time.monotonic() - t0)
    return best, raw


async def measure_drift(client: BleakClient, rtt: float, timeout: float = 2.5) -> float | None:
    """Device clock minus host clock, in seconds, to roughly half a BLE round trip.

    The time characteristic only has whole-second resolution, so poll it until the
    device's second ticks over; that tick marks the device's whole-second boundary.
    """
    prev = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent = time.time()
        ts = TIME_STRUCT.unpack_from(bytes(await client.read_gatt_char(CH_TIME)))[0]
        # The device answered somewhere mid-round-trip; assume halfway.
        host_at_read = sent + rtt / 2
        if prev is not None and ts != prev[0]:
            # The tick fell between the two reads; take the midpoint.
            return ts - (prev[1] + host_at_read) / 2
        prev = (ts, host_at_read)
    return None


async def cmd_set_time(args):
    tz = args.tz if args.tz is not None else local_tz_offset_hours()
    if not -12 <= tz <= 14:
        sys.exit(f"Timezone {tz} out of range [-12, 14]")

    device = await resolve_device(args)

    async with BleakClient(device) as client:
        reserved = 0
        fmt = TIME_FORMAT_24
        rtt, current = await estimate_read_rtt(client)
        with contextlib.suppress(Exception):
            _, _, reserved, fmt = parse_time_payload(current)

        # The device only takes whole seconds, so time the write to land right on a
        # second boundary rather than up to a second late (plus connection latency).
        lead = rtt / 2
        target = int(time.time()) + 1
        if target - lead - time.time() < 0.05:
            target += 1
        await asyncio.sleep(max(0.0, target - lead - time.time()))
        await client.write_gatt_char(CH_TIME, pack_time_payload(target, tz, reserved, fmt), response=True)

        drift = await measure_drift(client, rtt)

    when = datetime.fromtimestamp(target).isoformat(timespec="seconds")
    print(f"Set time to {when} (UTC{tz:+d}), BLE round trip ~{rtt * 1000:.0f} ms")
    if drift is not None:
        print(f"Verified drift: {drift:+.2f}s (device - host)")


async def cmd_set_time_format(args):
    fmt = TIME_FORMAT_12 if args.format == "12" else TIME_FORMAT_24
    device = await resolve_device(args)

    async with BleakClient(device) as client:
        current = bytes(await client.read_gatt_char(CH_TIME))
        ts, tz, reserved, _ = parse_time_payload(current)
        await client.write_gatt_char(CH_TIME, pack_time_payload(ts, tz, reserved, fmt), response=True)
        updated = bytes(await client.read_gatt_char(CH_TIME))

    print(f"Previous time raw: {fmt_bytes(current)}")
    print(f"Wrote format    : {args.format}h (fmt=0x{fmt:02x})")
    print(f"Updated time raw : {fmt_bytes(updated)}")
    decoded = decode_mijia_value(CH_TIME, updated)
    if decoded:
        print(f"Decoded         : {decoded}")


async def cmd_set_unit(args):
    unit = args.unit.upper()
    code = UNIT_F if unit == "F" else UNIT_C
    device = await resolve_device(args)
    async with BleakClient(device) as client:
        await client.write_gatt_char(CH_UNIT, bytes([code]), response=True)
    print(f"Set unit to °{unit}")


async def cmd_comfort(args):
    device = await resolve_device(args)
    async with BleakClient(device) as client:
        current = bytes(await client.read_gatt_char(CH_COMFORT))
        temp_min, temp_max, hum_min, hum_max = parse_comfort(current)
        print(f"Current comfort raw : {fmt_bytes(current)}")
        print(f"Current comfort     : {temp_min:.2f}..{temp_max:.2f} C, {hum_min}..{hum_max}% RH")

        wants_write = any(v is not None for v in (args.temp_min, args.temp_max, args.hum_min, args.hum_max))
        if not wants_write:
            return

        new_temp_min = temp_min if args.temp_min is None else args.temp_min
        new_temp_max = temp_max if args.temp_max is None else args.temp_max
        new_hum_min = hum_min if args.hum_min is None else args.hum_min
        new_hum_max = hum_max if args.hum_max is None else args.hum_max
        payload = pack_comfort(new_temp_min, new_temp_max, new_hum_min, new_hum_max)
        await client.write_gatt_char(CH_COMFORT, payload, response=True)
        updated = bytes(await client.read_gatt_char(CH_COMFORT))
        upd_temp_min, upd_temp_max, upd_hum_min, upd_hum_max = parse_comfort(updated)
        print(f"Updated comfort raw : {fmt_bytes(updated)}")
        print(f"Updated comfort     : {upd_temp_min:.2f}..{upd_temp_max:.2f} C, {upd_hum_min}..{upd_hum_max}% RH")


async def cmd_mi_activate(args):
    if not args.yes:
        sys.exit("This changes Mi bind state. Re-run with --yes if you really want to activate.")
    if AESCCM is None or ec is None or HKDF is None:
        sys.exit("This command needs `cryptography`. Run through `uv run lywsd.py ...`.")

    device = await resolve_device(args)
    private_key = ec.generate_private_key(ec.SECP256R1())
    own_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    device_id = random_mi_device_id()
    state = "init"
    device_public = b""
    generated: tuple[bytes, bytes, bytes] | None = None
    events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    done = asyncio.Event()

    async def send_hex(client: BleakClient, uuid: str, hexstr: str):
        print(f"send {uuid}: {hexstr}")
        await client.write_gatt_char(uuid, bytes.fromhex(hexstr), response=False)

    async def start_register(client: BleakClient):
        nonlocal state
        await send_hex(client, CH_FE95_AUTH, "15000000")
        state = "wait_register_echo"
        await send_hex(client, CH_FE95_STANDARD, "000000030400")

    async def send_public_key_chunks(client: BleakClient):
        nonlocal state
        public_xy = own_public[1:]
        state = "wait_device_public"
        for idx, off in enumerate(range(0, len(public_xy), 18), start=1):
            chunk = public_xy[off : off + 18]
            await send_hex(client, CH_FE95_STANDARD, f"{idx:02x}00" + chunk.hex())

    async def send_encrypted_did_chunks(client: BleakClient, encrypted_did: bytes):
        nonlocal state
        state = "wait_did_ack"
        for idx, off in enumerate(range(0, len(encrypted_did), 18), start=1):
            chunk = encrypted_did[off : off + 18]
            await send_hex(client, CH_FE95_STANDARD, f"{idx:02x}00" + chunk.hex())

    async with BleakClient(device) as client:
        if not client.services.get_service(MIJIA_SERVICE):
            sys.exit("FE95 Mi service is not present.")

        def auth_cb(_handle, data: bytearray):
            events.put_nowait(("auth", bytes(data).hex()))

        def std_cb(_handle, data: bytearray):
            events.put_nowait(("std", bytes(data).hex()))

        await client.start_notify(CH_FE95_AUTH, auth_cb)
        await client.start_notify(CH_FE95_STANDARD, std_cb)
        try:
            print("Starting Mi Activation. This may change the device's bind key.")
            await send_hex(client, CH_FE95_AUTH, "a2000000")
            deadline = asyncio.get_running_loop().time() + args.seconds

            while not done.is_set():
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    raise TimeoutError("Mi Activation timed out")
                source, value = await asyncio.wait_for(events.get(), timeout=timeout)

                if source == "auth":
                    print(f"auth: {value}")
                    if value in {"11000000", "13000000"}:
                        done.set()
                    elif value in {"12000000", "14000000", "e3000000"}:
                        raise RuntimeError(f"activation failed with auth status {value}")
                    continue

                print(f"std : {value}")
                if value.startswith("00000400"):
                    await send_hex(client, CH_FE95_STANDARD, "00000500" + value[8:])
                    continue
                if value.startswith("00000401"):
                    await send_hex(client, CH_FE95_STANDARD, "00000501" + value[8:])
                    await asyncio.sleep(0.25)
                    await send_hex(client, CH_FE95_AUTH, "a2000000")
                    continue
                if value.startswith("0000020001000000"):
                    if len(value) > 20:
                        device_id = bytes.fromhex(value[16:])
                        print(f"device id from device: {device_id.hex()} ({device_id[1:].decode('ascii', errors='replace')})")
                    await send_hex(client, CH_FE95_STANDARD, "00000100")
                    await asyncio.sleep(0.25)
                    await start_register(client)
                    continue
                if value == "000000000100":
                    await send_hex(client, CH_FE95_STANDARD, "00000101")
                    continue
                if value == "000000000200":
                    await send_hex(client, CH_FE95_STANDARD, "00000101")
                    continue
                if value == "010001000000":
                    await send_hex(client, CH_FE95_STANDARD, "00000100")
                    await asyncio.sleep(0.25)
                    await start_register(client)
                    continue
                if value == "000000030400":
                    await send_hex(client, CH_FE95_STANDARD, "00000101")
                    state = "wait_pub_request"
                    continue
                if state == "wait_pub_request" and value == "00000101":
                    await send_public_key_chunks(client)
                    continue
                if state == "wait_device_public" and value.startswith("0100"):
                    device_public = b"\x04" + bytes.fromhex(value[4:])
                    continue
                if state == "wait_device_public" and value.startswith("0200"):
                    device_public += bytes.fromhex(value[4:])
                    continue
                if state == "wait_device_public" and value.startswith("0300"):
                    device_public += bytes.fromhex(value[4:])
                    continue
                if state == "wait_device_public" and value.startswith("0400"):
                    device_public += bytes.fromhex(value[4:])
                    token, bind_key, encrypted_did = derive_mi_activation(private_key, device_public, device_id)
                    generated = (token, bind_key, encrypted_did)
                    print(f"Generated Mi Token   : {token.hex()}")
                    print(f"Generated Mi Bind Key: {bind_key.hex()}")
                    await send_hex(client, CH_FE95_STANDARD, "00000100")
                    await send_hex(client, CH_FE95_STANDARD, "000000000200")
                    state = "wait_did_request"
                    continue
                if state == "wait_device_public" and value.startswith("00000203"):
                    device_public = b"\x04" + bytes.fromhex(value[8:])
                    token, bind_key, encrypted_did = derive_mi_activation(private_key, device_public, device_id)
                    generated = (token, bind_key, encrypted_did)
                    print(f"Generated Mi Token   : {token.hex()}")
                    print(f"Generated Mi Bind Key: {bind_key.hex()}")
                    await send_hex(client, CH_FE95_STANDARD, "00000300")
                    await send_hex(client, CH_FE95_STANDARD, "000000000200")
                    state = "wait_did_request"
                    continue
                if state == "wait_did_request" and value == "00000101":
                    if generated is None:
                        raise RuntimeError("device requested encrypted did before key derivation")
                    await send_encrypted_did_chunks(client, generated[2])
                    continue
                if state == "wait_did_ack" and value == "00000100":
                    state = "verify"
                    await send_hex(client, CH_FE95_AUTH, "13000000")
                    continue

            if generated:
                print()
                print("Mi Activation generated:")
                print(f"Device ID   : {device_id[1:].decode('ascii', errors='replace')}")
                print(f"Mi Token    : {generated[0].hex()}")
                print(f"Mi Bind Key : {generated[1].hex()}")
                print()
                print("Test with:")
                print(f"uv run lywsd.py passive --seconds 60 --bindkey {generated[1].hex()}")
            else:
                print("Activation reached completion status, but no key was generated.")
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(CH_FE95_STANDARD)
            with contextlib.suppress(Exception):
                await client.stop_notify(CH_FE95_AUTH)


async def cmd_probe(args):
    device = await resolve_device(args)
    print(f"Connecting to {device.address}…", file=sys.stderr)

    async with BleakClient(device) as client:
        print("=== GATT services ===")
        for service in client.services:
            print(f"\nService {service.uuid}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                name = MIJIA_CHARS.get(char.uuid.lower(), "")
                label = f"  [{name}]" if name else ""
                print(f"  Char {char.uuid}{label}  ({props})")
                desc = await read_user_description(client, char)
                if desc and desc != name:
                    print(f"       descriptor: {desc}")

        print()
        print("=== Device Information (0x180A) ===")
        for uuid, (label, kind) in DEVICE_INFO_CHARS.items():
            char = client.services.get_characteristic(uuid)
            if not char:
                continue
            try:
                v = await client.read_gatt_char(uuid)
                if kind == "str":
                    print(f"{label:18s}: {v.decode('utf-8', errors='replace').strip()}")
                else:
                    print(f"{label:18s}: {v.hex(' ')}")
            except Exception as e:
                print(f"{label:18s}: read failed: {e}")


async def read_user_description(client: BleakClient, char) -> str | None:
    for descriptor in char.descriptors:
        if descriptor.uuid.lower() != MIJIA_DESCRIPTOR:
            continue
        try:
            raw = await client.read_gatt_descriptor(descriptor.handle)
            return strip_nul(bytes(raw))
        except Exception:
            return None
    return None


async def cmd_dump_mijia(args):
    device = await resolve_device(args)
    print(f"Connecting to {device.address}…", file=sys.stderr)

    async with BleakClient(device) as client:
        service = client.services.get_service(SERVICE)
        if not service:
            sys.exit("Mijia service ebe0ccb0 is not present.")

        print("=== Mijia service dump ===")
        for char in service.characteristics:
            name = MIJIA_CHARS.get(char.uuid.lower(), "Unknown")
            props = ",".join(char.properties)
            desc = await read_user_description(client, char)
            label = desc or name
            print(f"\n{label} ({char.uuid})")
            print(f"  properties: {props}")
            if "read" not in char.properties:
                continue
            try:
                raw = bytes(await client.read_gatt_char(char.uuid))
            except Exception as e:
                print(f"  read failed: {e}")
                continue
            print(f"  raw: {fmt_bytes(raw)}")
            decoded = decode_mijia_value(char.uuid, raw)
            if decoded:
                print(f"  decoded: {decoded}")


async def cmd_history(args):
    device = await resolve_device(args)
    print(f"Connecting to {device.address}…", file=sys.stderr)

    async with BleakClient(device) as client:
        counts_raw = bytes(await client.read_gatt_char(CH_DATA_COUNT))
        read_index, latest_index = struct.unpack_from("<II", counts_raw)
        start_index = args.from_index if args.from_index is not None else read_index
        wanted = args.count
        received = 0
        accepted = 0
        done = asyncio.Event()
        csv_file = open(args.csv, "w", newline="") if args.csv else None
        writer = csv.writer(csv_file) if csv_file else None

        print(f"Data Count raw: {fmt_bytes(counts_raw)}")
        print(f"Starting index : {start_index}")
        print(f"Read index     : {read_index}")
        print(f"Latest index   : {latest_index}")
        print()
        print("index,time_utc,temp_a_c,hum_a,temp_b_c,hum_b")
        if writer:
            writer.writerow(["index", "time_utc", "temp_a_c", "hum_a", "temp_b_c", "hum_b"])

        def cb(_handle, data: bytearray):
            nonlocal received, accepted
            raw = bytes(data)
            if len(raw) < HISTORY_STRUCT.size:
                print(f"# short notification: {fmt_bytes(raw)}")
                return
            idx, stamp, temp_a, hum_a, temp_b, hum_b = parse_history_record(raw)
            received += 1
            if args.filter_range and idx < start_index:
                print(f"# skipped out-of-range record index={idx} raw={fmt_bytes(raw)}")
                return
            print(f"{idx},{stamp.isoformat()},{temp_a:.2f},{hum_a},{temp_b:.2f},{hum_b}")
            if writer:
                writer.writerow([idx, stamp.isoformat(), f"{temp_a:.2f}", hum_a, f"{temp_b:.2f}", hum_b])
            accepted += 1
            if wanted and accepted >= wanted:
                done.set()

        try:
            async with notify_session(client, CH_DATA_NOTIFY, cb):
                await client.write_gatt_char(CH_INDEX, struct.pack("<I", start_index), response=True)
                try:
                    await asyncio.wait_for(done.wait(), timeout=args.seconds)
                except asyncio.TimeoutError:
                    if received == 0:
                        print("# no history notifications before timeout")
        finally:
            if csv_file:
                csv_file.close()


async def cmd_watch(args):
    device = await resolve_device(args)

    def cb(_handle, data: bytearray):
        temp, hum = parse_sensor(data)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {temp:6.2f} °C   {hum:3d}%")

    async with BleakClient(device) as client:
        async with notify_session(client, CH_DATA, cb):
            if args.seconds:
                await asyncio.sleep(args.seconds)
            else:
                print("Streaming. Ctrl-C to stop.", file=sys.stderr)
                await asyncio.Event().wait()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--address", help="BLE MAC / UUID. If omitted, scans by name.")
    common.add_argument("--name", default=DEFAULT_NAME_PREFIX, help=f"Name prefix to match (default: {DEFAULT_NAME_PREFIX})")

    s = sub.add_parser("scan", help="List nearby LYWSD02 devices")
    s.add_argument("--name", default=DEFAULT_NAME_PREFIX)
    s.add_argument("--seconds", type=float, default=8.0)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("adv", help="Print matching BLE advertisements")
    s.add_argument("--name", default=DEFAULT_NAME_PREFIX)
    s.add_argument("--seconds", type=float, default=20.0)
    s.add_argument("--mac", help="Public Xiaomi MAC for decrypting frames without a clear MAC")
    s.add_argument("--bindkey", help="32-hex-character Mi bind key for encrypted advertisements")
    s.add_argument("--summary", action="store_true", help="Print a latest-value/object summary after scanning")
    s.add_argument("--verbose", action="store_true", help="With --summary, also print every matching packet")
    s.set_defaults(func=cmd_adv)

    s = sub.add_parser("passive", help="Decode passive LYWSD02MMC MiBeacon advertisements")
    s.add_argument("--name", default=DEFAULT_NAME_PREFIX)
    s.add_argument("--seconds", type=float, default=60.0)
    s.add_argument("--mac", help="Public Xiaomi MAC; learned from clear FE95/manufacturer frames when omitted")
    s.add_argument("--bindkey", required=True, help="32-hex-character Mi bind key for encrypted advertisements")
    s.add_argument("--verbose", action="store_true", help="Print every matching packet in addition to the summary")
    s.set_defaults(func=cmd_passive)

    s = sub.add_parser("decode-adv", help="Decode one raw FE95 MiBeacon service-data payload")
    s.add_argument("hex", help="Raw service-data bytes as hex")
    s.add_argument("--mac", help="Public Xiaomi MAC for decrypting frames without a clear MAC")
    s.add_argument("--bindkey", help="32-hex-character Mi bind key for encrypted advertisements")
    s.set_defaults(func=cmd_decode_adv)

    s = sub.add_parser("info", parents=[common], help="Read time, battery, units, and a sensor sample")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("dump-mijia", parents=[common], help="Read and decode known Mijia service characteristics")
    s.set_defaults(func=cmd_dump_mijia)

    s = sub.add_parser("history", parents=[common], help="Read stored history records through Index/Data Notify")
    s.add_argument("--from-index", type=int, help="History index to request (default: device read index)")
    s.add_argument("--count", type=int, default=10, help="Stop after N records (default: 10)")
    s.add_argument("--seconds", type=float, default=8.0, help="Timeout while waiting for notifications (default: 8)")
    s.add_argument("--csv", help="Also write records to a CSV file")
    s.add_argument("--filter-range", action="store_true", help="Skip records below the requested start index")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("set-time", parents=[common], help="Push host clock + timezone to the device")
    s.add_argument("--tz", type=int, help="Timezone offset in hours (default: from system)")
    s.set_defaults(func=cmd_set_time)

    s = sub.add_parser("set-time-format", parents=[common], help="Switch display between 12-hour and 24-hour mode")
    s.add_argument("format", choices=["12", "24"])
    s.set_defaults(func=cmd_set_time_format)

    s = sub.add_parser("set-unit", parents=[common], help="Switch display unit")
    s.add_argument("unit", choices=["C", "F", "c", "f"])
    s.set_defaults(func=cmd_set_unit)

    s = sub.add_parser("comfort", parents=[common], help="Read or update comfort threshold range")
    s.add_argument("--temp-min", type=float, help="Comfort minimum temperature in C")
    s.add_argument("--temp-max", type=float, help="Comfort maximum temperature in C")
    s.add_argument("--hum-min", type=int, help="Comfort minimum relative humidity percent")
    s.add_argument("--hum-max", type=int, help="Comfort maximum relative humidity percent")
    s.set_defaults(func=cmd_comfort)

    s = sub.add_parser("mi-activate", parents=[common], help="Register/rebind through Mi Activation and generate a new Mi bind key")
    s.add_argument("--yes", action="store_true", help="Required: acknowledge this changes Mi bind state")
    s.add_argument("--seconds", type=float, default=30.0, help="Activation timeout (default: 30)")
    s.set_defaults(func=cmd_mi_activate)

    s = sub.add_parser("watch", parents=[common], help="Stream sensor readings until interrupted")
    s.add_argument("--seconds", type=float, help="Stop after N seconds (default: run forever)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("probe", parents=[common], help="Enumerate GATT services")
    s.set_defaults(func=cmd_probe)

    return p


def main():
    args = build_parser().parse_args()
    try:
        asyncio.run(args.func(args))
    except CliError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
