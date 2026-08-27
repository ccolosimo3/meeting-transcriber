#!/usr/bin/env bash
set -euo pipefail
umask 077

source_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
real_home="$HOME"
original_path="$PATH"
artifact_root="${source_project_root}/tests/artifacts/$(date '+%Y%m%d-%H%M%S')-$$"
project_root="${artifact_root}/app"
test_home="${artifact_root}/home"
data_root="${artifact_root}/meeting data"
config_root="${artifact_root}/config"
mkdir -p "$test_home" "$data_root" "$project_root"
cp -R "${source_project_root}/bin" "${source_project_root}/lib" \
  "${source_project_root}/skills" "$project_root/"
mkdir -p "${project_root}/tests"
cp -R "${source_project_root}/tests/fixtures" "${project_root}/tests/"
ln -s "${source_project_root}/.venv" "${project_root}/.venv"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

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
export MEETING_TRANSCRIBER_ASSEMBLYAI_BIN="${project_root}/tests/fixtures/assemblyai-adapter-stub"
export MEETING_TRANSCRIBER_FFPROBE_BIN="${project_root}/tests/fixtures/ffprobe-duration-stub"
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/assemblyai-argv.txt"
export STUB_ASSEMBLYAI_ENV="${artifact_root}/assemblyai-env.txt"
open_stub="${artifact_root}/open-stub"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$@" > "$STUB_OPEN_ARGV"' > "$open_stub"
chmod +x "$open_stub"
export MEETING_TRANSCRIBER_OPEN_BIN="$open_stub"
export STUB_OPEN_ARGV="${artifact_root}/open-argv.txt"
# Keep Homebrew's read-only discovery out of the isolated test home.
export HOMEBREW_CACHE="${real_home}/Library/Caches/Homebrew"

"${project_root}/bin/meeting" setup --device 0 \
  > "${artifact_root}/setup.txt" 2>&1 || true
grep -Fq 'Configuration:' "${artifact_root}/setup.txt"
[[ "$(command -v meeting)" == "${test_home}/.local/bin/meeting" ]]
[[ "$(stat -f '%Lp' "$preexisting_bundle")" == "700" ]]
[[ "$(stat -f '%Lp' "${preexisting_bundle}/recording.wav")" == "600" ]]
if grep -Eq 'Hugging Face|hf-token|MLX|mlx|WhisperX|whisperx|pyannote' "${artifact_root}/setup.txt"; then
  fail 'setup still mentions the removed local transcription stack'
fi
touch -t 202608252000 "${preexisting_bundle}/recording.wav"

# Doctor: recording readiness passes while a missing key blocks transcription.
if env -u ASSEMBLYAI_API_KEY meeting doctor \
  > "${artifact_root}/doctor-missing-key.txt" 2>&1; then
  fail 'doctor passed without an AssemblyAI key'
fi
grep -Fq 'No AssemblyAI API key' "${artifact_root}/doctor-missing-key.txt"
grep -Fq 'Recording remains available' "${artifact_root}/doctor-missing-key.txt"
grep -Fq 'Recording is ready.' "${artifact_root}/doctor-missing-key.txt"
grep -Fq 'Transcription is not ready.' "${artifact_root}/doctor-missing-key.txt"
if grep -Eq 'Hugging Face|MLX|PyAV|TorchCodec|WhisperX|pyannote' "${artifact_root}/doctor-missing-key.txt"; then
  fail 'doctor still checks the removed local transcription stack'
fi
ASSEMBLYAI_API_KEY=environment-test-key \
  meeting doctor > "${artifact_root}/doctor-with-key.txt" 2>&1 \
  || fail 'doctor failed with an AssemblyAI key present'
grep -Fq 'AssemblyAI API key is available' "${artifact_root}/doctor-with-key.txt"

# A live link to another installation must be preserved but rejected.
conflict_home="${artifact_root}/conflict-home"
mkdir -p "${conflict_home}/.local/bin"
ln -s /usr/bin/true "${conflict_home}/.local/bin/meeting"
if HOME="$conflict_home" \
  PATH="${conflict_home}/.local/bin:${original_path}" \
  MEETING_DATA_DIR="${artifact_root}/conflict-data" \
  MEETING_CONFIG_DIR="${artifact_root}/conflict-config" \
  "${project_root}/bin/meeting" setup --device 0 \
  > "${artifact_root}/conflict-setup.txt" 2>&1; then
  fail 'setup accepted a meeting link to another installation'
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
latest_sha_before="$(shasum -a 256 "$latest_recording" | awk '{print $1}')"

# The public no-input command deterministically selects the newest recording,
# produces exactly the four-file bundle, and contains the credential — even
# when the caller's umask is deliberately loose.
umask 022
export MEETING_TRANSCRIBER_RUN_ID=20260826-121000
ASSEMBLYAI_API_KEY=non-secret-assemblyai-sentinel \
  meeting transcribe --skip-names --no-open \
  > "${artifact_root}/transcribe-stdout.txt" 2> "${artifact_root}/transcribe-stderr.txt"
umask 077

run_dir="${latest_bundle}/transcripts/20260826-121000"
json_path="${run_dir}/transcript.json"
[[ "$(cat "${artifact_root}/transcribe-stdout.txt")" == "$json_path" ]]
[[ "$(wc -l < "${artifact_root}/transcribe-stdout.txt" | tr -d ' ')" == "1" ]]
grep -Fq -- '--input' "$STUB_ASSEMBLYAI_ARGV"
grep -Fxq "$latest_recording" "$STUB_ASSEMBLYAI_ARGV"
grep -Fxq present "$STUB_ASSEMBLYAI_ENV"
if grep -Eq 'Transcribe this recording\?|Name the detected speakers' \
  "${artifact_root}/transcribe-stderr.txt"; then
  fail 'noninteractive transcribe prompted'
fi
[[ ! -e "$STUB_OPEN_ARGV" ]]
for artifact in transcript.json transcript.md transcript.html .assemblyai.json; do
  [[ -s "${run_dir}/${artifact}" ]] || fail "missing run artifact: ${artifact}"
done
[[ "$(find "$run_dir" -mindepth 1 | wc -l | tr -d ' ')" == "4" ]]
for legacy in transcript.txt transcript.srt transcript.vtt transcript.tsv \
  transcript.agent.md transcript.speakers.json transcript.assemblyai.json; do
  [[ ! -e "${run_dir}/${legacy}" ]] || fail "legacy output was generated: ${legacy}"
done
[[ "$(shasum -a 256 "$latest_recording" | awk '{print $1}')" == "$latest_sha_before" ]]
if grep -Fq 'non-secret-assemblyai-sentinel' "$STUB_ASSEMBLYAI_ARGV" \
  || grep -R -Fq 'non-secret-assemblyai-sentinel' "$run_dir" \
  || grep -Fq 'non-secret-assemblyai-sentinel' "${artifact_root}/transcribe-stdout.txt" \
  || grep -Fq 'non-secret-assemblyai-sentinel' "${artifact_root}/transcribe-stderr.txt"; then
  fail 'AssemblyAI credential escaped into argv, output, or terminal text'
fi
[[ "$(stat -f '%Lp' "$run_dir")" == "700" ]]

# Missing credential stops before preflight or the provider stub.
export MEETING_TRANSCRIBER_RUN_ID=20260826-121500
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/assemblyai-missing-key-argv.txt"
if env -u ASSEMBLYAI_API_KEY \
  meeting transcribe "$latest_recording" \
  > "${artifact_root}/missing-key.txt" 2>&1; then
  fail 'transcribe accepted a missing AssemblyAI key'
fi
[[ ! -e "$STUB_ASSEMBLYAI_ARGV" ]]
grep -Fq 'meeting setup' "${artifact_root}/missing-key.txt"
if grep -Fq -- '--local' "${artifact_root}/missing-key.txt"; then
  fail 'missing-key guidance still mentions removed local transcription'
fi

# Reusing a transcript run id must fail before launching the provider and
# without changing existing evidence.
export MEETING_TRANSCRIBER_RUN_ID=20260826-121000
existing_json_sha="$(shasum -a 256 "$json_path" | awk '{print $1}')"
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/collision-argv.txt"
if ASSEMBLYAI_API_KEY=x meeting transcribe "$latest_recording" \
  > "${artifact_root}/collision.txt" 2>&1; then
  fail 'an existing transcript run was silently replaced'
fi
[[ ! -e "$STUB_ASSEMBLYAI_ARGV" ]]
[[ "$(shasum -a 256 "$json_path" | awk '{print $1}')" == "$existing_json_sha" ]]

# An explicit external recording — in a path with spaces — is imported into its
# own private bundle without modifying the source.
external_parent="${artifact_root}/external source"
mkdir "$external_parent"
external_recording="${external_parent}/My Meeting.m4a"
printf 'external' > "$external_recording"
external_sha="$(shasum -a 256 "$external_recording" | awk '{print $1}')"
export MEETING_TRANSCRIBER_RUN_ID=20260826-122000
export MEETING_TRANSCRIBER_BUNDLE_ID=20260826-121900
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/assemblyai-import-argv.txt"
ASSEMBLYAI_API_KEY=x meeting transcribe --skip-names --no-open "$external_recording" \
  > "${artifact_root}/import-stdout.txt" 2> "${artifact_root}/import-stderr.txt"
imported_bundle="${data_root}/20260826-121900-My-Meeting"
imported_recording="${imported_bundle}/recording.m4a"
[[ -f "$imported_recording" ]]
[[ "$(shasum -a 256 "$imported_recording" | awk '{print $1}')" == "$external_sha" ]]
[[ "$(shasum -a 256 "$external_recording" | awk '{print $1}')" == "$external_sha" ]]
import_run_dir="${imported_bundle}/transcripts/20260826-122000"
[[ -s "${import_run_dir}/transcript.json" ]]
[[ "$(cat "${artifact_root}/import-stdout.txt")" == "${import_run_dir}/transcript.json" ]]

# A canonical-looking symlink must be copied into a new private bundle instead
# of being treated as the bundle's owned recording.
symlink_bundle="${data_root}/20260825-230000-symlink"
symlink_source="${artifact_root}/symlink-source.wav"
mkdir -p "$symlink_bundle"
printf 'symlink source' > "$symlink_source"
symlink_source_sha="$(shasum -a 256 "$symlink_source" | awk '{print $1}')"
ln -s "$symlink_source" "${symlink_bundle}/recording.wav"
export MEETING_TRANSCRIBER_RUN_ID=20260826-122500
export MEETING_TRANSCRIBER_BUNDLE_ID=20260826-122400
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/assemblyai-symlink-argv.txt"
ASSEMBLYAI_API_KEY=x meeting transcribe --skip-names --no-open \
  "${symlink_bundle}/recording.wav" > /dev/null 2>&1
symlink_imported="${data_root}/20260826-122400-recording/recording.wav"
[[ -f "$symlink_imported" && ! -L "$symlink_imported" ]]
[[ "$(shasum -a 256 "$symlink_imported" | awk '{print $1}')" == "$symlink_source_sha" ]]
[[ ! -e "${symlink_bundle}/transcripts" ]]
unset MEETING_TRANSCRIBER_BUNDLE_ID

# Latest discovery selects the newest usable run and opens the saved HTML.
meeting open > "${artifact_root}/open.txt" 2>&1
[[ "$(cat "$STUB_OPEN_ARGV")" == "${data_root}/20260826-122400-recording/transcripts/20260826-122500/transcript.html" ]]
if grep -Fq 'Remote cleanup required' "${artifact_root}/open.txt"; then
  fail 'a clean run was reported as cleanup-required'
fi

# Interactive speaker naming updates only the embedded names and atomically
# regenerates both views through the real Python seam.
name_json="${import_run_dir}/transcript.json"
receipt_sha_before="$(shasum -a 256 "${import_run_dir}/.assemblyai.json" | awk '{print $1}')"
printf 'Casey\n\n' | "${project_root}/.venv/bin/python" \
  "${project_root}/lib/set_speaker_names.py" "$name_json" \
  > /dev/null 2> "${artifact_root}/naming.txt"
grep -Fq '"SPEAKER_00": "Casey"' <("${project_root}/.venv/bin/python" -c \
  'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["speaker_names"], indent=1))' \
  "$name_json")
grep -Fq 'Casey (SPEAKER_00)' "${import_run_dir}/transcript.md"
grep -Fq 'Casey' "${import_run_dir}/transcript.html"
grep -Fq 'My Meeting' "${import_run_dir}/transcript.md" \
  || grep -Fq 'My meeting' "${import_run_dir}/transcript.md"
[[ "$(shasum -a 256 "${import_run_dir}/.assemblyai.json" | awk '{print $1}')" == "$receipt_sha_before" ]]
[[ ! -e "${import_run_dir}/transcript.speakers.json" ]]
[[ "$(find "$import_run_dir" -mindepth 1 | wc -l | tr -d ' ')" == "4" ]]

# A publication whose remote deletion is unconfirmed exits 3, prints the sole
# canonical path, stays selectable, and is flagged for cleanup — never retried.
export MEETING_TRANSCRIBER_RUN_ID=20260826-123000
export STUB_ASSEMBLYAI_ARGV="${artifact_root}/assemblyai-exit3-argv.txt"
set +e
STUB_ASSEMBLYAI_EXIT3=1 ASSEMBLYAI_API_KEY=x meeting transcribe \
  --skip-names --no-open "$latest_recording" \
  > "${artifact_root}/exit3-stdout.txt" 2> "${artifact_root}/exit3-stderr.txt"
exit3_status=$?
set -e
[[ "$exit3_status" == "3" ]] || fail "cleanup-required publication exited ${exit3_status}, not 3"
exit3_run="${latest_bundle}/transcripts/20260826-123000"
[[ "$(cat "${artifact_root}/exit3-stdout.txt")" == "${exit3_run}/transcript.json" ]]
[[ "$(wc -l < "${artifact_root}/exit3-stdout.txt" | tr -d ' ')" == "1" ]]
rm -f "$STUB_OPEN_ARGV"
meeting open > "${artifact_root}/open-cleanup.txt" 2>&1
[[ "$(cat "$STUB_OPEN_ARGV")" == "${exit3_run}/transcript.html" ]]
grep -Fq 'Remote cleanup required' "${artifact_root}/open-cleanup.txt"
grep -Fq "meeting cleanup ${exit3_run}/transcript.json" "${artifact_root}/open-cleanup.txt"
if grep -Fq 'Retry' "${artifact_root}/open-cleanup.txt"; then
  fail 'cleanup-required open output suggested a retry'
fi

# Every bundle path stays private even though the caller loosened its umask.
while IFS= read -r -d '' private_path; do
  mode="$(stat -f '%Lp' "$private_path")"
  if [[ -d "$private_path" ]]; then
    [[ "$mode" == "700" ]] || fail "expected directory mode 700, got ${mode}: ${private_path}"
  else
    [[ "$mode" == "600" ]] || fail "expected file mode 600, got ${mode}: ${private_path}"
  fi
done < <(find "$data_root" ! -type l -print0)

printf 'Interface smoke: PASS\n'
printf 'Artifacts retained: %s\n' "$artifact_root"
