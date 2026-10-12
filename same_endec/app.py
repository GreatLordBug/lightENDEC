from __future__ import annotations

from array import array
from datetime import datetime
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .audio_io import (
    LatestWavServer,
    input_devices,
    output_devices,
    play_test_tone,
    play_wav,
    read_pcm16,
    write_pcm16,
)
from .core import RelayPolicy, SameAlert, safe_filename_component
from .encoder import SAMPLE_RATE, build_same_header, encode_same_audio
from .decoder import AudioMonitorSession


EVENT_CODES = (
    "ADR AVA AVW BHW BWW BZW CAE CDW CEM CFA CFW CHW CWW DBA DBW DEW DMO DSW EAN EAT EQW EVA EVI EWW FCW FFA FFW FLA FFS FLW FRW FSW FZW HLS HMW HUA HUW HWA HWW IBW IFW LAE LEW LSW NAT NIC NMN NPT NST NUW POS RHW RMT RWT SMW SPS SSA SSW SVA SVR SVS TOA TOE TOR TRA TRW TSA TSW VOW WFA WFW WSA WSW SQW"
).split()


def _location_codes(value: str) -> tuple[str, ...]:
    codes = tuple(part.strip() for part in value.split(",") if part.strip())
    if any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("Enter six-digit SAME location codes, separated by commas")
    return codes


class SameEndecApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("SAME ENDEC")
        self.geometry("1160x820")
        self.minsize(960, 700)
        self.appearance_mode = "System"
        self.color_theme = "green"
        self.theme_var: tk.StringVar | None = None
        self.color_var: tk.StringVar | None = None
        ctk.set_appearance_mode(self.appearance_mode)
        self._apply_color(self.color_theme)

        self.session: AudioMonitorSession | None = None
        self.http_server: LatestWavServer | None = None
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.latest_header = ""
        self.event_vars: dict[str, tk.BooleanVar] = {}
        self.input_device_map = {"System default input": None}
        self.output_device_map = {"System default output": None}
        try:
            self.input_device_map.update(input_devices())
            self.output_device_map.update(output_devices())
        except Exception as error:
            self.ui_queue.put(("log", f"Could not enumerate audio devices: {error}"))
        self.output_dir = Path.home() / "SameENDEC"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = Path.home() / ".same_endec_settings.json"
        self._closing = False
        self._build_ui()
        self._load_settings()
        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, corner_radius=0, fg_color=("#e6f0eb", "#17251f"))
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="SAME ENDEC", font=ctk.CTkFont(size=23, weight="bold")).pack(
            side="left", padx=24, pady=16
        )
        self.status_label = ctk.CTkLabel(header, text="STOPPED", text_color=("#586d63", "#a9b9b0"))
        self.status_label.pack(side="right", padx=(0, 8))
        self.recording_indicator = ctk.CTkLabel(header, text="●", font=ctk.CTkFont(size=18, weight="bold"), text_color=("#d12727", "#ff5a5a"))
        self.recording_indicator.pack(side="right", padx=(0, 12))
        self.recording_indicator.configure(text="")
        self._build_theme_controls(header)

        tabs = ctk.CTkTabview(self, corner_radius=6)
        tabs.grid(row=1, column=0, sticky="nsew", padx=18, pady=16)
        monitor_tab = tabs.add("Monitor")
        test_tab = tabs.add("Manual RWT")
        log_tab = tabs.add("Activity")
        self._build_monitor_tab(monitor_tab)
        self._build_test_tab(test_tab)
        self.log_box = ctk.CTkTextbox(log_tab, wrap="word", font=ctk.CTkFont(family="monospace", size=12))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=12)
        self.log_box.configure(state="disabled")

    def _build_theme_controls(self, header):
        settings = ctk.CTkFrame(header, fg_color="transparent")
        settings.pack(side="right", padx=(0, 16), pady=12)
        self.theme_var = tk.StringVar(value=self.appearance_mode)
        self.theme_menu = ctk.CTkOptionMenu(
            settings,
            values=["Light", "Dark", "System"],
            variable=self.theme_var,
            command=self._apply_theme,
            width=90,
        )
        self.theme_menu.pack(side="top")
        self.color_var = tk.StringVar(value=self.color_theme)
        self.color_menu = ctk.CTkOptionMenu(
            settings,
            values=["green", "blue", "dark-blue", "gold"],
            variable=self.color_var,
            command=self._apply_color,
            width=90,
        )
        self.color_menu.pack(side="top", pady=(6, 0))

    def _apply_theme(self, value: str):
        ctk.set_appearance_mode(value)
        self.appearance_mode = value

    def _apply_color(self, value: str):
        valid_themes = {"green", "blue", "dark-blue", "gold"}
        if value not in valid_themes:
            return
        ctk.set_default_color_theme(value)
        self.color_theme = value
        if self.color_var is not None:
            self.color_var.set(value)

    def _build_monitor_tab(self, parent):
        parent.grid_columnconfigure((0, 1), weight=1, uniform="panels")
        parent.grid_rowconfigure(1, weight=1)
        controls = ctk.CTkFrame(parent)
        controls.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(8, 5), pady=8)
        controls.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(controls, text="Receiver", font=ctk.CTkFont(size=17, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(14, 8)
        )
        ctk.CTkLabel(controls, text="dSAME3 script").grid(row=1, column=0, sticky="w", padx=14, pady=6)
        self.dsame_entry = ctk.CTkEntry(controls, placeholder_text="/path/to/dsame.py")
        self.dsame_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=6)
        ctk.CTkButton(controls, text="Browse", width=74, command=self._browse_dsame).grid(
            row=1, column=2, padx=(4, 12), pady=6
        )
        ctk.CTkLabel(controls, text="Callsign").grid(row=2, column=0, sticky="w", padx=14, pady=6)
        self.callsign_entry = ctk.CTkEntry(controls, placeholder_text="your callsign, up to 8 characters")
        self.callsign_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(6, 12), pady=6)
        ctk.CTkLabel(controls, text="SAME locations").grid(row=3, column=0, sticky="w", padx=14, pady=6)
        self.location_entry = ctk.CTkEntry(controls)
        self.location_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(6, 12), pady=6)
        self.location_entry.insert(0, "042003")
        ctk.CTkLabel(controls, text="Output folder").grid(row=4, column=0, sticky="w", padx=14, pady=6)
        self.output_entry = ctk.CTkEntry(controls)
        self.output_entry.grid(row=4, column=1, sticky="ew", padx=6, pady=6)
        self.output_entry.insert(0, str(self.output_dir))
        ctk.CTkButton(controls, text="Browse", width=74, command=self._browse_output).grid(
            row=4, column=2, padx=(4, 12), pady=6
        )

        ctk.CTkLabel(controls, text="853 + 960 Hz", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=5, column=0, sticky="w", padx=14, pady=(14, 2)
        )
        self.tone_value = ctk.CTkLabel(controls, text="10 seconds")
        self.tone_value.grid(row=5, column=1, columnspan=2, sticky="e", padx=14, pady=(14, 2))
        self.tone_slider = ctk.CTkSlider(
            controls, from_=8, to=25, number_of_steps=17, command=self._tone_changed
        )
        self.tone_slider.set(10)
        self.tone_slider.grid(row=6, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 10))

        ctk.CTkLabel(controls, text="Output sample rate", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=7, column=0, sticky="w", padx=14, pady=(2, 2)
        )
        self.output_sample_rate_var = tk.StringVar(value="22050")
        self.output_sample_rate_menu = ctk.CTkOptionMenu(
            controls,
            values=["8000", "11025", "16000", "22050", "32000", "44100", "48000"],
            variable=self.output_sample_rate_var,
        )
        self.output_sample_rate_menu.grid(row=8, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 8))

        ctk.CTkLabel(controls, text="Custom ENDEC signature bits", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=9, column=0, sticky="w", padx=14, pady=(8, 2)
        )
        self.signature_entry = ctk.CTkEntry(controls, placeholder_text="10101100")
        self.signature_entry.grid(row=10, column=0, columnspan=3, sticky="ew", padx=14, pady=(2, 4))
        self.signature_position_var = tk.StringVar(value="Suffix")
        self.signature_position_menu = ctk.CTkOptionMenu(
            controls,
            values=["Suffix"],
            variable=self.signature_position_var,
        )
        self.signature_position_menu.grid(row=11, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 10))

        event_frame = ctk.CTkScrollableFrame(controls, fg_color="transparent", height=190)
        event_frame.grid(row=12, column=0, columnspan=3, sticky="nsew", padx=10, pady=(6, 12))
        event_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        ctk.CTkLabel(
            event_frame,
            text="Rebroadcast events",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 6)
        )
        ctk.CTkButton(
            event_frame, text="Select all", width=78, height=26,
            command=self._select_all_events,
        ).grid(row=0, column=3, sticky="e", padx=4, pady=(0, 6))
        for index, event in enumerate(EVENT_CODES):
            row, column = divmod(index, 4)
            variable = tk.BooleanVar(value=event in {"TOR", "FFW"})
            self.event_vars[event] = variable
            check = ctk.CTkCheckBox(event_frame, text=event, variable=variable, width=70)
            check.grid(row=row + 1, column=column, sticky="w", padx=4, pady=3)

        actions = ctk.CTkFrame(controls, fg_color="transparent")
        actions.grid(row=13, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 14))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.start_button = ctk.CTkButton(actions, text="Start monitor", command=self._start_monitor)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = ctk.CTkButton(actions, text="Stop", command=self._stop_monitor, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(5, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(right, text="Output", font=ctk.CTkFont(size=17, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 6)
        )
        self.play_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(right, text="Play each encoded alert locally", variable=self.play_var).grid(
            row=1, column=0, sticky="w", padx=12, pady=7
        )
        ctk.CTkLabel(right, text="Playback device").grid(row=2, column=0, sticky="w", padx=12, pady=(7, 2))
        self.output_device_var = tk.StringVar(value="System default output")
        self.output_device_menu = ctk.CTkOptionMenu(
            right, values=list(self.output_device_map), variable=self.output_device_var
        )
        self.output_device_menu.grid(row=3, column=0, sticky="ew", padx=12, pady=3)
        ctk.CTkButton(right, text="Test output", command=self._test_output).grid(
            row=4, column=0, sticky="ew", padx=12, pady=3
        )
        self.http_status = ctk.CTkLabel(right, text="HTTP output stopped", anchor="w", wraplength=370)
        self.http_status.grid(row=5, column=0, sticky="ew", padx=12, pady=(2, 8))
        ctk.CTkLabel(right, text="Audio source", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=6, column=0, sticky="w", padx=12, pady=(10, 6)
        )
        self.source_var = tk.StringVar(value="Soundcard")
        self.source_menu = ctk.CTkOptionMenu(
            right, values=["Soundcard", "WAV file", "Network URL"], variable=self.source_var,
            command=self._source_changed
        )
        self.source_menu.grid(row=7, column=0, sticky="ew", padx=12, pady=4)
        source_path_row = ctk.CTkFrame(right, fg_color="transparent")
        source_path_row.grid(row=8, column=0, sticky="ew", padx=12, pady=4)
        source_path_row.grid_columnconfigure(0, weight=1)
        self.source_path_entry = ctk.CTkEntry(source_path_row, placeholder_text="Input file or stream URL")
        self.source_path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.source_browse_button = ctk.CTkButton(
            source_path_row, text="Browse", width=74, command=self._browse_source
        )
        self.source_browse_button.grid(row=0, column=1)
        ctk.CTkLabel(right, text="Capture device").grid(row=9, column=0, sticky="w", padx=12, pady=(5, 2))
        self.input_device_var = tk.StringVar(value="System default input")
        self.input_device_menu = ctk.CTkOptionMenu(
            right, values=list(self.input_device_map), variable=self.input_device_var
        )
        self.input_device_menu.grid(row=10, column=0, sticky="ew", padx=12, pady=3)
        ctk.CTkLabel(right, text="Live monitor", font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=11, column=0, sticky="w", padx=12, pady=(8, 4)
        )
        self.monitor_status = ctk.CTkLabel(
            right,
            text="Latest SAME header: none\nRecording: no",
            justify="left",
            anchor="nw",
            wraplength=390,
        )
        self.monitor_status.grid(row=12, column=0, sticky="new", padx=12, pady=4)
        ctk.CTkButton(right, text="Start / stop HTTP output", command=self._toggle_http).grid(
            row=13, column=0, sticky="ew", padx=12, pady=(7, 4)
        )
        ctk.CTkButton(right, text="Save settings", command=self._save_settings).grid(
            row=14, column=0, sticky="ew", padx=12, pady=(7, 4)
        )
        self._source_changed("Soundcard")

    def _build_test_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="Manual Required Weekly Test", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 8)
        )
        self.rwt_text = ctk.CTkTextbox(parent, height=230, wrap="word")
        self.rwt_text.grid(row=1, column=0, sticky="nsew", padx=18, pady=8)
        self.rwt_text.insert("1.0", "This is a required weekly test of the emergency alert system.")
        controls = ctk.CTkFrame(parent, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=18, pady=12)
        controls.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(controls, text="Speech volume boost").grid(row=0, column=0, sticky="w")
        self.voice_boost_value = ctk.CTkLabel(controls, text="0 dB")
        self.voice_boost_value.grid(row=0, column=1, sticky="e")
        self.voice_boost_slider = ctk.CTkSlider(
            controls, from_=0, to=20, number_of_steps=20, command=self._voice_boost_changed
        )
        self.voice_boost_slider.set(0)
        self.voice_boost_slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        ctk.CTkButton(controls, text="Generate and save RWT WAV", command=self._generate_rwt).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )

    def _browse_dsame(self):
        chosen = filedialog.askopenfilename(filetypes=[("Python script", "*.py"), ("All files", "*")])
        if chosen:
            self.dsame_entry.delete(0, "end")
            self.dsame_entry.insert(0, chosen)

    def _browse_output(self):
        chosen = filedialog.askdirectory(initialdir=self.output_entry.get())
        if chosen:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, chosen)

    def _browse_source(self):
        chosen = filedialog.askopenfilename(filetypes=[("Audio files", "*.wav *.mp3 *.flac *.ogg"), ("All files", "*")])
        if chosen:
            self.source_path_entry.delete(0, "end")
            self.source_path_entry.insert(0, chosen)

    def _source_changed(self, value):
        enabled = value != "Soundcard"
        self.source_path_entry.configure(state="normal" if enabled else "disabled")
        self.source_browse_button.configure(state="normal" if value == "WAV file" else "disabled")
        self.input_device_menu.configure(state="normal" if value == "Soundcard" else "disabled")

    def _selected_device(self, device_map: dict[str, int | None], variable: tk.StringVar) -> int | None:
        return device_map.get(variable.get())

    def _test_output(self):
        device = self._selected_device(self.output_device_map, self.output_device_var)
        device_name = self.output_device_var.get()

        def play_and_report():
            try:
                play_test_tone(device)
                self.ui_queue.put(("log", f"Output test completed on {device_name}"))
            except Exception as error:
                self.ui_queue.put(("log", f"Audio output failed: {error}"))

        threading.Thread(target=play_and_report, daemon=True).start()

    def _tone_changed(self, value):
        self.tone_value.configure(text=f"{round(float(value))} seconds")

    def _voice_boost_changed(self, value):
        self.voice_boost_value.configure(text=f"{round(float(value))} dB")

    def _select_all_events(self):
        for variable in self.event_vars.values():
            variable.set(True)

    def _settings(self, require_events: bool = True):
        locations = _location_codes(self.location_entry.get())
        callsign = self.callsign_entry.get().strip().upper()
        if not callsign:
            raise ValueError("Enter your callsign before starting")
        selected = tuple(
            event for event, variable in self.event_vars.items()
            if variable.get()
        )
        if require_events and not selected:
            raise ValueError("Select at least one event to rebroadcast")
        return locations, callsign, selected

    def _save_settings(self):
        settings = {
            "dsame": self.dsame_entry.get().strip(),
            "callsign": self.callsign_entry.get().strip(),
            "locations": self.location_entry.get().strip(),
            "output_dir": self.output_entry.get().strip(),
            "tone_seconds": float(self.tone_slider.get()),
            "output_sample_rate": self.output_sample_rate_var.get(),
            "custom_bits": self.signature_entry.get().strip(),
            "custom_position": self.signature_position_var.get(),
            "play_on_decode": bool(self.play_var.get()),
            "source": self.source_var.get(),
            "source_path": self.source_path_entry.get().strip(),
            "output_device": self.output_device_var.get(),
            "input_device": self.input_device_var.get(),
            "appearance_mode": self.appearance_mode,
            "color_theme": self.color_theme,
            "events": {event: bool(variable.get()) for event, variable in self.event_vars.items()},
        }
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        if self.winfo_exists() and not self._closing:
            self._log(f"Saved settings to {self.settings_path}")
            try:
                messagebox.showinfo("Settings saved", f"Saved app settings to {self.settings_path}")
            except tk.TclError:
                pass

    def _load_settings(self):
        if not self.settings_path.exists():
            return
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(settings, dict):
            return
        for field_name, value in (
            ("dsame", self.dsame_entry),
            ("callsign", self.callsign_entry),
            ("locations", self.location_entry),
            ("output_dir", self.output_entry),
            ("source_path", self.source_path_entry),
        ):
            text = settings.get(field_name)
            if isinstance(text, str) and text:
                value.delete(0, "end")
                value.insert(0, text)
        tone_seconds = settings.get("tone_seconds")
        if isinstance(tone_seconds, (int, float)):
            self.tone_slider.set(float(tone_seconds))
        output_rate = settings.get("output_sample_rate")
        if isinstance(output_rate, str):
            self.output_sample_rate_var.set(output_rate)
        custom_bits = settings.get("custom_bits")
        if isinstance(custom_bits, str):
            self.signature_entry.delete(0, "end")
            self.signature_entry.insert(0, custom_bits)
        custom_position = settings.get("custom_position")
        if isinstance(custom_position, str):
            self.signature_position_var.set(custom_position)
        play_value = settings.get("play_on_decode")
        if isinstance(play_value, bool):
            self.play_var.set(play_value)
        source_value = settings.get("source")
        if isinstance(source_value, str):
            self.source_var.set(source_value)
        appearance_mode = settings.get("appearance_mode")
        if isinstance(appearance_mode, str):
            self.appearance_mode = appearance_mode
            if self.theme_var is not None:
                self.theme_var.set(appearance_mode)
            self._apply_theme(appearance_mode)
        color_theme = settings.get("color_theme")
        if isinstance(color_theme, str) and color_theme in {"green", "blue", "dark-blue", "gold"}:
            self.color_theme = color_theme
            if self.color_var is not None:
                self.color_var.set(color_theme)
            self._apply_color(color_theme)
        output_device = settings.get("output_device")
        if isinstance(output_device, str) and output_device in self.output_device_map:
            self.output_device_var.set(output_device)
        input_device = settings.get("input_device")
        if isinstance(input_device, str) and input_device in self.input_device_map:
            self.input_device_var.set(input_device)
        events = settings.get("events")
        if isinstance(events, dict):
            for event, variable in self.event_vars.items():
                enabled = events.get(event, variable.get())
                if isinstance(enabled, bool):
                    variable.set(enabled)
        if self.source_var.get() in {"Soundcard", "WAV file", "Network URL"}:
            self._source_changed(self.source_var.get())

    def _start_monitor(self):
        try:
            locations, callsign, events = self._settings()
            output_dir = Path(self.output_entry.get()).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir = output_dir
            source_name = self.source_var.get()
            source_mode = {"Soundcard": "soundcard", "WAV file": "file", "Network URL": "url"}[source_name]
            self.session = AudioMonitorSession(
                self.dsame_entry.get().strip(), locations, events, source_mode,
                self.source_path_entry.get().strip(),
                lambda text: self.ui_queue.put(("log", text)),
                lambda alert, pcm: self.ui_queue.put(("alert", (alert, pcm))),
                lambda code: self.ui_queue.put(("exit", code)),
                input_device=self._selected_device(self.input_device_map, self.input_device_var),
                on_status=lambda header, recording: self.ui_queue.put(("decode_status", (header, recording))),
                local_callsign=callsign,
            )
            self._set_decoder_status("", False)
            self.session.start()
        except Exception as error:
            messagebox.showerror("Cannot start monitor", str(error))
            return
        self.status_label.configure(text="MONITORING", text_color=("#207451", "#75d5a8"))
        self.monitor_status.configure(text=f"Listening to {source_name}. Captured edges are inferred from decoded SAME markers.")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._log(f"dSAME3/multimon-ng monitor started: {source_name}")

    def _stop_monitor(self):
        if self.session:
            self.session.stop()
        self.status_label.configure(text="STOPPING")
        self.stop_button.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                kind, value = self.ui_queue.get_nowait()
                if kind == "log":
                    self._log(str(value))
                elif kind == "decode_status":
                    header, recording = value
                    self._set_decoder_status(str(header), bool(recording))
                elif kind == "alert":
                    alert, pcm = value
                    self._process_alert(alert, pcm)
                elif kind == "exit":
                    self._set_decoder_status(self.latest_header, False)
                    self.status_label.configure(text="STOPPED", text_color=("#586d63", "#a9b9b0"))
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if int(value) != 0:
                        self._log(f"Decoder exited with status {value}")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _set_decoder_status(self, header: str, recording: bool):
        if header:
            self.latest_header = header
        self.recording_indicator.configure(text="●" if recording else "")
        self.monitor_status.configure(
            text=f"Latest SAME header: {self.latest_header or 'none'}\nRecording: {'YES' if recording else 'no'}"
        )

    def _process_alert(self, alert: SameAlert, program: array):
        try:
            event = alert.event
            callsign = self.callsign_entry.get().strip().upper()
            if alert.callsign.strip().casefold() == callsign.casefold():
                self._log(f"Echo canceled {event}: station callsign matches {callsign}")
                return
            policy = RelayPolicy(
                frozenset(key for key, variable in self.event_vars.items() if variable.get()),
                frozenset(_location_codes(self.location_entry.get())),
            )
            if not policy.accepts(alert):
                self._log(f"Filtered {event}: no selected event/location match")
                return
            locations = alert.locations or _location_codes(self.location_entry.get())
            hours, minutes = int(alert.purge_time[:2]), int(alert.purge_time[2:])
            purge_minutes = min(5999, hours * 60 + minutes)
            output_sample_rate_var = getattr(self, "output_sample_rate_var", None)
            signature_entry = getattr(self, "signature_entry", None)
            signature_position_var = getattr(self, "signature_position_var", None)
            sample_rate = int(output_sample_rate_var.get() if output_sample_rate_var is not None else str(SAMPLE_RATE))
            custom_bits = signature_entry.get().strip() if signature_entry is not None else ""
            custom_position = "suffix"
            if signature_position_var is not None:
                custom_position = signature_position_var.get().lower()
            header = build_same_header(event, locations, callsign, purge_minutes)
            encoded = encode_same_audio(
                header,
                program,
                self.tone_slider.get(),
                sample_rate,
                custom_bits=custom_bits,
                custom_position=custom_position,
                program_sample_rate=SAMPLE_RATE,
            )
            name = f"{datetime.now():%Y%m%d-%H%M%S}_{event}_{safe_filename_component(callsign)}.wav"
            destination = self.output_dir / name
            write_pcm16(destination, encoded.tobytes(), sample_rate)
            shutil.copy2(destination, self.output_dir / "latest.wav")
            self._log(f"Rebroadcast audio saved: {destination}")
            if self.play_var.get():
                self._play_wav_async(destination, delay_seconds=5)
        except Exception as error:
            self._log(f"Relay failed for {alert.event}: {error}")

    def _play_wav_async(self, path: Path, label: str = "Playback", delay_seconds: float = 0):
        device = self._selected_device(self.output_device_map, self.output_device_var)
        device_name = self.output_device_var.get()

        def play_and_report():
            try:
                if delay_seconds:
                    time.sleep(delay_seconds)
                play_wav(path, device=device)
                self.ui_queue.put(("log", f"{label} completed on {device_name}"))
            except Exception as error:
                self.ui_queue.put(("log", f"Audio output failed: {error}"))

        threading.Thread(target=play_and_report, daemon=True).start()

    def _generate_rwt(self):
        try:
            locations, callsign, _events = self._settings(require_events=False)
            speech = self.rwt_text.get("1.0", "end").strip()
            if not speech:
                raise ValueError("Enter the RWT announcement text")
            output_dir = Path(self.output_entry.get()).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)
            output_sample_rate_var = getattr(self, "output_sample_rate_var", None)
            signature_entry = getattr(self, "signature_entry", None)
            signature_position_var = getattr(self, "signature_position_var", None)
            output_sample_rate = int(output_sample_rate_var.get() if output_sample_rate_var is not None else str(SAMPLE_RATE))
            custom_bits = signature_entry.get().strip() if signature_entry is not None else ""
            custom_position = "suffix"
            if signature_position_var is not None:
                custom_position = signature_position_var.get().lower()
            voice_path = output_dir / "rwt-voice.wav"
            subprocess.run(["espeak-ng", "-w", str(voice_path), speech], check=True)
            voice_bytes, _voice_sample_rate = read_pcm16(voice_path, output_sample_rate)
            voice = array("h")
            voice.frombytes(voice_bytes)
            header = build_same_header("RWT", locations, callsign, purge_minutes=30)
            encoded = encode_same_audio(
                header,
                voice,
                self.tone_slider.get(),
                output_sample_rate,
                program_boost_db=self.voice_boost_slider.get(),
                custom_bits=custom_bits,
                custom_position=custom_position,
                program_sample_rate=output_sample_rate,
            )
            destination = output_dir / (
                f"{datetime.now():%Y%m%d-%H%M%S}_RWT_{safe_filename_component(callsign)}.wav"
            )
            write_pcm16(destination, encoded.tobytes(), output_sample_rate)
            shutil.copy2(destination, output_dir / "latest.wav")
            self._log(f"Manual RWT WAV generated: {destination}")
            self._play_wav_async(destination, "Custom RWT playback")
            messagebox.showinfo("RWT ready", f"Saved {destination}\nNo transmitter is controlled by this app.")
        except FileNotFoundError as error:
            messagebox.showerror("Missing dependency", f"Could not find eSpeak NG or a required file.\n{error}")
        except Exception as error:
            messagebox.showerror("Cannot generate RWT", str(error))

    def _toggle_http(self):
        if self.http_server:
            self.http_server.stop()
            self.http_server = None
            self.http_status.configure(text="HTTP output stopped")
            return
        try:
            output = Path(self.output_entry.get()).expanduser() / "latest.wav"
            self.http_server = LatestWavServer(output)
            address = self.http_server.start()
            self.http_status.configure(text=f"Serving latest WAV at {address}")
            self._log(f"HTTP WAV endpoint started on port 8765")
        except OSError as error:
            self.http_server = None
            messagebox.showerror("Cannot start HTTP output", str(error))

    def _log(self, text: str):
        if not self.winfo_exists() or not hasattr(self, "log_box"):
            return
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except tk.TclError:
            pass

    def _close(self):
        self._closing = True
        self._save_settings()
        if self.session:
            self.session.stop()
        if self.http_server:
            self.http_server.stop()
        self.destroy()


def main():
    app = SameEndecApp()
    app.mainloop()