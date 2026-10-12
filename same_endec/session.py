from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile

from .core import SameAlert


class DsameUnavailable(RuntimeError):
    pass


class Dsame3Validator:
    def __init__(self, script_path: str, locations: tuple[str, ...], events: tuple[str, ...]):
        self.script_path = str(Path(script_path).expanduser())
        self.locations = locations
        self.events = events

    def validate(self, message: str) -> SameAlert | None:
        with tempfile.TemporaryDirectory(prefix="same-endec-") as temporary_dir:
            metadata_path = Path(temporary_dir) / "alert.json"
            command = [
                sys.executable,
                self.script_path,
                "--msg", message,
                "--same", *self.locations,
                "--event", *self.events,
                "--json", str(metadata_path),
                "--no-text",
                "--skip_dependency",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            if result.returncode != 0:
                details = (result.stderr or result.stdout).strip()
                if "ModuleNotFoundError" in details or "ImportError" in details:
                    raise DsameUnavailable(details or "dSAME3 could not import a dependency")
                raise RuntimeError(details or f"dSAME3 exited with status {result.returncode}")
            if not metadata_path.is_file():
                raise RuntimeError("dSAME3 completed without writing its JSON alert metadata")
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                return SameAlert(
                    originator=str(data["ORG"]),
                    event=str(data["EEE"]).upper(),
                    locations=tuple(data["PSSCCC_list"]),
                    purge_time=str(data["TTTT"]),
                    julian_time=str(data["JJJHHMM"]),
                    callsign=str(data["LLLLLLLL"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"dSAME3 returned invalid alert metadata: {error}") from error