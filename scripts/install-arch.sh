#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "This installer needs root privileges. Re-run with sudo:"
  echo "  sudo bash $0"
  exit 1
fi

pacman -Syu --noconfirm
pacman -S --noconfirm \
  python \
  python-pip \
  portaudio \
  libsndfile \
  ffmpeg \
  multimon-ng \
  espeak-ng \
  git \
  base-devel

python -m pip install --upgrade pip
python -m pip install -r "$(dirname "$0")/../requirements.txt"

echo
cat <<'EOF'
SAME ENDEC Arch setup is complete.

Next steps:
  1. Install dSAME3 separately: https://github.com/jamieden/dsame3
  2. Point the app at dsame.py in the Monitor tab.
  3. Run the app:
       python main.py

Note:
  This project does not control a transmitter. Use it for monitoring,
  testing, and local audio rebroadcast experiments only.
EOF
