//! The picker grants one directory. All descendants are opened one component
//! at a time against that directory's handle, without following links.
use std::{
    collections::HashMap,
    ffi::{OsStr, OsString},
    fs::File,
    io::{self, BufReader},
    path::{Component, Path, PathBuf},
};

use cap_fs_ext::{DirExt, FollowSymlinks, MetadataExt as _, OpenOptionsFollowExt};
use cap_std::{
    ambient_authority,
    fs::{Dir, Metadata, OpenOptions},
};
use chrono::NaiveDateTime;
use serde::Serialize;
use serde_json::Value;

use crate::{media::token, transcript::ReaderTranscript};

const CANONICAL: &str = "transcript.json";
const MARKDOWN: &str = "transcript.md";
const HTML: &str = "transcript.html";
const MANAGED: &str = ".assemblyai.json";
const HISTORICAL: &str = "transcript.assemblyai.json";
const INCOMPLETE: &str = "Transcript incomplete or unavailable";

#[derive(Clone, Copy, PartialEq, Eq)]
struct Identity(u64, u64);

impl Identity {
    fn of(info: &Metadata) -> Self {
        Self(info.dev(), info.ino())
    }
}

#[derive(PartialEq, Eq)]
struct FileVersion(Identity, u64, cap_std::time::SystemTime);

impl FileVersion {
    fn of(info: &Metadata) -> io::Result<Self> {
        Ok(Self(Identity::of(info), info.len(), info.modified()?))
    }
}

#[derive(Clone)]
struct Root {
    path: PathBuf,
    identity: Identity,
}

impl Root {
    fn choose(path: PathBuf) -> io::Result<Self> {
        let directory = open_absolute(&path)?;
        Ok(Self {
            path,
            identity: Identity::of(&directory.dir_metadata()?),
        })
    }

    fn open(&self) -> io::Result<Dir> {
        let directory = open_absolute(&self.path)?;
        if Identity::of(&directory.dir_metadata()?) != self.identity {
            return Err(unsafe_path());
        }
        Ok(directory)
    }
}

fn open_absolute(path: &Path) -> io::Result<Dir> {
    let mut anchor = PathBuf::new();
    let mut parts = path.components().peekable();
    if let Some(Component::Prefix(prefix)) = parts.peek().copied() {
        if !matches!(
            prefix.kind(),
            std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)
        ) {
            return Err(unsafe_path());
        }
        anchor.push(prefix.as_os_str());
        parts.next();
    }
    let Some(Component::RootDir) = parts.next() else {
        return Err(unsafe_path());
    };
    anchor.push(std::path::MAIN_SEPARATOR_STR);
    // Ambient access ends at the volume root. Every following open is relative
    // to the preceding verified directory, including the native-picked leaf.
    let mut directory = Dir::open_ambient_dir(anchor, ambient_authority())?;
    for part in parts {
        let Component::Normal(name) = part else {
            return Err(unsafe_path());
        };
        directory = open_dir(&directory, name)?;
    }
    Ok(directory)
}

fn unsafe_path() -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, "Linked or replaced source")
}

fn linked(info: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use cap_std::fs::MetadataExt as _;
        info.file_attributes() & 0x400 != 0 // FILE_ATTRIBUTE_REPARSE_POINT
    }
    #[cfg(not(windows))]
    {
        info.file_type().is_symlink()
    }
}

fn open_dir(directory: &Dir, name: impl AsRef<Path>) -> io::Result<Dir> {
    let name = name.as_ref();
    let info = directory.symlink_metadata(name)?;
    if linked(&info) {
        return Err(unsafe_path());
    }
    if !info.is_dir() {
        return Err(io::ErrorKind::NotADirectory.into());
    }
    let opened = directory.open_dir_nofollow(name)?;
    let opened_info = opened.dir_metadata()?;
    if linked(&opened_info) || Identity::of(&info) != Identity::of(&opened_info) {
        return Err(unsafe_path());
    }
    Ok(opened)
}

fn regular(directory: &Dir, name: impl AsRef<Path>) -> io::Result<Option<(File, FileVersion)>> {
    let name = name.as_ref();
    let info = match directory.symlink_metadata(name) {
        Ok(info) => info,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if linked(&info) {
        return Err(unsafe_path());
    }
    if !info.is_file() {
        return Ok(None);
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_fs_ext::OpenOptionsSyncExt;
        options.nonblock(true);
    }
    let file = directory.open_with(name, &options)?;
    let opened = file.metadata()?;
    if linked(&opened) || !opened.is_file() || Identity::of(&info) != Identity::of(&opened) {
        return Err(unsafe_path());
    }
    Ok(Some((file.into_std(), FileVersion::of(&opened)?)))
}

fn names(directory: &Dir) -> io::Result<Vec<OsString>> {
    let mut names = directory
        .entries()?
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect::<io::Result<Vec<_>>>()?;
    names.sort_unstable_by(|left, right| right.as_encoded_bytes().cmp(left.as_encoded_bytes()));
    Ok(names)
}

fn recording(name: &OsStr) -> bool {
    name.to_str().is_some_and(|name| {
        (name == "recording" || name.starts_with("recording."))
            && !name.starts_with("recording.partial.")
    })
}

fn partial(name: &OsStr) -> bool {
    name.to_str()
        .is_some_and(|name| name.starts_with("recording.partial."))
}

fn inspect_recording(directory: &Dir, names: &[OsString]) -> io::Result<(Audio, bool)> {
    let mut audio = Audio::Missing;
    let mut unfinished = false;
    for name in names {
        if recording(name) {
            if let Some((file, _)) = regular(directory, name)? {
                audio = match audio {
                    Audio::Missing => Audio::Available(file, name.clone()),
                    _ => Audio::Ambiguous,
                };
            }
        } else if partial(name) && regular(directory, name)?.is_some() {
            unfinished = true;
        }
    }
    Ok((audio, unfinished))
}

pub enum Audio {
    Missing,
    Ambiguous,
    Available(File, OsString),
}

impl Audio {
    pub fn state(&self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Ambiguous => "ambiguous",
            Self::Available(..) => "ready",
        }
    }
}

#[derive(Clone, Copy)]
enum RunKind {
    Managed(bool),
    Historical { valid: bool, unconfirmed: bool },
}

impl RunKind {
    fn status(self) -> &'static str {
        match self {
            Self::Managed(true) => "Transcript ready",
            Self::Managed(false) => "Transcript ready; remote cleanup unconfirmed",
            Self::Historical {
                valid: false,
                unconfirmed: true,
            } => "Saved HTML available; remote cleanup unconfirmed",
            Self::Historical {
                valid: false,
                unconfirmed: false,
            } => "Saved HTML available",
            Self::Historical {
                valid: true,
                unconfirmed: true,
            } => "Historical transcript; remote cleanup unconfirmed",
            Self::Historical {
                valid: true,
                unconfirmed: false,
            } => "Historical transcript",
        }
    }
}

#[derive(PartialEq, Eq)]
struct RunVersion {
    directory: Identity,
    files: [Option<FileVersion>; 5],
}

struct ReadRun {
    version: RunVersion,
    kind: RunKind,
    transcript: Option<ReaderTranscript>,
}

fn read_run(runs: &Dir, name: &OsStr) -> io::Result<Option<ReadRun>> {
    let directory = match open_dir(runs, name) {
        Ok(directory) => directory,
        Err(error) if error.kind() == io::ErrorKind::NotADirectory => return Ok(None),
        Err(error) => return Err(error),
    };
    let receipt_exists = match directory.symlink_metadata(MANAGED) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => return Err(error),
    };
    let Some((canonical, canonical_version)) = regular(&directory, CANONICAL)? else {
        return Ok(None);
    };
    let Some((_, html_version)) = regular(&directory, HTML)? else {
        return Ok(None);
    };
    let identity = Identity::of(&directory.dir_metadata()?);
    if receipt_exists {
        let Some((_, markdown_version)) = regular(&directory, MARKDOWN)? else {
            return Ok(None);
        };
        let Some((receipt, receipt_version)) = regular(&directory, MANAGED)? else {
            return Ok(None);
        };
        if markdown_version.1 == 0 || html_version.1 == 0 {
            return Ok(None);
        }
        let Some(receipt) = receipt_json(receipt) else {
            return Ok(None);
        };
        if receipt.get("state").and_then(Value::as_str) != Some("published") {
            return Ok(None);
        }
        let transcript = ReaderTranscript::read(canonical, false)?;
        return Ok(Some(ReadRun {
            version: RunVersion {
                directory: identity,
                files: [
                    Some(canonical_version),
                    Some(html_version),
                    Some(markdown_version),
                    Some(receipt_version),
                    None,
                ],
            },
            kind: RunKind::Managed(deletion_confirmed(&receipt)),
            transcript: Some(transcript),
        }));
    }

    let old_receipt = regular(&directory, HISTORICAL)?;
    let (old_receipt, unconfirmed) = match old_receipt {
        Some((file, version)) => (
            Some(version),
            receipt_json(file).is_some_and(|receipt| !deletion_confirmed(&receipt)),
        ),
        None => (None, false),
    };
    let valid = ReaderTranscript::read(canonical, true).is_ok();
    Ok(Some(ReadRun {
        version: RunVersion {
            directory: identity,
            files: [
                Some(canonical_version),
                Some(html_version),
                None,
                None,
                old_receipt,
            ],
        },
        kind: RunKind::Historical { valid, unconfirmed },
        transcript: None,
    }))
}

fn receipt_json(file: File) -> Option<Value> {
    serde_json::from_reader(BufReader::new(file)).ok()
}

fn deletion_confirmed(receipt: &Value) -> bool {
    receipt
        .get("deletion")
        .and_then(|deletion| deletion.get("confirmed"))
        .and_then(Value::as_bool)
        == Some(true)
}

#[derive(Debug, Serialize)]
pub struct Error {
    pub code: &'static str,
    pub message: &'static str,
}

impl Error {
    pub fn unavailable() -> Self {
        Self {
            code: "unavailable_library",
            message: "Library unavailable. Choose a library or refresh.",
        }
    }

    pub fn stale() -> Self {
        Self {
            code: "stale_id",
            message: "Unknown or expired selection. Refresh and select it again.",
        }
    }

    fn source(error: &io::Error) -> Self {
        match error.kind() {
            io::ErrorKind::PermissionDenied => Self {
                code: "unsafe_path",
                message: "Linked or changed source. Refresh the library.",
            },
            io::ErrorKind::InvalidData => Self {
                code: "malformed_transcript",
                message: "Saved transcript is malformed.",
            },
            _ => Self {
                code: "unavailable_source",
                message: "Source unavailable. Refresh the library.",
            },
        }
    }
}

struct Run {
    id: String,
    name: OsString,
    version: RunVersion,
}

struct Meeting {
    id: String,
    name: OsString,
    identity: Identity,
    runs: Vec<Run>,
}

#[derive(Serialize)]
struct RunRow {
    id: String,
    label: String,
    status: &'static str,
}

#[derive(Serialize)]
struct MeetingRow {
    id: String,
    title: String,
    added: String,
    status: &'static str,
    default_run_id: Option<String>,
    runs: Vec<RunRow>,
}

#[derive(Default, Serialize)]
pub struct Listing {
    library: String,
    meetings: Vec<MeetingRow>,
    errors: Vec<Error>,
}

impl Listing {
    fn report(&mut self, error: &io::Error) {
        if self.errors.len() < 100 {
            self.errors.push(Error::source(error));
        }
    }
}

pub struct Library {
    root: Root,
    meetings: HashMap<String, Meeting>,
}

pub struct Selection {
    pub title: String,
    pub added: String,
    pub status: &'static str,
    pub transcript: Option<ReaderTranscript>,
    pub historical: bool,
    pub audio: Audio,
    pub media_error: Option<Error>,
}

impl Library {
    pub fn choose(path: PathBuf) -> Result<Self, Error> {
        let root = Root::choose(path).map_err(|_| Error::unavailable())?;
        Ok(Self {
            root,
            meetings: HashMap::new(),
        })
    }

    pub fn scan(&self) -> Result<(Self, Listing), Error> {
        let directory = self.root.open().map_err(|_| Error::unavailable())?;
        let mut listing = Listing {
            library: self
                .root
                .path
                .file_name()
                .unwrap_or(OsStr::new("Selected library"))
                .to_string_lossy()
                .into_owned(),
            ..Listing::default()
        };
        let mut meetings = HashMap::new();
        let previous = self
            .meetings
            .values()
            .map(|meeting| (&meeting.name, meeting))
            .collect::<HashMap<_, _>>();
        for name in names(&directory).map_err(|_| Error::unavailable())? {
            match scan_meeting(
                &directory,
                name.clone(),
                previous.get(&name).copied(),
                &mut listing,
            ) {
                Ok(Some((meeting, row))) => {
                    meetings.insert(meeting.id.clone(), meeting);
                    listing.meetings.push(row);
                }
                Ok(None) => {}
                Err(error) => listing.report(&error),
            }
        }
        Ok((
            Self {
                root: self.root.clone(),
                meetings,
            },
            listing,
        ))
    }

    pub fn select(&self, meeting_id: &str, run_id: Option<&str>) -> Result<Selection, Error> {
        let meeting = self.meetings.get(meeting_id).ok_or_else(Error::stale)?;
        let run = run_id
            .map(|id| {
                meeting
                    .runs
                    .iter()
                    .find(|run| run.id == id)
                    .ok_or_else(Error::stale)
            })
            .transpose()?;
        let root = self.root.open().map_err(|_| Error::stale())?;
        let bundle = open_dir(&root, &meeting.name).map_err(|error| Error::source(&error))?;
        if Identity::of(
            &bundle
                .dir_metadata()
                .map_err(|error| Error::source(&error))?,
        ) != meeting.identity
        {
            return Err(Error::stale());
        }
        let files = names(&bundle).map_err(|error| Error::source(&error))?;
        let (audio, unfinished, media_error) = match inspect_recording(&bundle, &files) {
            Ok((audio, unfinished)) => (audio, unfinished, None),
            Err(error) => (Audio::Missing, false, Some(Error::source(&error))),
        };
        let (status, transcript, historical) = if let Some(run) = run {
            let runs = open_dir(&bundle, "transcripts").map_err(|error| Error::source(&error))?;
            let Some(read) = read_run(&runs, &run.name).map_err(|error| Error::source(&error))?
            else {
                return Err(Error {
                    code: "unavailable_source",
                    message: "Transcript incomplete or unavailable. Refresh the library.",
                });
            };
            if read.version != run.version {
                return Err(Error::stale());
            }
            (
                read.kind.status(),
                read.transcript,
                matches!(read.kind, RunKind::Historical { .. }),
            )
        } else {
            (
                meeting_status(has_transcripts(&files), &audio, unfinished),
                None,
                false,
            )
        };
        let (title, added) = meeting_identity(&meeting.name);
        Ok(Selection {
            title,
            added,
            status,
            transcript,
            historical,
            audio,
            media_error,
        })
    }
}

fn has_transcripts(files: &[OsString]) -> bool {
    files.iter().any(|name| name == "transcripts")
}

fn meeting_status(transcripts: bool, audio: &Audio, unfinished: bool) -> &'static str {
    match (transcripts, audio, unfinished) {
        (true, _, _) => INCOMPLETE,
        (false, Audio::Available(..) | Audio::Ambiguous, _) => "Recording saved",
        (false, Audio::Missing, true) => "Unfinished recording",
        _ => INCOMPLETE,
    }
}

fn scan_meeting(
    root: &Dir,
    name: OsString,
    old: Option<&Meeting>,
    listing: &mut Listing,
) -> io::Result<Option<(Meeting, MeetingRow)>> {
    let bundle = match open_dir(root, &name) {
        Ok(bundle) => bundle,
        Err(error) if error.kind() == io::ErrorKind::NotADirectory => return Ok(None),
        Err(error) => return Err(error),
    };
    let files = names(&bundle)?;
    let transcripts = has_transcripts(&files);
    if !transcripts && !files.iter().any(|name| recording(name) || partial(name)) {
        return Ok(None);
    }
    let identity = Identity::of(&bundle.dir_metadata()?);
    let old = old.filter(|meeting| meeting.identity == identity);
    let id = old
        .map(|meeting| Ok(meeting.id.clone()))
        .unwrap_or_else(token)?;
    let mut runs = Vec::new();
    let mut rows = Vec::new();
    if transcripts {
        match open_dir(&bundle, "transcripts").and_then(|dir| Ok((names(&dir)?, dir))) {
            Ok((candidates, dir)) => {
                for candidate in candidates {
                    match read_run(&dir, &candidate) {
                        Ok(Some(read)) => {
                            let old_run = old.and_then(|meeting| {
                                meeting.runs.iter().find(|run| {
                                    run.name == candidate && run.version == read.version
                                })
                            });
                            let run_id = old_run
                                .map(|run| Ok(run.id.clone()))
                                .unwrap_or_else(token)?;
                            rows.push(RunRow {
                                id: run_id.clone(),
                                label: candidate.to_string_lossy().into_owned(),
                                status: read.kind.status(),
                            });
                            runs.push(Run {
                                id: run_id,
                                name: candidate,
                                version: read.version,
                            });
                        }
                        Ok(None) => {}
                        Err(error) if error.kind() == io::ErrorKind::InvalidData => {}
                        Err(error) => listing.report(&error),
                    }
                }
            }
            Err(error) => listing.report(&error),
        }
    }
    let status = if let Some(row) = rows.first() {
        row.status
    } else {
        match inspect_recording(&bundle, &files) {
            Ok((audio, unfinished)) => meeting_status(transcripts, &audio, unfinished),
            Err(error) => {
                listing.report(&error);
                INCOMPLETE
            }
        }
    };
    let (title, added) = meeting_identity(&name);
    let row = MeetingRow {
        id: id.clone(),
        title,
        added,
        status,
        default_run_id: rows.first().map(|row| row.id.clone()),
        runs: rows,
    };
    Ok(Some((
        Meeting {
            id,
            name,
            identity,
            runs,
        },
        row,
    )))
}

fn meeting_identity(name: &OsStr) -> (String, String) {
    let name = name.to_string_lossy();
    let Some((date, label)) = name.get(..15).zip(name.get(15..)) else {
        return ("Transcript".into(), "Date unavailable".into());
    };
    let Ok(date) = NaiveDateTime::parse_from_str(date, "%Y%m%d-%H%M%S") else {
        return ("Transcript".into(), "Date unavailable".into());
    };
    let label = if label.is_empty() {
        "meeting"
    } else if let Some(label) = label.strip_prefix('-').filter(|label| !label.is_empty()) {
        label
    } else {
        return ("Transcript".into(), "Date unavailable".into());
    };
    let label = label.replace(['-', '_'], " ");
    let mut chars = label.trim().chars();
    let title = chars
        .next()
        .map(|first| first.to_uppercase().chain(chars).collect())
        .unwrap_or_default();
    (title, date.format("%B %-d, %Y at %-I:%M %p").to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    const VALID: &str = r#"{"language":"en","text":"Hello","speaker_names":{"A":"Alex"},"segments":[{"id":4,"start":0,"end":1,"text":"Hello","speaker":"A","words":[{"word":"Hello","start":0,"end":1,"probability":0.9,"speaker":"A"}]}]}"#;

    #[test]
    fn mixed_runs_keep_historical_identity_and_validate_managed_words() {
        let temp = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let meeting = temp.path().join("20261104-120000-Test-meeting");
        let runs = meeting.join("transcripts");
        for label in ["03-clean", "02-unconfirmed", "01-historical", "00-bad"] {
            let directory = runs.join(label);
            fs::create_dir_all(&directory).unwrap();
            fs::write(directory.join(HTML), "<p>Hello</p>").unwrap();
            fs::write(directory.join(MARKDOWN), "# Hello").unwrap();
            let raw = if label == "00-bad" {
                VALID.replace("0.9", "1.2")
            } else {
                VALID.into()
            };
            fs::write(directory.join(CANONICAL), raw).unwrap();
            if label != "01-historical" {
                let receipt = if label == "03-clean" {
                    json!({"state":"published","deletion":{"confirmed":true}})
                } else {
                    json!({"state":"published"})
                };
                fs::write(directory.join(MANAGED), receipt.to_string()).unwrap();
            }
        }
        fs::write(meeting.join("recording.wav"), "test audio bytes").unwrap();
        let library = Library::choose(temp.path().to_owned()).unwrap();
        let (library, listing) = library.scan().unwrap();
        let row = &listing.meetings[0];
        assert_eq!(row.title, "Test meeting");
        assert_eq!(
            row.runs
                .iter()
                .map(|run| run.label.as_str())
                .collect::<Vec<_>>(),
            ["03-clean", "02-unconfirmed", "01-historical"]
        );
        assert!(row.runs[1].status.contains("cleanup unconfirmed"));
        let historical = library.select(&row.id, Some(&row.runs[2].id)).unwrap();
        assert!(historical.historical && historical.transcript.is_none());
        let transcript = library
            .select(&row.id, Some(&row.runs[1].id))
            .unwrap()
            .transcript
            .unwrap();
        let display = serde_json::to_value(transcript).unwrap();
        assert_eq!(
            display["passages"][0],
            json!({"start":0.0,"end":1.0,"text":"Hello","speaker":"A"})
        );
        assert_eq!(display["speaker_names"]["A"], "Alex");
        fs::remove_dir_all(runs.join("02-unconfirmed")).unwrap();
        assert!(library.select(&row.id, Some(&row.runs[1].id)).is_err());
        let (_, refreshed) = library.scan().unwrap();
        assert_eq!(refreshed.meetings[0].id, row.id);
        assert_eq!(
            refreshed.meetings[0].default_run_id.as_deref(),
            Some(row.runs[0].id.as_str())
        );
    }

    #[cfg(unix)]
    #[test]
    fn replacement_link_cannot_change_the_bytes_of_a_verified_recording() {
        use std::{io::Read, os::unix::fs::symlink};

        let temp = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let chosen = temp.path().join("chosen");
        let meeting = chosen.join("20261104-120000-Audio-only");
        fs::create_dir_all(&meeting).unwrap();
        let recording = meeting.join("recording.wav");
        let outside = temp.path().join("secret");
        fs::write(&recording, "verified").unwrap();
        fs::write(&outside, "outside sentinel").unwrap();

        let (library, listing) = Library::choose(chosen).unwrap().scan().unwrap();
        let row = &listing.meetings[0];
        assert_eq!(row.status, "Recording saved");
        let Audio::Available(mut handle, _) = library.select(&row.id, None).unwrap().audio else {
            panic!("expected the selected recording");
        };
        fs::rename(&recording, meeting.join("moved.wav")).unwrap();
        symlink(outside, recording).unwrap();
        let mut bytes = String::new();
        handle.read_to_string(&mut bytes).unwrap();
        assert_eq!(bytes, "verified");
        let selection = library.select(&row.id, None).unwrap();
        assert!(matches!(selection.audio, Audio::Missing));
        assert!(selection.media_error.is_some());
    }
}
