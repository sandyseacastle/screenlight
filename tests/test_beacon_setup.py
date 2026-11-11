from screenlight.beacon.protocol import build_frame, dp_colour, dp_power
from screenlight.gui.beacon_dialog import listen_for_off_on

LK, BK = "0123456789abcdef", bytes(range(16))


class ChunkRadio:
    """capture() emits one scripted chunk of frames per call."""

    def reset_denied(self):
        pass

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = []

    def capture(self, seconds, on_frame):
        self.calls.append(seconds)
        for frame in self.chunks.pop(0) if self.chunks else []:
            on_frame(frame)


def test_listen_finds_key_and_stops_early():
    off = build_frame(0x7A09, 2, 5, dp_power(False), LK, BK)
    on = build_frame(0x7A09, 2, 6, dp_power(True), LK, BK)
    radio = ChunkRadio([[build_frame(0x7A09, 2, 4, dp_colour(0, 1, 1), LK, BK)], [off], [on], [], []])
    assert listen_for_off_on(radio, LK, seconds=60, chunk=5) == (BK, 2)
    assert radio.calls == [5, 5, 5]


def test_listen_gives_up_after_the_time_limit():
    radio = ChunkRadio([])
    assert listen_for_off_on(radio, LK, seconds=15, chunk=5) is None
    assert radio.calls == [5, 5, 5]


def test_listen_can_be_cancelled():
    radio = ChunkRadio([])
    assert listen_for_off_on(radio, LK, seconds=60, chunk=5, cancelled=lambda: len(radio.calls) >= 2) is None
    assert radio.calls == [5, 5]


def test_listen_is_user_initiated_so_it_clears_a_cancelled_prompt():
    radio = ChunkRadio([])
    resets = []
    radio.reset_denied = lambda: resets.append(len(radio.calls))
    listen_for_off_on(radio, LK, seconds=5, chunk=5)
    assert resets == [0]                 # before the first capture
