#!/usr/bin/env bash

meeting_data_root() {
  printf '%s\n' "${MEETING_DATA_DIR:-${HOME}/Transcriptions}"
}

meeting_latest_recording() {
  local meetings_root="$1"
  local latest_file=""
  local latest_mtime=0
  local candidate candidate_mtime
  [[ -d "$meetings_root" ]] || return 1
  while IFS= read -r -d '' candidate; do
    candidate_mtime="$(stat -f '%m' "$candidate")"
    if [[ "$candidate_mtime" -gt "$latest_mtime" ]]; then
      latest_mtime="$candidate_mtime"
      latest_file="$candidate"
    fi
  done < <(find "$meetings_root" -mindepth 2 -maxdepth 2 -type f \
    \( -name 'recording' -o -name 'recording.*' \) \
    ! -name 'recording.partial.*' -print0)
  [[ -n "$latest_file" ]] || return 1
  printf '%s\n' "$latest_file"
}

meeting_latest_transcript() {
  local meetings_root="$1"
  local latest_file=""
  local latest_mtime=0
  local candidate candidate_mtime
  [[ -d "$meetings_root" ]] || return 1
  while IFS= read -r -d '' candidate; do
    candidate_mtime="$(stat -f '%m' "$candidate")"
    if [[ "$candidate_mtime" -gt "$latest_mtime" ]]; then
      latest_mtime="$candidate_mtime"
      latest_file="$candidate"
    fi
  done < <(find "$meetings_root" -mindepth 4 -maxdepth 4 -type f \
    -path '*/transcripts/*/transcript.json' -print0)
  [[ -n "$latest_file" ]] || return 1
  printf '%s\n' "$latest_file"
}

meeting_config_root() {
  printf '%s\n' "${MEETING_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/meeting-transcriber}"
}

meeting_config_file() {
  printf '%s/config\n' "$(meeting_config_root)"
}

meeting_config_get() {
  local key="$1"
  local fallback="${2:-}"
  local config_file value
  config_file="$(meeting_config_file)"
  value=""
  if [[ -f "$config_file" ]]; then
    value="$(awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); found=$0} END {print found}' "$config_file")"
  fi
  printf '%s\n' "${value:-$fallback}"
}

meeting_notify() {
  local message="$1"
  local title="${2:-Meeting Transcriber}"
  [[ "${MEETING_NOTIFICATIONS:-1}" != "0" ]] || return 0
  if command -v osascript >/dev/null 2>&1; then
    osascript \
      -e 'on run argv' \
      -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
      -e 'end run' \
      -- "$message" "$title" >/dev/null 2>&1 || true
  fi
}
