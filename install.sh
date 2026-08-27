#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
assume_yes=0

usage() {
  printf '%s\n' \
    'Usage: ./install.sh [--yes]' \
    '' \
    'Installs Homebrew dependencies, a small locked Python environment, the' \
    'meeting command, local configuration, and the bundled $meeting skill.'
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
  '  - install or confirm Homebrew uv and ffmpeg@7' \
  '  - install uv-managed Python 3.13' \
  '  - create a small locked .venv in this checkout' \
  '  - link the meeting command under ~/.local/bin' \
  '  - configure the microphone, AssemblyAI .env key, local data, and the $meeting skill' \
  '' \
  'Transcription uploads recordings to AssemblyAI and requires a paid AssemblyAI' \
  'account; the remote copies are deleted after local files are saved.' \
  'Recording always stays local. The credential is stored only in this checkout' \
  'root .env file, which remains untracked and private (mode 0600).'

if [[ "$assume_yes" -eq 0 ]]; then
  [[ -t 0 ]] || { printf 'Error: rerun interactively or pass --yes\n' >&2; exit 2; }
  printf '\nContinue? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) printf 'Cancelled.\n'; exit 0 ;;
  esac
fi

brew install uv ffmpeg@7
uv python install 3.13
uv sync --frozen --no-dev --python 3.13 --project "$project_root"

"${project_root}/bin/meeting-setup"

printf '\nInstallation complete. Run: meeting\n'
