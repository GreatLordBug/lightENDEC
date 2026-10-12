#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "This installer needs root privileges. Re-run with sudo:"
  echo "  sudo bash $0"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  python3-dev \
  portaudio19-dev \
  libsndfile1 \
  libsndfile1-dev \
  ffmpeg \
  multimon-ng \
  espeak-ng \
  git \
  curl

if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  if [[ "$ID" == "ubuntu" || "$ID" == "debian" || "$ID" == "linuxmint" ]]; then
    echo
    echo "Installing Python dependencies from requirements.txt..."
    python3 -m pip install --upgrade pip
    python3 -m pip install -r "$(dirname "$0")/../requirements.txt"
  fi
fi

echo
cat <<'EOF'
SAME ENDEC Debian/Ubuntu setup is complete.

Next steps:
  1. Install dSAME3 separately: https://github.com/jamieden/dsame3
  2. Point the app at dsame.py in the Monitor tab.
  3. Run the app:
       python3 main.py

Note:
  This project does not control a transmitter. Use it for monitoring,
  testing, and local audio rebroadcast experiments only.
EOF
