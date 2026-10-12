from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
import re


_HEADER_RE = re.compile(
    r"ZCZC-([A-Z0-9]{3})-([A-Z0-9]{3})-([0-9]{6}(?:-[0-9]{6})*)"
    r"\+(\d{4})-(\d{7})-([A-Z0-9/-]{1,16})-?"
)


@dataclass(frozen=True)
class SameAlert:
    originator: str
    event: str
    locations: tuple[str, ...]
    purge_time: str
    julian_time: str
    callsign: str


def parse_same_header(raw: str) -> SameAlert | None:
    match = _HEADER_RE.search(raw.upper().replace(" ", ""))
    if match is None:
        return None
    originator, event, locations, purge_time, julian_time, callsign = match.groups()
    return SameAlert(
        originator=originator,
        event=event,
        locations=tuple(locations.split("-")),
        purge_time=purge_time,
        julian_time=julian_time,
        callsign=callsign.rstrip("-"),
    )


def safe_filename_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return component or "station"


@dataclass(frozen=True)
class RelayPolicy:
    enabled_events: frozenset[str] = frozenset({"TOR", "FFW"})
    enabled_locations: frozenset[str] = frozenset({"042003"})

    def accepts(self, alert: SameAlert) -> bool:
        if alert.event not in self.enabled_events:
            return False
        return not self.enabled_locations or bool(
            self.enabled_locations.intersection(alert.locations)
        )


def attention_tone(sample_rate: int = 22050, duration_seconds: float = 10.0) -> array:
    if sample_rate <= 0 or duration_seconds <= 0:
        raise ValueError("Sample rate and duration must be positive")
    frames = round(sample_rate * duration_seconds)
    samples = array("h")
    for frame in range(frames):
        time_seconds = frame / sample_rate
        mixed = 0.42 * (
            math.sin(2 * math.pi * 853 * time_seconds)
            + math.sin(2 * math.pi * 960 * time_seconds)
        )
        samples.append(round(max(-1.0, min(1.0, mixed)) * 32767))
    return samples


def prepend_attention_tone(captured_pcm: array, sample_rate: int, duration_seconds: float) -> array:
    if captured_pcm.typecode != "h":
        raise TypeError("Captured audio must be signed 16-bit PCM")
    return attention_tone(sample_rate, duration_seconds) + captured_pcm


class PcmRingBuffer:
    def __init__(self, capacity_frames: int):
        if capacity_frames <= 0:
            raise ValueError("Capacity must be positive")
        self.capacity_frames = capacity_frames
        self.first_frame = 0
        self.samples = array("h")

    @property
    def end_frame(self) -> int:
        return self.first_frame + len(self.samples)

    def append(self, pcm: array) -> None:
        if pcm.typecode != "h":
            raise TypeError("Audio chunks must be signed 16-bit PCM")
        self.samples.extend(pcm)
        overflow = len(self.samples) - self.capacity_frames
        if overflow > 0:
            del self.samples[:overflow]
            self.first_frame += overflow

    def extract(self, start_frame: int, end_frame: int) -> array:
        if start_frame < self.first_frame or end_frame > self.end_frame:
            raise ValueError("Requested audio is outside the buffered range")
        if end_frame < start_frame:
            raise ValueError("End frame precedes start frame")
        start = start_frame - self.first_frame
        end = end_frame - self.first_frame
        return self.samples[start:end]