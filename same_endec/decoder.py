from __future__ import annotations

from array import array
import fcntl
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

import sounddevice as sd

from .audio_io import resample_pcm16
from .core import PcmRingBuffer, SameAlert, parse_same_header
from .session import Dsame3Validator, DsameUnavailable


SAMPLE_RATE = 22050
BLOCK_FRAMES = 512
_EOM_BYTES = 20
_EOM_BIT_RATE = 520.833333
_PIPE_DELAY_SECONDS = 0.08


class AudioMonitorSession:
    def __init__(
        self,
        dsame_script: str,
        locations: tuple[str, ...],
        events: tuple[str, ...],
        source: str,
        source_value: str,
        on_log: Callable[[str], None],
        on_alert: Callable[[SameAlert, array], None],
        on_exit: Callable[[int], None],
        input_device: int | None = None,
        on_status: Callable[[str, bool], None] | None = None,
        local_callsign: str | None = None,
    ):
        self.source = source
        self.source_value = source_value
        self.input_device = input_device
        self.input_sample_rate = SAMPLE_RATE
        self.on_log = on_log
        self.on_alert = on_alert
        self.on_exit = on_exit
        self.on_status = on_status or (lambda _header, _recording: None)
        self.local_callsign = local_callsign.strip().casefold() if local_callsign else ""
        self.validator = Dsame3Validator(dsame_script, locations, events)
        self.locations = locations
        self.events = events
        self.audio_buffer = PcmRingBuffer(SAMPLE_RATE * 300)
        self.lock = threading.Lock()
        self.frames_sent = 0
        self.input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.multimon: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.input_stream = None
        self.threads: list[threading.Thread] = []
        self.pending_alert: SameAlert | None = None
        self.pending_header: str | None = None
        self.last_processed_header: str | None = None
        self.capture_start: int | None = None

    def _soundcard_input_ready(self, device: int | None) -> bool:
        if not hasattr(sd, "RawInputStream"):
            return False
        code = (
            "import sys\n"
            "import sounddevice as sd\n"
            "def cb(indata, frames, time_info, status):\n"
            "    pass\n"
            "try:\n"
            "    stream = sd.RawInputStream(samplerate=22050, channels=1, dtype='int16', blocksize=512, device=%r, callback=cb)\n"
            "    stream.start()\n"
            "    stream.stop()\n"
            "    stream.close()\n"
            "    print('OK')\n"
            "    sys.exit(0)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, exc)\n"
            "    sys.exit(1)\n"
        ) % device
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and "OK" in completed.stdout

    def _close_input_stream(self):
        stream = self.input_stream
        self.input_stream = None
        if stream is None:
            return
        try:
            if getattr(stream, "active", False):
                stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def start(self) -> None:
        script_path = Path(self.validator.script_path)
        if not script_path.is_file():
            raise FileNotFoundError(f"dSAME3 script not found: {script_path}")
        if self.source not in {"soundcard", "file", "url"}:
            raise ValueError(f"Unsupported audio source: {self.source}")
        if self.source in {"file", "url"} and not self.source_value:
            raise ValueError("Choose an audio file or enter a network URL")
        if self.source == "file" and not Path(self.source_value).is_file():
            raise FileNotFoundError(f"Audio file not found: {self.source_value}")
        try:
            self.multimon = subprocess.Popen(
                ["multimon-ng", "-a", "EAS", "-t", "raw", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
            if self.multimon.stdin is None or self.multimon.stdout is None:
                raise RuntimeError("Could not start multimon-ng pipes")
            try:
                fcntl.fcntl(self.multimon.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            except OSError:
                pass
            self.threads.append(threading.Thread(target=self._read_decoder, daemon=True))
            self.threads[-1].start()
            if self.source == "soundcard":
                device = self.input_device
                if device is None:
                    device = sd.default.device[0]
                if device is None or device < 0:
                    device = None
                try:
                    available = sd.query_devices()
                    if device is None and not any(
                        bool(info.get("max_input_channels", 0)) for info in available
                    ):
                        raise RuntimeError("No soundcard input device is available")
                    device_info = sd.query_devices(device, "input")
                    if not isinstance(device_info, dict) or "default_samplerate" not in device_info:
                        raise KeyError("default_samplerate")
                    self.input_sample_rate = int(device_info["default_samplerate"])
                except (KeyError, TypeError, ValueError, RuntimeError, sd.PortAudioError) as exc:
                    raise RuntimeError(
                        "Could not open the selected soundcard input device. "
                        "Check that an input device is connected and enabled, or switch to a WAV file or network URL source."
                    ) from exc
                if not self._soundcard_input_ready(device):
                    raise RuntimeError(
                        "Could not start soundcard input. The configured capture device is not usable with this ALSA/PulseAudio setup. "
                        "Choose a WAV file or network URL source instead, or fix the system audio backend."
                    )
                try:
                    stream = sd.RawInputStream(
                        samplerate=self.input_sample_rate,
                        channels=1,
                        dtype="int16",
                        blocksize=BLOCK_FRAMES,
                        device=device,
                        callback=self._audio_callback,
                    )
                    stream.start()
                    self.input_stream = stream
                except sd.PortAudioError as exc:
                    self._close_input_stream()
                    raise RuntimeError(
                        "Could not start soundcard input. Check that the capture device is not already in use or blocked by the system."
                    ) from exc
                self.threads.append(threading.Thread(target=self._feed_device, daemon=True))
                self.threads[-1].start()
                self.on_log(
                    f"Listening to input device {device if device is not None else 'system default'} "
                    f"at {self.input_sample_rate} Hz; resampling to {SAMPLE_RATE} Hz for decoding"
                )
            else:
                self.threads.append(threading.Thread(target=self._feed_ffmpeg, daemon=True))
                self.threads[-1].start()
        except Exception:
            self._close_input_stream()
            self.stop_event.set()
            try:
                if self.multimon is not None and self.multimon.poll() is None:
                    self.multimon.terminate()
            except Exception:
                pass
            raise

    def _audio_callback(self, indata, _frames, _time, status):
        if status:
            self.on_log(f"Audio input: {status}")
        try:
            pcm = resample_pcm16(bytes(indata), self.input_sample_rate, SAMPLE_RATE)
            self.input_queue.put_nowait(pcm)
        except queue.Full:
            self.on_log("Audio input overrun; some samples were dropped")

    def _feed_device(self):
        while not self.stop_event.is_set():
            try:
                pcm = self.input_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if pcm is None:
                break
            self._send_pcm(pcm)

    def _feed_ffmpeg(self):
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.source == "file":
            command.append("-re")
        command.extend([
            "-i", self.source_value, "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
        ])
        try:
            self.ffmpeg = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert self.ffmpeg.stdout is not None
            while not self.stop_event.is_set():
                pcm = self.ffmpeg.stdout.read(2048)
                if not pcm:
                    break
                self._send_pcm(pcm)
            code = self.ffmpeg.wait()
            if code and not self.stop_event.is_set():
                self.on_log(f"ffmpeg stopped with status {code}")
            if not self.stop_event.is_set() and self.multimon and self.multimon.stdin:
                self.multimon.stdin.close()
                self.on_log("Audio source ended; waiting for decoder output to drain")
        except Exception as error:
            self.on_log(f"Could not read audio source: {error}")

    def _send_pcm(self, pcm: bytes):
        if not pcm or self.multimon is None or self.multimon.stdin is None:
            return
        aligned = pcm[:len(pcm) - (len(pcm) % 2)]
        samples = array("h")
        samples.frombytes(aligned)
        with self.lock:
            self.audio_buffer.append(samples)
            self.frames_sent += len(aligned) // 2
        try:
            self.multimon.stdin.write(aligned)
        except (BrokenPipeError, OSError) as error:
            if not self.stop_event.is_set():
                self.on_log(f"Decoder input ended: {error}")
                self.stop()

    def _read_decoder(self):
        assert self.multimon is not None and self.multimon.stdout is not None
        for raw_line in iter(self.multimon.stdout.readline, b""):
            line = raw_line.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            self.on_log(f"multimon-ng: {line}")
            header_start = line.find("ZCZC-")
            if header_start >= 0:
                header = line[header_start:]
                alert = parse_same_header(header)
                if alert:
                    self._header_seen(header, alert)
                else:
                    self.on_log("Could not parse decoded SAME header")
            if "NNNN" in line and self.pending_alert and self.capture_start is not None:
                self._finish_capture()
        code = self.multimon.wait()
        if not self.stop_event.is_set():
            self.on_log(f"multimon-ng exited with status {code}")
            self.stop_event.set()
        self.on_exit(code)

    def _header_seen(self, header: str, alert: SameAlert):
        if self.local_callsign and alert.callsign.strip().casefold() == self.local_callsign:
            self.on_log(f"Echo canceled {alert.event}: station callsign matches {alert.callsign}")
            self.on_status(header, False)
            return
        if self.last_processed_header == header:
            self.on_log(f"Duplicate SAME header for {alert.event} already processed; ignoring repeat")
            return
        if self.pending_alert and self.capture_start is not None and self.pending_header == header:
            self.on_log(f"Duplicate SAME header for {alert.event} while capture is active; ignoring repeat")
            return
        if self.pending_alert and self.capture_start is not None:
            self.on_log("A new SAME header arrived before the previous EOM")
        self.pending_alert = alert
        self.pending_header = header
        header_copy_seconds = ((16 + len(header.encode("ascii"))) * 8 + 4) / _EOM_BIT_RATE
        remaining_header_set_seconds = header_copy_seconds + 2.0
        with self.lock:
            self.capture_start = max(
                self.audio_buffer.first_frame,
                self.frames_sent
                - round(SAMPLE_RATE * _PIPE_DELAY_SECONDS)
                + round(SAMPLE_RATE * remaining_header_set_seconds),
            )
        self.on_log(f"Three-copy SAME header set decoded for {alert.event}; program capture armed")
        self.on_status(header, True)

    def _finish_capture(self):
        with self.lock:
            eom_seconds = (_EOM_BYTES * 8 + 4) / _EOM_BIT_RATE + _PIPE_DELAY_SECONDS
            eom_frames = round(SAMPLE_RATE * eom_seconds)
            end_frame = self.frames_sent - eom_frames
            start_frame = self.capture_start
            alert = self.pending_alert
            header = self.pending_header
            self.capture_start = None
            self.pending_alert = None
            self.pending_header = None
            incomplete = start_frame is None or alert is None or header is None or end_frame <= start_frame
            capture_error = None
            captured = None
            if not incomplete:
                try:
                    captured = self.audio_buffer.extract(start_frame, end_frame)
                except ValueError as error:
                    capture_error = error
        if header is not None:
            self.on_status(header, False)
        if incomplete:
            self.on_log("Ignored alert with incomplete capture boundaries")
            return
        if capture_error is not None:
            self.on_log(f"Capture boundary fell outside the audio buffer: {capture_error}")
            return
        message = header.rstrip("-") + "-NNNN"
        try:
            validated = self.validator.validate(message)
        except DsameUnavailable as error:
            validated = parse_same_header(message)
            self.on_log(f"dSAME3 unavailable ({error}); using locally parsed SAME header")
        except Exception as error:
            self.on_log(f"dSAME3 validation failed: {error}")
            return
        if validated is None:
            self.on_log("dSAME3 rejected the decoded message or did not return metadata")
            return
        if validated.event not in self.events:
            self.on_log(f"Filtered {validated.event}")
            return
        if self.locations and not set(validated.locations).intersection(self.locations):
            self.on_log(f"Filtered {validated.event}: no selected location")
            return
        self.last_processed_header = header
        self.on_alert(validated, captured)

    def stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        with self.lock:
            header = self.pending_header
            self.pending_alert = None
            self.pending_header = None
            self.capture_start = None
        if header is not None:
            self.on_status(header, False)
        stream = self.input_stream
        self.input_stream = None
        if stream is not None:
            try:
                if getattr(stream, "active", False):
                    stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.input_queue.put_nowait(None)
        except queue.Full:
            pass
        if self.ffmpeg is not None and self.ffmpeg.poll() is None:
            self.ffmpeg.terminate()
        if self.multimon is not None:
            if self.multimon.stdin:
                try:
                    self.multimon.stdin.close()
                except OSError:
                    pass
            if self.multimon.poll() is None:
                self.multimon.terminate()