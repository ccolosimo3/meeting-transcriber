#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
assume_yes=0

usage() {
  printf '%s\n' \
    'Usage: ./install.sh [--yes]' \
    '' \
    'Installs Homebrew dependencies, a locked Python environment, the meeting' \
    'command, local configuration, and the bundled Codex meeting skill.'
}

case "${1:-}" in
  --yes) assume_yes=1 ;;
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 2 ;;
esac

[[ "$(uname -s)" == "Darwin" ]] || {
  printf 'Error: this installer currently supports macOS only.\n' >&2
  exit 2
}
command -v brew >/dev/null 2>&1 || {
  printf 'Error: install Homebrew first: https://brew.sh\n' >&2
  exit 2
}

printf '%s\n' \
  'This will:' \
  '  - install or confirm Homebrew uv, pkgconf, and ffmpeg@7' \
  '  - install uv-managed Python 3.13' \
  '  - create a locked .venv in this checkout' \
  '  - link the meeting command under ~/.local/bin' \
  '  - configure AssemblyAI/local credentials, local data, and the Codex skill' \
  '' \
  'The normal path uploads recordings to AssemblyAI; --local keeps processing local.' \
  'Credentials are stored in macOS Keychain, never in this repository.'

if [[ "$assume_yes" -eq 0 ]]; then
  [[ -t 0 ]] || { printf 'Error: rerun interactively or pass --yes\n' >&2; exit 2; }
  printf '\nContinue? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) printf 'Cancelled.\n'; exit 0 ;;
  esac
fi

brew install uv pkgconf ffmpeg@7
ffmpeg_prefix="$(brew --prefix ffmpeg@7)"
uv python install 3.13
PKG_CONFIG_PATH="${ffmpeg_prefix}/lib/pkgconfig" \
  LDFLAGS="-L${ffmpeg_prefix}/lib" \
  CPPFLAGS="-I${ffmpeg_prefix}/include" \
  uv sync --frozen --python 3.13 --no-binary-package av --project "$project_root"

"${project_root}/bin/meeting-setup"

printf '\nInstallation complete. Run: meeting\n'
