from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

from .core import attention_tone


def output_devices() -> list[tuple[str, int]]:
    devices = sd.query_devices()
    return [
        (f"{index}: {device['name']}", index)
        for index, device in enumerate(devices)
        if device["max_output_channels"] > 0
    ]


def input_devices() -> list[tuple[str, int]]:
    devices = sd.query_devices()
    return [
        (f"{index}: {device['name']}", index)
        for index, device in enumerate(devices)
        if device["max_input_channels"] > 0
    ]


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    samples = np.frombuffer(pcm, dtype=np.int16)
    if source_rate == target_rate or samples.size == 0:
        return pcm
    target_length = round(samples.size * target_rate / source_rate)
    if source_rate == target_rate * 2 and samples.size % 2 == 0:
        pairs = samples.astype(np.int32).reshape(-1, 2)
        return (pairs.sum(axis=1) // 2).astype(np.int16).tobytes()
    source_axis = np.arange(samples.size, dtype=np.float64)
    target_axis = np.linspace(0, samples.size - 1, target_length)
    return np.interp(target_axis, source_axis, samples).astype(np.int16).tobytes()


def _native_output_rate(device: int | None) -> int | None:
    selected = device
    if selected is None:
        selected = sd.default.device[1]
    if selected is None or selected < 0:
        return None
    return int(sd.query_devices(selected, "output")["default_samplerate"])


def read_pcm16(path: str | Path, sample_rate: int = 22050) -> tuple[bytes, int]:
    samples, source_rate = sf.read(str(path), dtype="int16", always_2d=True)
    mono = samples.mean(axis=1).astype(np.int16)
    if source_rate != sample_rate:
        target_length = round(len(mono) * sample_rate / source_rate)
        old_axis = np.arange(len(mono), dtype=np.float64)
        new_axis = np.linspace(0, max(0, len(mono) - 1), target_length)
        mono = np.interp(new_axis, old_axis, mono).astype(np.int16)
    return mono.tobytes(), sample_rate


def write_pcm16(path: str | Path, pcm: bytes, sample_rate: int = 22050) -> None:
    samples = np.frombuffer(pcm, dtype=np.int16)
    sf.write(str(path), samples, sample_rate, subtype="PCM_16", format="WAV")


def play_wav(path: str | Path, device: int | None = None) -> None:
    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    target_rate = _native_output_rate(device) or sample_rate
    if target_rate != sample_rate:
        target_length = round(len(samples) * target_rate / sample_rate)
        source_axis = np.arange(len(samples), dtype=np.float64)
        target_axis = np.linspace(0, max(0, len(samples) - 1), target_length)
        samples = np.column_stack([
            np.interp(target_axis, source_axis, samples[:, channel])
            for channel in range(samples.shape[1])
        ]).astype(np.float32)
    sd.play(samples, target_rate, device=device)
    sd.wait()


def play_test_tone(device: int | None = None) -> None:
    tone = np.frombuffer(attention_tone(22050, 1.0), dtype=np.int16).astype(np.float32) / 32768
    source_rate = 22050
    target_rate = _native_output_rate(device) or source_rate
    if target_rate != source_rate:
        target_length = round(len(tone) * target_rate / source_rate)
        tone = np.interp(
            np.linspace(0, len(tone) - 1, target_length),
            np.arange(len(tone), dtype=np.float64),
            tone,
        ).astype(np.float32)
    sd.play(tone, target_rate, device=device)
    sd.wait()


class LatestWavServer:
    def __init__(self, output_path: Path, port: int = 8765):
        self.output_path = output_path
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        output_path = self.output_path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/latest.wav" or not output_path.is_file():
                    self.send_error(404)
                    return
                data = output_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://<this-machine-ip>:{self.port}/latest.wav"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
