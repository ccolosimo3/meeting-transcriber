#!/usr/bin/env bash

_meeting_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

meeting_data_root() {
  printf '%s\n' "${MEETING_DATA_DIR:-${HOME}/Transcriptions}"
}

meeting_python_bin() {
  printf '%s\n' "${_meeting_lib_dir}/../.venv/bin/python"
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

# Candidate transcript-run directories, newest first by the byte-ordered tuple
# (run directory basename, meeting-bundle basename, canonical JSON path).
_meeting_run_candidates() {
  local meetings_root="$1"
  local run_dir bundle_dir
  while IFS= read -r -d '' run_dir; do
    bundle_dir="$(dirname "$(dirname "$run_dir")")"
    printf '%s\t%s\t%s\t%s\n' \
      "$(basename "$run_dir")" \
      "$(basename "$bundle_dir")" \
      "${run_dir}/transcript.json" \
      "$run_dir"
  done < <(find "$meetings_root" -mindepth 3 -maxdepth 3 -type d \
    -path '*/transcripts/*' -print0 2>/dev/null) \
    | LC_ALL=C sort -r
}

# Print "state<TAB>transcript.json[<TAB>transcript_id]" for the newest usable
# run whose state matches $2 (empty matches any usable state). Validation is
# owned by the typed predicate in lib/transcript_run.py; partial, malformed,
# symlinked, and pre-publication runs are skipped.
_meeting_latest_run_matching() {
  local meetings_root="$1"
  local wanted_state="$2"
  local python_bin predicate line run_dir result state
  python_bin="$(meeting_python_bin)"
  predicate="${_meeting_lib_dir}/transcript_run.py"
  [[ -d "$meetings_root" && -x "$python_bin" && -f "$predicate" ]] || return 1
  while IFS= read -r line; do
    run_dir="${line##*$'\t'}"
    result="$("$python_bin" "$predicate" "$run_dir" 2>/dev/null)" || continue
    state="${result%%$'\t'*}"
    if [[ -n "$wanted_state" && "$state" != "$wanted_state" ]]; then
      continue
    fi
    printf '%s\n' "$result"
    return 0
  done < <(_meeting_run_candidates "$meetings_root")
  return 1
}

meeting_latest_run() {
  _meeting_latest_run_matching "$1" ''
}

meeting_latest_cleanup_required() {
  _meeting_latest_run_matching "$1" 'cleanup-required'
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
