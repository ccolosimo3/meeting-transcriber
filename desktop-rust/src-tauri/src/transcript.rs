//! Canonical reader projection. Timed words must validate even though the UI
//! receives only passages. The file contract is shared with lib/transcript_bundle.py.
use std::{
    collections::{BTreeMap, HashSet},
    fs::File,
    io::{self, BufReader},
};

use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct Canonical {
    language: String,
    text: String,
    segments: Vec<Segment>,
    speaker_names: Option<BTreeMap<String, String>>,
}

#[derive(Deserialize)]
struct Segment {
    id: Box<serde_json::value::RawValue>,
    start: f64,
    end: f64,
    text: String,
    speaker: String,
    words: Vec<Word>,
}

#[derive(Deserialize, Serialize)]
struct Passage {
    start: f64,
    end: f64,
    text: String,
    speaker: String,
}

#[derive(Deserialize)]
struct Word {
    word: String,
    start: f64,
    end: f64,
    probability: f64,
    speaker: String,
}

#[derive(Serialize)]
pub struct ReaderTranscript {
    language: String,
    speaker_names: BTreeMap<String, String>,
    passages: Vec<Passage>,
}

// Python's str.strip also recognizes the four ASCII information separators.
fn trimmed(value: &str) -> &str {
    value.trim_matches(|character: char| {
        character.is_whitespace() || ('\u{001c}'..='\u{001f}').contains(&character)
    })
}

fn interval(start: f64, end: f64) -> bool {
    start.is_finite() && end.is_finite() && start >= 0.0 && end >= start
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Invalid canonical transcript")
}

impl ReaderTranscript {
    pub fn read(file: File, historical: bool) -> io::Result<Self> {
        let canonical: Canonical =
            serde_json::from_reader(BufReader::new(file)).map_err(|error| {
                if error.is_io() {
                    error.into()
                } else {
                    invalid()
                }
            })?;
        if trimmed(&canonical.language).is_empty() || trimmed(&canonical.text).is_empty() {
            return Err(invalid());
        }
        let mut labels = HashSet::new();
        let mut has_words = false;
        for segment in &canonical.segments {
            let id = segment
                .id
                .get()
                .strip_prefix('-')
                .unwrap_or(segment.id.get());
            if !id.bytes().all(|byte| byte.is_ascii_digit())
                || !interval(segment.start, segment.end)
                || trimmed(&segment.text).is_empty()
                || trimmed(&segment.speaker).is_empty()
            {
                return Err(invalid());
            }
            labels.insert(segment.speaker.as_str());
            for word in &segment.words {
                if word.word.is_empty()
                    || word.speaker.is_empty()
                    || !interval(word.start, word.end)
                    || !(0.0..=1.0).contains(&word.probability)
                {
                    return Err(invalid());
                }
                labels.insert(word.speaker.as_str());
                has_words = true;
            }
        }
        if !has_words {
            return Err(invalid());
        }
        let speaker_names = match canonical.speaker_names {
            Some(names) => names,
            None if historical => BTreeMap::new(),
            None => return Err(invalid()),
        };
        if speaker_names.iter().any(|(label, name)| {
            !labels.contains(label.as_str()) || name.is_empty() || name != trimmed(name)
        }) {
            return Err(invalid());
        }
        Ok(Self {
            language: canonical.language,
            speaker_names,
            passages: canonical
                .segments
                .into_iter()
                .map(|segment| Passage {
                    start: segment.start,
                    end: segment.end,
                    text: segment.text,
                    speaker: segment.speaker,
                })
                .collect(),
        })
    }
}
