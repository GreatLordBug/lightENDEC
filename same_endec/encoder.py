from __future__ import annotations

from array import array
from datetime import datetime, timezone
import math
import re

import numpy as np

from .audio_io import resample_pcm16
from .core import attention_tone


SAMPLE_RATE = 22050
MARK_HZ = 2083.333333
SPACE_HZ = 1562.5
BAUD = 520.833333
_CALLSIGN_RE = re.compile(r"^[A-Z0-9/-]{1,8}$")
_ORIGINATOR_RE = re.compile(r"^[A-Z]{3}$")
_EVENT_RE = re.compile(r"^[A-Z0-9]{3}$")


def build_same_header(
    event: str,
    locations: tuple[str, ...],
    callsign: str,
    purge_minutes: int = 30,
    originator: str = "EAS",
    now: datetime | None = None,
) -> str:
    event = event.upper()
    originator = originator.upper()
    callsign = callsign.upper()
    if not _EVENT_RE.fullmatch(event):
        raise ValueError("Event must be a three-character SAME event code")
    if not _ORIGINATOR_RE.fullmatch(originator):
        raise ValueError("Originator must be a three-letter SAME code")
    if not _CALLSIGN_RE.fullmatch(callsign):
        raise ValueError("Callsign must be 1-8 SAME header characters")
    if not locations or any(not re.fullmatch(r"\d{6}", item) for item in locations):
        raise ValueError("At least one six-character SAME location code is required")
    if not 1 <= purge_minutes <= 9999:
        raise ValueError("Purge duration must be between 1 and 9999 minutes")
    timestamp = now or datetime.now(timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    purge = f"{purge_minutes // 60:02d}{purge_minutes % 60:02d}"
    date_code = timestamp.strftime("%j%H%M")
    return (
        f"ZCZC-{originator}-{event}-{'-'.join(locations)}+{purge}-"
        f"{date_code}-{callsign}-"
    )


def _encode_data(message: str | bytes, sample_rate: int) -> np.ndarray:
    bits = []
    encoded = message.encode("ascii") if isinstance(message, str) else message
    for character in encoded:
        bits.extend((character >> bit) & 1 for bit in range(8))
    bits.extend([1] * 4)
    frame_edges = np.rint(np.arange(len(bits) + 1) * sample_rate / BAUD).astype(int)
    output = np.empty(frame_edges[-1], dtype=np.float64)
    phase = 0.0
    for index, bit in enumerate(bits):
        start, end = frame_edges[index], frame_edges[index + 1]
        frequency = MARK_HZ if bit else SPACE_HZ
        frames = np.arange(end - start, dtype=np.float64)
        output[start:end] = np.sin(phase + (2 * math.pi * frequency / sample_rate) * frames)
        phase = (phase + 2 * math.pi * frequency * len(frames) / sample_rate) % (2 * math.pi)
    return output


def _silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(round(seconds * sample_rate), dtype=np.float64)


def _encode_bit_string(bits: str, sample_rate: int) -> np.ndarray:
    normalized = bits.replace(" ", "").replace("\t", "").replace("\n", "")
    if not normalized:
        return np.zeros(0, dtype=np.float64)
    if not re.fullmatch(r"[01]+", normalized):
        raise ValueError("Custom ENDEC signature must contain only binary 0/1 digits")
    tone_map = {
        0: SPACE_HZ,
        1: MARK_HZ,
    }
    bit_values = [tone_map[int(bit)] for bit in normalized]
    frame_edges = np.rint(np.arange(len(bit_values) + 1) * sample_rate / BAUD).astype(int)
    output = np.empty(frame_edges[-1], dtype=np.float64)
    phase = 0.0
    for index, frequency in enumerate(bit_values):
        start, end = frame_edges[index], frame_edges[index + 1]
        frames = np.arange(end - start, dtype=np.float64)
        output[start:end] = np.sin(phase + (2 * math.pi * frequency / sample_rate) * frames)
        phase = (phase + 2 * math.pi * frequency * len(frames) / sample_rate) % (2 * math.pi)
    return output


def encode_same_audio(
    header: str,
    program_pcm: array,
    attention_seconds: float = 10.0,
    sample_rate: int = SAMPLE_RATE,
    program_boost_db: float = 0.0,
    custom_bits: str = "",
    custom_position: str = "suffix",
    program_sample_rate: int | None = None,
) -> array:
    if program_pcm.typecode != "h":
        raise TypeError("Program audio must be signed 16-bit PCM")
    if not 8 <= attention_seconds <= 25:
        raise ValueError("Attention duration must be between 8 and 25 seconds")
    if sample_rate < 8000:
        raise ValueError("Sample rate is too low for SAME FSK audio")
    if not 0 <= program_boost_db <= 30:
        raise ValueError("Program boost must be between 0 and 30 dB")
    custom_bits = custom_bits.strip()
    custom_position = custom_position.lower()
    if custom_bits and custom_position != "suffix":
        raise ValueError("Custom ENDEC signature is suffix-only; prefix is unsupported")
    if program_sample_rate is None:
        program_sample_rate = sample_rate
    if program_sample_rate != sample_rate:
        source_bytes = program_pcm.tobytes()
        resampled = resample_pcm16(source_bytes, program_sample_rate, sample_rate)
        program_pcm = array("h")
        program_pcm.frombytes(resampled)
    header = header.upper()
    if not header.startswith("ZCZC-") or not header.endswith("-"):
        raise ValueError("Header must begin with ZCZC- and end with a hyphen")

    pieces = [_silence(0.5, sample_rate)]
    preamble = b"\xab" * 16
    for _ in range(3):
        pieces.extend((
            _encode_data(preamble + header.encode("ascii"), sample_rate),
            _silence(1.0, sample_rate),
        ))
    tone = np.frombuffer(attention_tone(sample_rate, attention_seconds), dtype=np.int16).astype(np.float64) / 32767
    pieces.extend((tone, _silence(1.0, sample_rate)))
    program = np.frombuffer(program_pcm, dtype=np.int16).astype(np.float64) / 32768
    program *= 10 ** (program_boost_db / 20)
    pieces.append(program)
    if custom_bits:
        pieces.append(_encode_bit_string(custom_bits, sample_rate))
    eom = preamble + b"NNNN"
    for _ in range(3):
        pieces.extend((_encode_data(eom, sample_rate), _silence(1.0, sample_rate)))
    waveform = np.concatenate(pieces)
    waveform *= 10 ** (-3 / 20)
    return array("h", np.clip(waveform * 32767, -32768, 32767).astype(np.int16))