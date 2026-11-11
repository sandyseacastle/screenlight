"""Tuya P2 beacon codec: the one-way commands Smart Life broadcasts to Bluetooth beacon bulbs.

Frame (26 bytes, carried as AD type 0x03 after Flags 02 01 06):
  0  type 0x0b (DP request)    1-2 source ID    3-4 node ID    5-6 sequence    7 subcommand 0x05 (DP write)
  8  CRC-8 of the padded plaintext    9-24 XXTEA(plaintext, local key) XOR beacon key    25 outer CRC
Plaintext is compact datapoints: dp_id, (type << 4 | length), value..., zero-padded to 16 bytes.

Matches FinnGrndl/SunsetLampHATuyaBridge (MIT) and was verified against a real bulb.
"""

import struct
from collections.abc import Iterable
from dataclasses import dataclass

BLOCK = 16
FRAME_LEN = 26
FRAME_DP = 0x0B
SUBCMD_DP_WRITE = 0x05
FLAGS = b"\x02\x01\x06"
_DELTA = 0x9E3779B9
_ROUNDS = 19
_M32 = 0xFFFFFFFF


@dataclass(frozen=True)
class Frame:
    source_id: int
    node_id: int
    seq: int
    datapoints: dict[int, bytes]


def _words(data: bytes, what: str) -> list[int]:
    if len(data) != BLOCK:
        raise ValueError(f"{what} must be exactly {BLOCK} bytes")
    return list(struct.unpack(">4I", data))


def _mx(z: int, y: int, total: int, p: int, e: int, k: list[int]) -> int:
    return ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((total ^ y) + (k[(p & 3) ^ e] ^ z))) & _M32


def xxtea_encrypt(block: bytes, key: bytes) -> bytes:
    """Tuya's big-endian XXTEA variant on one 16-byte block."""
    v, k = _words(block, "block"), _words(key, "key")
    total, z = 0, v[3]
    for _ in range(_ROUNDS):
        total = (total + _DELTA) & _M32
        e = (total >> 2) & 3
        for p in range(4):
            v[p] = (v[p] + _mx(z, v[(p + 1) & 3], total, p, e, k)) & _M32
            z = v[p]
    return struct.pack(">4I", *v)


def xxtea_decrypt(block: bytes, key: bytes) -> bytes:
    v, k = _words(block, "block"), _words(key, "key")
    total, y = (_ROUNDS * _DELTA) & _M32, v[0]
    while total:
        e = (total >> 2) & 3
        for p in range(3, -1, -1):
            v[p] = (v[p] - _mx(v[(p - 1) & 3], y, total, p, e, k)) & _M32
            y = v[p]
        total = (total - _DELTA) & _M32
    return struct.pack(">4I", *v)


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, init 0."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def outer_crc(frame25: bytes) -> int:
    """Tuya's P2 frame CRC over the first 25 (unmasked) bytes."""
    crc = 0
    for byte in bytes((frame25[0] & 0xFC,)) + frame25[1:25]:
        crc = (crc ^ (byte << 8)) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc ^= 0x8380
            crc = (crc << 1) & 0xFFFF
    return crc >> 8


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _key(local_key: str) -> bytes:
    key = local_key.encode("ascii")
    if len(key) != BLOCK:
        raise ValueError("local key must be 16 ASCII characters")
    return key


def _padded(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= BLOCK:
        raise ValueError("payload must be 1-16 bytes")
    return payload.ljust(BLOCK, b"\0")


def build_frame(source_id: int, node_id: int, seq: int, payload: bytes, local_key: str,
                beacon_key: bytes) -> bytes:
    plain = _padded(payload)
    enc = xxtea_encrypt(plain, _key(local_key))
    head = struct.pack(">BHHHBB", FRAME_DP, source_id, node_id, seq, SUBCMD_DP_WRITE, crc8(plain))
    return head + _xor(enc, beacon_key) + bytes((outer_crc(head + enc),))


def _decode_datapoints(plain: bytes) -> dict[int, bytes]:
    dps, i = {}, 0
    while i + 2 <= len(plain) and plain[i]:
        length = plain[i + 1] & 0x0F
        if length == 0 or i + 2 + length > len(plain):
            break
        dps[plain[i]] = plain[i + 2:i + 2 + length]
        i += 2 + length
    return dps


def parse_frame(frame: bytes, local_key: str, beacon_key: bytes) -> Frame | None:
    """Validate both CRCs and decrypt; None if the frame isn't ours or is corrupt."""
    if len(frame) != FRAME_LEN or frame[0] != FRAME_DP:
        return None
    enc = _xor(frame[9:25], beacon_key)
    if outer_crc(frame[:9] + enc) != frame[25]:
        return None
    plain = xxtea_decrypt(enc, _key(local_key))
    if crc8(plain) != frame[8]:
        return None
    _, source, node, seq = struct.unpack(">BHHH", frame[:7])
    return Frame(source, node, seq, _decode_datapoints(plain))


def advert(frame: bytes) -> bytes:
    """The 31-byte advertisement. The bulb ignores adverts with Flags anywhere but first."""
    return FLAGS + bytes((len(frame) + 1, 0x03)) + frame


def dp_power(on: bool) -> bytes:
    return bytes((0x01, 0x11, int(on)))


def dp_colour(h: float, s: float, v: float) -> bytes:
    """DP 11 from HSV floats (0-1): H 0-359, S 0-100, V 1-100."""
    hue = round(h * 360) % 360
    return bytes((0x0B, 0x04, hue >> 8, hue & 0xFF, round(s * 100), max(1, round(v * 100))))


def derive_from_off_on(frames: Iterable[bytes], local_key: str) -> tuple[bytes, int] | None:
    """Recover (beacon key, node ID) from a capture containing this bulb's OFF and then ON commands."""
    frames = [f for f in frames if len(f) == FRAME_LEN and f[0] == FRAME_DP and f[7] == SUBCMD_DP_WRITE]
    off_plain = _padded(dp_power(False))
    off_crc, off_enc = crc8(off_plain), xxtea_encrypt(off_plain, _key(local_key))
    for off in frames:
        if off[8] != off_crc or outer_crc(off[:9] + off_enc) != off[25]:
            continue
        beacon_key = _xor(off[9:25], off_enc)
        for on in frames:
            if on is off or on[1:5] != off[1:5]:
                continue
            parsed = parse_frame(on, local_key, beacon_key)
            if parsed and parsed.datapoints == {1: b"\x01"}:
                return beacon_key, parsed.node_id
    return None
