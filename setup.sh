#!/usr/bin/env bash
# One-time setup: installs a toolchain if it is missing and builds the patched Luau 0.739
# interpreter (bin/luau, bin/luau-ast) that all engines of this repository run on.
# Safe to re-run.  Details and the reason for the patches: build_luau.py.
set -euo pipefail
cd "$(dirname "$0")"

need=""
for tool in git cmake python3; do
    command -v "$tool" >/dev/null 2>&1 || need="$need $tool"
done
if ! command -v g++ >/dev/null 2>&1 && ! command -v clang++ >/dev/null 2>&1; then
    need="$need c++"
fi

if [ -n "$need" ]; then
    echo "[setup] missing:$need -- trying to install a toolchain"
    SUDO=""
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -y
        $SUDO apt-get install -y git cmake g++ python3
    elif command -v apk >/dev/null 2>&1; then
        $SUDO apk add --no-cache git cmake g++ make python3 linux-headers
    elif command -v pkg >/dev/null 2>&1; then          # Termux
        pkg install -y git cmake clang make python
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y git cmake gcc-c++ python3
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm git cmake gcc python
    elif command -v brew >/dev/null 2>&1; then
        brew install git cmake python
    else
        echo "[setup] no known package manager: install git, cmake, python3 and a C++ compiler, then re-run" >&2
        exit 1
    fi
fi

python3 build_luau.py "$@"
