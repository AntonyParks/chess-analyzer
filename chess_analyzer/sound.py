"""A short click for moves, synthesised once in memory. Playback is Windows-only."""

from __future__ import annotations

import io
import logging
import math
import struct
import wave

try:
    import winsound
except ImportError:  # not Windows
    winsound = None

log = logging.getLogger(__name__)

_RATE = 22050
_DURATION = 0.07
_wav: bytes | None = None


def _synthesize() -> bytes:
    frames = int(_RATE * _DURATION)
    samples = []
    for i in range(frames):
        t = i / _RATE
        envelope = math.exp(-t * 55)
        value = envelope * (
            0.5 * math.sin(2 * math.pi * 700 * t)
            + 0.3 * math.sin(2 * math.pi * 1400 * t)
            + 0.2 * math.sin(2 * math.pi * 350 * t)
        )
        samples.append(max(-32767, min(32767, int(value * 32767))))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_RATE)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def play_move() -> None:
    """Play the move click asynchronously; silently does nothing where unsupported."""
    global _wav
    if winsound is None:
        return
    if _wav is None:
        _wav = _synthesize()
    try:
        winsound.PlaySound(_wav, winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except RuntimeError:
        log.debug("Could not play the move sound", exc_info=True)
