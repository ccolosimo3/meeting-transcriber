#!/usr/bin/env bash
# Shared terminal formatting for the public meeting commands.
# Human status goes to stderr; stdout stays machine-readable.

ui_title() {
  printf '%s\n' 'Meeting Transcriber' >&2
}

ui_heading() {
  printf '\n%s\n' "$1" >&2
}

ui_row() {
  printf '  %-12s%s\n' "$1" "$2" >&2
}

ui_line() {
  printf '  %s\n' "$1" >&2
}

ui_next() {
  printf '  %-20s%s\n' "$1" "$2" >&2
}

ui_text() {
  printf '%s\n' "$1" >&2
}

ui_display_path() {
  local path="$1"
  case "$path" in
    "$HOME"/*) printf '~%s\n' "${path#"$HOME"}" ;;
    *) printf '%s\n' "$path" ;;
  esac
}
