# SAME ENDEC

A Linux desktop application for monitoring decoded SAME alerts, filtering by event and location, encoding selected alert audio into a fresh SAME WAV, and creating manual RWT files with eSpeak NG. The app does not control or key a transmitter.

## Current Scope

- System soundcard, WAV-file, and FFmpeg-supported network URL inputs, demodulated by multimon-ng and validated by dSAME3.
- Selectable PortAudio capture and playback devices; capture is resampled to the decoder rate and playback is resampled to the selected output's native rate. The Monitor tab includes a one-second output test tone.
- Initial filter selection: TOR and FFW for `042003` (Allegheny County, Pennsylvania, subdivision 0). RMT and RWT can be enabled like other event codes.
- Outgoing SAME headers include the configured callsign as the station identifier, then a configurable simultaneous 853 + 960 Hz tone (default 10 seconds, adjustable from 8 to 25), the captured audio, and three NNNN end markers.
- The received audio segment is preserved. If its original attention tone was captured, the new attention tone is placed before it, as configured.
- WAV output, optional local playback, and an opt-in HTTP endpoint serving only `latest.wav` on port 8765.
- Manually generated custom RWT announcements using eSpeak NG; generated RWT WAVs are saved and played locally through the selected output device.

The receiver maintains a rolling PCM buffer, starts capture when multimon-ng recognizes the repeated header set, and excludes the first EOM preamble/NNNN sequence. dSAME3 and multimon-ng expose decoded messages rather than sample offsets, so capture edges are estimated from decoder output and pipe buffering; they are not sample-accurate and need validation against the actual receiver/device. If dSAME3 cannot import an optional Python dependency, the app reports that and locally parses/filters the header already decoded by multimon-ng rather than dropping the alert.

## Install Without A Virtual Environment

Use the system Python directly. Install the Python requirements into the system/user Python according to your Linux distribution, plus `multimon-ng` and `espeak-ng`. For example, where user-site pip installs are permitted:

```sh
python3 -m pip install --user -r requirements.txt
```

If you want distro-friendly setup shortcuts, use one of the bundled installers:

```sh
# Debian / Ubuntu / Mint
sudo bash scripts/install-debian.sh

# Arch / Manjaro
sudo bash scripts/install-arch.sh
```

Install [dSAME3](https://github.com/jamieden/dsame3) and its documented dependencies separately. This app asks dSAME3 to skip its automatic dependency installer. Install `multimon-ng`, `espeak-ng`, PortAudio, and `libsndfile` using your distribution's package manager.

See [DISCLAIMER.md](DISCLAIMER.md) for the project safety and legal warning.

## Run

```sh
/bin/python main.py
```

In the Monitor tab, choose the `dsame.py` script, enter your callsign, confirm the location/event filters, then start monitoring. The Manual RWT tab creates and plays a local WAV. The HTTP endpoint is opt-in and listens on all network interfaces; it serves only the latest output WAV.

Run the focused tests with:

```sh
/bin/python -m unittest discover -s tests -v
```

## Safety

This project is an experimental audio tool, not a certified EAS encoder/decoder. Verify output with suitable test equipment before any operational use. No transmitter controls are implemented. Do not inject alert audio into an on-air or public-safety system without authorization and applicable approvals.