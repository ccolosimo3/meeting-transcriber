#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
real_home="$HOME"
original_path="$PATH"
artifact_root="${project_root}/tests/artifacts/$(date '+%Y%m%d-%H%M%S')-$$"
test_home="${artifact_root}/home"
data_root="${artifact_root}/data"
config_root="${artifact_root}/config"
mkdir -p "$test_home" "$data_root"

# Setup must repair loose permissions throughout existing meeting bundles.
preexisting_bundle="${data_root}/20260825-200000-preexisting"
umask 022
mkdir -p "$preexisting_bundle"
printf 'preexisting' > "${preexisting_bundle}/recording.wav"
umask 077

export HOME="$test_home"
export PATH="${test_home}/.local/bin:${PATH}"
export MEETING_DATA_DIR="$data_root"
export MEETING_CONFIG_DIR="$config_root"
export MEETING_NOTIFICATIONS=0
export MEETING_TRANSCRIBER_MLX_BIN="${project_root}/tests/fixtures/mlx-adapter-stub"
export MEETING_TRANSCRIBER_WHISPERX_BIN="${project_root}/tests/fixtures/whisperx-stub"
export MEETING_TRANSCRIBER_RENDER_HTML_BIN="${project_root}/tests/fixtures/render-html-stub"
export MEETING_TRANSCRIBER_RUN_ID=interface-test
export STUB_ARGV="${artifact_root}/stub-argv.txt"
export STUB_ENV="${artifact_root}/stub-env.txt"
export STUB_RENDER_ENV="${artifact_root}/render-env.txt"
export REAL_HTML_RENDERER="${project_root}/bin/render-transcript-html"
# Keep Homebrew's read-only discovery out of the isolated test home.
export HOMEBREW_CACHE="${real_home}/Library/Caches/Homebrew"

"${project_root}/bin/meeting" setup --device 0 --skip-token --skip-skill \
  > "${artifact_root}/setup.txt"
[[ "$(command -v meeting)" == "${test_home}/.local/bin/meeting" ]]
[[ "$(stat -f '%Lp' "$preexisting_bundle")" == "700" ]]
[[ "$(stat -f '%Lp' "${preexisting_bundle}/recording.wav")" == "600" ]]
touch -t 202608252000 "${preexisting_bundle}/recording.wav"

# A live link to another installation must be preserved but rejected.
conflict_home="${artifact_root}/conflict-home"
mkdir -p "${conflict_home}/.local/bin"
ln -s /usr/bin/true "${conflict_home}/.local/bin/meeting"
if HOME="$conflict_home" \
  PATH="${conflict_home}/.local/bin:${original_path}" \
  MEETING_DATA_DIR="${artifact_root}/conflict-data" \
  MEETING_CONFIG_DIR="${artifact_root}/conflict-config" \
  "${project_root}/bin/meeting" setup --device 0 --skip-token --skip-skill \
  > "${artifact_root}/conflict-setup.txt" 2>&1; then
  printf 'Error: setup accepted a meeting link to another installation\n' >&2
  exit 1
fi
[[ "$(realpath "${conflict_home}/.local/bin/meeting")" == "/usr/bin/true" ]]

older_bundle="${data_root}/20260825-210000-older"
latest_bundle="${data_root}/20260825-210100-newest-meeting.v1"
mkdir "$older_bundle" "$latest_bundle"
printf 'older' > "${older_bundle}/recording.wav"
latest_recording="${latest_bundle}/recording.wav"
printf 'newer' > "$latest_recording"
chmod 600 "${older_bundle}/recording.wav" "$latest_recording"
touch -t 202608252100 "${older_bundle}/recording.wav"
touch -t 202608252101 "$latest_recording"

# Deliberately loosen the caller default; the utility must still create private output.
touch "${artifact_root}/process.txt"
chmod 600 "${artifact_root}/process.txt"
umask 022
"${project_root}/bin/meeting" process --no-diarize --skip-names --no-open --no-copy \
  > "${artifact_root}/process.txt"

[[ "$(sed -n '1p' "$STUB_ARGV")" == "--input" ]]
[[ "$(sed -n '2p' "$STUB_ARGV")" == "$latest_recording" ]]
grep -Fxq -- '--model' "$STUB_ARGV"
grep -Fxq -- 'turbo' "$STUB_ARGV"
output_dir="${latest_bundle}/transcripts/interface-test"
output_stem="${output_dir}/transcript"
for extension in json txt srt vtt tsv html agent.md; do
  [[ -s "${output_stem}.${extension}" ]]
done
grep -Fq '[SPEAKER_00]: interface smoke' "${output_stem}.txt"
grep -Fq '[SPEAKER_00]: interface smoke' "${output_stem}.srt"
grep -Fq '[SPEAKER_00]: interface smoke' "${output_stem}.vtt"
grep -Fq 'SPEAKER_00' "${output_stem}.html"
grep -Fq 'SPEAKER_00' "${output_stem}.agent.md"
grep -Fxq absent "$STUB_RENDER_ENV"

# Reusing a transcript run id must fail before launching an engine or changing
# the existing evidence.
existing_json_sha="$(shasum -a 256 "${output_stem}.json" | awk '{print $1}')"
export STUB_ARGV="${artifact_root}/collision-argv.txt"
if "${project_root}/bin/meeting" transcribe --no-diarize "$latest_recording" \
  > "${artifact_root}/collision.txt" 2>&1; then
  printf 'Error: an existing transcript run was silently replaced\n' >&2
  exit 1
fi
[[ ! -e "$STUB_ARGV" ]]
[[ "$(shasum -a 256 "${output_stem}.json" | awk '{print $1}')" == "$existing_json_sha" ]]

# A diarization credential reaches only the transcription child, never argv or
# the later HTML-rendering child.
export MEETING_TRANSCRIBER_RUN_ID=interface-diarized
export STUB_ARGV="${artifact_root}/mlx-diarized-argv.txt"
HF_TOKEN=non-secret-sentinel "${project_root}/bin/meeting" transcribe --speakers 2 \
  "$latest_recording" \
  > "${artifact_root}/mlx-diarized.txt"
grep -Fxq present "$STUB_ENV"
grep -Fxq absent "$STUB_RENDER_ENV"
if grep -Fq 'non-secret-sentinel' "$STUB_ARGV" \
  || grep -R -Fq 'non-secret-sentinel' "${latest_bundle}/transcripts/interface-diarized"; then
  printf 'Error: diarization credential escaped into argv or transcript output\n' >&2
  exit 1
fi

# CPU Turbo remains an explicit fallback and retains the existing WhisperX path.
export MEETING_TRANSCRIBER_RUN_ID=interface-cpu
export STUB_ARGV="${artifact_root}/whisperx-argv.txt"
"${project_root}/bin/meeting" transcribe --backend whisperx --no-diarize \
  "$latest_recording" \
  > "${artifact_root}/cpu-fallback.txt"
[[ "$(sed -n '1p' "$STUB_ARGV")" == "$latest_recording" ]]
grep -Fxq -- '--model' "$STUB_ARGV"
grep -Fxq -- 'turbo' "$STUB_ARGV"
grep -Fxq -- '--device' "$STUB_ARGV"
grep -Fxq -- 'cpu' "$STUB_ARGV"
[[ -s "${latest_bundle}/transcripts/interface-cpu/transcript.json" ]]
cpu_output_dir="${latest_bundle}/transcripts/interface-cpu"
for extension in json txt srt vtt tsv html; do
  [[ -s "${cpu_output_dir}/transcript.${extension}" ]]
done
[[ "$(find "$cpu_output_dir" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "6" ]]

# A source already named transcript must not trigger a self-move in the CPU
# output normalizer.
cpu_self_input="${artifact_root}/transcript.wav"
cpu_self_output_root="${artifact_root}/cpu-self-output"
printf 'cpu self-move' > "$cpu_self_input"
export MEETING_TRANSCRIBER_RUN_ID=interface-cpu-self
export STUB_ARGV="${artifact_root}/whisperx-self-argv.txt"
"${project_root}/bin/meeting" transcribe --backend whisperx --no-diarize \
  --output-root "$cpu_self_output_root" "$cpu_self_input" \
  > "${artifact_root}/cpu-self.txt"
cpu_self_output_dir="${cpu_self_output_root}/interface-cpu-self"
for extension in json txt srt vtt tsv html; do
  [[ -s "${cpu_self_output_dir}/transcript.${extension}" ]]
done
[[ "$(find "$cpu_self_output_dir" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "6" ]]

# An alternate meeting root and an extensionless canonical recording must stay
# together rather than being re-imported into the configured default root.
alternate_meetings_root="${artifact_root}/alternate-meetings"
alternate_bundle="${alternate_meetings_root}/20260825-220000-extensionless"
mkdir -p "$alternate_bundle"
alternate_recording="${alternate_bundle}/recording"
printf 'extensionless' > "$alternate_recording"
chmod 600 "$alternate_recording"
default_bundle_count_before="$(find "$data_root" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
export MEETING_TRANSCRIBER_RUN_ID=interface-alternate-root
export STUB_ARGV="${artifact_root}/mlx-alternate-root-argv.txt"
"${project_root}/bin/meeting" process --meetings-root "$alternate_meetings_root" \
  --no-diarize --skip-names --no-open --no-copy \
  > "${artifact_root}/alternate-root.txt"
[[ -s "${alternate_bundle}/transcripts/interface-alternate-root/transcript.json" ]]
[[ "$(find "$data_root" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" == "$default_bundle_count_before" ]]

# An explicit external recording is imported into its own bundle without
# modifying the source, then receives a canonical transcript run.
external_parent="${artifact_root}/external-source"
mkdir "$external_parent"
external_recording="${external_parent}/recording.m4a"
printf 'external' > "$external_recording"
external_sha="$(shasum -a 256 "$external_recording" | awk '{print $1}')"
export MEETING_TRANSCRIBER_RUN_ID=interface-import
export MEETING_TRANSCRIBER_BUNDLE_ID=interface-import-bundle
export STUB_ARGV="${artifact_root}/mlx-import-argv.txt"
"${project_root}/bin/meeting" transcribe --no-diarize "$external_recording" \
  > "${artifact_root}/import.txt"
imported_recording="${data_root}/interface-import-bundle-recording/recording.m4a"
[[ -n "$imported_recording" ]]
[[ "$(shasum -a 256 "$imported_recording" | awk '{print $1}')" == "$external_sha" ]]
[[ "$(shasum -a 256 "$external_recording" | awk '{print $1}')" == "$external_sha" ]]
[[ -s "$(dirname "$imported_recording")/transcripts/interface-import/transcript.json" ]]
unset MEETING_TRANSCRIBER_BUNDLE_ID

# A canonical-looking symlink must be copied into a new private bundle instead
# of being treated as the bundle's owned recording.
symlink_meetings_root="${artifact_root}/symlink-meetings"
symlink_bundle="${symlink_meetings_root}/20260825-230000-symlink"
symlink_source="${artifact_root}/symlink-source.wav"
mkdir -p "$symlink_bundle"
printf 'symlink source' > "$symlink_source"
symlink_source_sha="$(shasum -a 256 "$symlink_source" | awk '{print $1}')"
ln -s "$symlink_source" "${symlink_bundle}/recording.wav"
export MEETING_TRANSCRIBER_RUN_ID=interface-symlink
export STUB_ARGV="${artifact_root}/mlx-symlink-argv.txt"
MEETING_DATA_DIR="$symlink_meetings_root" \
  MEETING_TRANSCRIBER_BUNDLE_ID=symlink-import \
  "${project_root}/bin/meeting" transcribe --no-diarize \
  "${symlink_bundle}/recording.wav" > "${artifact_root}/symlink-import.txt"
symlink_imported_recording="${symlink_meetings_root}/symlink-import-recording/recording.wav"
[[ -f "$symlink_imported_recording" && ! -L "$symlink_imported_recording" ]]
[[ "$(shasum -a 256 "$symlink_imported_recording" | awk '{print $1}')" == "$symlink_source_sha" ]]
[[ "$(shasum -a 256 "$symlink_source" | awk '{print $1}')" == "$symlink_source_sha" ]]
[[ ! -e "${symlink_bundle}/transcripts" ]]
[[ -s "${symlink_meetings_root}/symlink-import-recording/transcripts/interface-symlink/transcript.json" ]]

while IFS= read -r -d '' private_path; do
  mode="$(stat -f '%Lp' "$private_path")"
  if [[ -d "$private_path" ]]; then
    [[ "$mode" == "700" ]] || {
      printf 'Error: expected directory mode 700, got %s: %s\n' "$mode" "$private_path" >&2
      exit 1
    }
  else
    [[ "$mode" == "600" ]] || {
      printf 'Error: expected file mode 600, got %s: %s\n' "$mode" "$private_path" >&2
      exit 1
    }
  fi
done < <(find "$data_root" -print0)

printf 'Interface smoke: PASS\n'
printf 'Artifacts retained: %s\n' "$artifact_root"
