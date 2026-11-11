import pytest

from screenlight.beacon.protocol import (
    Frame,
    advert,
    build_frame,
    crc8,
    derive_from_off_on,
    dp_colour,
    dp_power,
    parse_frame,
    xxtea_decrypt,
    xxtea_encrypt,
)

# Dummy keys only. Expected vectors come from the spike implementation that controlled the real bulb.
LK = "0123456789abcdef"
BK = bytes(range(16))


def test_xxtea_known_answer_and_round_trip():
    assert xxtea_encrypt(bytes(16), LK.encode()).hex() == "e1939f01f4c06036ae8fb9dcc7883b7f"
    assert xxtea_decrypt(xxtea_encrypt(bytes(range(16)), LK.encode()), LK.encode()) == bytes(range(16))


def test_xxtea_rejects_bad_lengths():
    with pytest.raises(ValueError):
        xxtea_encrypt(bytes(15), LK.encode())
    with pytest.raises(ValueError):
        xxtea_decrypt(bytes(16), b"short")


def test_crc8_check_value():
    assert crc8(b"123456789") == 0xF4


@pytest.mark.parametrize("seq,payload,expected", [
    (0x100, dp_power(False), "0b5c110002010005d6efa00f81f8516528a4a9a2904da64bd2ab"),
    (0x101, dp_power(True), "0b5c11000201010533eb14c7a272416d6b2aa86361a7449d20a9"),
    (0x102, bytes((0x0B, 0x04, 0, 240, 100, 100)), "0b5c110002010205c0f4f27e72e123d03a51e1fbe6037f120194"),
])
def test_build_frame_known_answers(seq, payload, expected):
    assert build_frame(0x5C11, 0x0002, seq, payload, LK, BK).hex() == expected


def test_parse_round_trip_and_rejects_corruption():
    f = build_frame(0x4001, 7, 42, dp_colour(2 / 3, 1, 1), LK, BK)
    assert parse_frame(f, LK, BK) == Frame(0x4001, 7, 42, {11: bytes((0, 240, 100, 100))})
    for i in (8, 12, 25):
        bad = bytearray(f)
        bad[i] ^= 1
        assert parse_frame(bytes(bad), LK, BK) is None
    assert parse_frame(f, "fedcba9876543210", BK) is None


def test_parse_decodes_multiple_datapoints():
    f = build_frame(0x4001, 7, 43, dp_power(True) + dp_colour(0, 1, 1), LK, BK)
    assert parse_frame(f, LK, BK).datapoints == {1: b"\x01", 11: bytes((0, 0, 100, 100))}


def test_advert_layout():
    a = advert(bytes(26))
    assert len(a) == 31 and a[:5] == bytes.fromhex("0201061b03")


def test_dp_colour_clamps_dark_and_wraps_hue():
    assert dp_colour(1.0, 1.0, 0.0) == bytes((0x0B, 0x04, 0, 0, 100, 1))
    assert dp_colour(0.5, 0.5, 0.5) == bytes((0x0B, 0x04, 0, 180, 50, 50))


def test_derive_from_off_on_ignores_decoys():
    other = "fedcba9876543210"
    frames = [build_frame(0x7A09, 2, 9, dp_power(False), other, BK),   # decoy: another bulb's key
              build_frame(0x7A09, 2, 10, dp_colour(0, 1, 1), LK, BK),
              build_frame(0x7A09, 2, 11, dp_power(False), LK, BK),
              build_frame(0x7A09, 2, 12, dp_power(True), LK, BK)]
    assert derive_from_off_on(frames, LK) == (BK, 2)
    assert derive_from_off_on(frames[:3], LK) is None                    # OFF without ON
