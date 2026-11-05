#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod library;
mod media;
mod transcript;

use std::{
    ffi::OsStr,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

use serde_json::{Value, json};
use tauri::{Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder, utils::config::Csp};
use tauri_plugin_dialog::DialogExt;

use library::{Audio, Error, Library, Listing, Selection};
use media::{Media, READER_ORIGIN};

#[derive(Default)]
struct Session {
    generation: u64,
    library: Option<Arc<Library>>,
}

struct Reader {
    session: Mutex<Session>,
    media: Arc<Media>,
    verified: AtomicBool,
}

enum Operation {
    Replace,
    Refresh,
    Load,
}

impl Reader {
    fn begin(&self, operation: Operation) -> Option<(u64, Option<Arc<Library>>)> {
        let mut session = self.session.lock().unwrap();
        let library = match operation {
            Operation::Replace => {
                session.library = None;
                None
            }
            Operation::Refresh => Some(session.library.take()?),
            Operation::Load => Some(session.library.clone()?),
        };
        self.media.revoke();
        session.generation += 1;
        Some((session.generation, library))
    }

    fn finish_scan(&self, generation: u64, loaded: Result<(Library, Listing), Error>) -> Value {
        let mut session = self.session.lock().unwrap();
        if session.generation != generation {
            return failure(Error::stale());
        }
        match loaded {
            Ok((library, listing)) => {
                session.library = Some(Arc::new(library));
                json!(listing)
            }
            Err(error) => failure(error),
        }
    }
}

fn failure(error: Error) -> Value {
    json!({"error": error.message, "code": error.code})
}

async fn scan(reader: &Reader, chosen: Option<Result<PathBuf, Error>>) -> Value {
    let operation = if chosen.is_some() {
        Operation::Replace
    } else {
        Operation::Refresh
    };
    let Some((generation, previous)) = reader.begin(operation) else {
        return json!(Listing::default());
    };
    let loaded = tauri::async_runtime::spawn_blocking(move || match chosen {
        Some(path) => Ok(Library::choose(path?)?.scan()),
        None => Ok(previous.ok_or_else(Error::unavailable)?.scan()),
    })
    .await
    .unwrap_or_else(|_| Err(Error::unavailable()));
    reader.finish_scan(generation, loaded)
}

#[tauri::command]
async fn choose_library(window: WebviewWindow, reader: State<'_, Reader>) -> Result<Value, ()> {
    if !reader.verified.load(Ordering::Acquire) {
        return Err(());
    }
    let (sender, result) = tokio::sync::oneshot::channel();
    window
        .dialog()
        .file()
        .set_parent(&window)
        .set_can_create_directories(false)
        .pick_folder(move |choice| {
            let _ = sender.send(choice);
        });
    let Some(choice) = result.await.map_err(|_| ())? else {
        return Ok(json!({"cancelled": true}));
    };
    Ok(scan(
        &reader,
        Some(choice.into_path().map_err(|_| Error::unavailable())),
    )
    .await)
}

#[tauri::command]
async fn refresh_library(reader: State<'_, Reader>) -> Result<Value, ()> {
    if !reader.verified.load(Ordering::Acquire) {
        return Err(());
    }
    Ok(scan(&reader, None).await)
}

#[tauri::command]
async fn load_run(
    meeting_id: String,
    run_id: Option<String>,
    reader: State<'_, Reader>,
) -> Result<Value, ()> {
    if !reader.verified.load(Ordering::Acquire) {
        return Err(());
    }
    let Some((generation, Some(library))) = reader.begin(Operation::Load) else {
        return Ok(failure(Error::stale()));
    };
    let loaded = tauri::async_runtime::spawn_blocking(move || {
        library.select(&meeting_id, run_id.as_deref())
    })
    .await
    .map_err(|_| ())?;
    let selection = match loaded {
        Ok(selection) => selection,
        Err(error) => return Ok(failure(error)),
    };

    Ok(reader.finish_load(generation, selection))
}

impl Reader {
    fn finish_load(&self, generation: u64, selection: Selection) -> Value {
        // The same lock protects selection generation changes and media grants.
        let session = self.session.lock().unwrap();
        if session.generation != generation {
            return failure(Error::stale());
        }
        let mut media_state = selection.audio.state();
        let mut media_error = selection.media_error;
        let mut media_url = None;
        if let Audio::Available(file, name) = selection.audio {
            match self.media.issue(file, mime(&name)) {
                Ok(url) => media_url = Some(url),
                Err(_) => {
                    media_state = "missing";
                    media_error = Some(Error {
                        code: "unavailable_source",
                        message: "Audio changed or is unavailable. Refresh to try again.",
                    });
                }
            }
        }
        json!({
            "title": selection.title,
            "added": selection.added,
            "status": selection.status,
            "reader_transcript": selection.transcript,
            "historical": selection.historical,
            "media_state": media_state,
            "media_error": media_error,
            "media_url": media_url,
        })
    }
}

fn mime(name: &OsStr) -> &'static str {
    let extension = Path::new(name)
        .extension()
        .and_then(OsStr::to_str)
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "wav" => "audio/wav",
        "mp3" => "audio/mpeg",
        "m4a" | "m4b" | "mp4" => "audio/mp4",
        "mov" | "qt" => "video/quicktime",
        "aac" => "audio/aac",
        "aif" | "aiff" | "aifc" => "audio/aiff",
        "caf" => "audio/x-caf",
        "flac" => "audio/flac",
        "ogg" | "oga" | "opus" => "audio/ogg",
        "webm" => "audio/webm",
        _ => "application/octet-stream",
    }
}

fn reader_document(url: &tauri::Url) -> bool {
    let Some(path) = url.as_str().strip_prefix(READER_ORIGIN) else {
        return false;
    };
    matches!(path, "" | "/")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (media, listener) = Media::listen()?;
    let mut context = tauri::generate_context!();
    context.config_mut().app.security.csp = Some(Csp::Policy(media.csp()));
    tauri::Builder::default()
        .manage(Reader {
            session: Mutex::new(Session::default()),
            media: media.clone(),
            verified: AtomicBool::new(false),
        })
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            choose_library,
            refresh_library,
            load_run
        ])
        .setup(move |app| {
            tauri::async_runtime::spawn(media.serve(listener));
            let window =
                WebviewWindowBuilder::new(app, "reader", WebviewUrl::App("index.html".into()))
                    .title("Meeting Transcriber Reader")
                    .inner_size(1200.0, 820.0)
                    .min_inner_size(400.0, 500.0)
                    .incognito(true)
                    .disable_drag_drop_handler()
                    .on_navigation(reader_document)
                    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                    .initialization_script(include_str!("bridge.js"))
                    .visible(false);
            start_native(window, app)?;
            Ok(())
        })
        .run(context)?;
    Ok(())
}

#[cfg(target_os = "macos")]
fn start_native(
    window: WebviewWindowBuilder<'_, tauri::Wry, tauri::App>,
    app: &tauri::App,
) -> Result<(), tauri::Error> {
    use objc2_web_kit::WKWebView;

    let window = window.build()?;
    let handle = app.handle().clone();
    let shown = window.clone();
    window.with_webview(move |platform| {
        // Wry supplies a live WKWebView for the duration of this main-thread callback.
        let persistent = unsafe {
            let native = &*platform.inner().cast::<WKWebView>();
            native.configuration().websiteDataStore().isPersistent()
        };
        if persistent {
            eprintln!("The reader could not obtain a private WKWebView data store.");
            handle.exit(1);
        } else {
            handle
                .state::<Reader>()
                .verified
                .store(true, Ordering::Release);
            if shown.show().is_err() {
                handle.exit(1);
            }
        }
    })
}

#[cfg(target_os = "windows")]
fn start_native(
    _window: WebviewWindowBuilder<'_, tauri::Wry, tauri::App>,
    _app: &tauri::App,
) -> Result<(), tauri::Error> {
    Err(std::io::Error::other("Protected WebView2 session is pending native qualification").into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, sync::mpsc, thread, time::Duration};

    #[test]
    fn refresh_blocks_selections_from_the_snapshot_it_revalidates() {
        let temporary = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let meeting = temporary.path().join("20261104-120000-Audio-only");
        fs::create_dir(&meeting).unwrap();
        fs::write(meeting.join("recording.wav"), "synthetic recording").unwrap();
        let (library, listing) = Library::choose(temporary.path().to_owned()).unwrap().scan();
        let id = json!(listing)["meetings"][0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let (media, _listener) = Media::listen().unwrap();
        let reader = Reader {
            session: Mutex::new(Session {
                generation: 0,
                library: Some(Arc::new(library)),
            }),
            media,
            verified: AtomicBool::new(true),
        };
        let (selected, selection_ready) = mpsc::channel();
        let (resume, resume_selection) = mpsc::channel();
        thread::scope(|scope| {
            let reader = &reader;
            let id = &id;
            let old = scope.spawn(move || {
                let (generation, library) = reader.begin(Operation::Load).unwrap();
                let selection = library.unwrap().select(id, None).unwrap();
                selected.send(()).unwrap();
                resume_selection
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap();
                reader.finish_load(generation, selection)
            });
            selection_ready
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
            let (generation, previous) = reader.begin(Operation::Refresh).unwrap();
            assert!(reader.begin(Operation::Load).is_none());
            let refreshed = reader.finish_scan(generation, Ok(previous.unwrap().scan()));
            assert_eq!(refreshed["meetings"][0]["id"], *id);
            resume.send(()).unwrap();
            let obsolete = old.join().unwrap();
            assert_eq!(obsolete["code"], "stale_id");
            assert!(obsolete["media_url"].is_null());
            let (generation, library) = reader.begin(Operation::Load).unwrap();
            let active = reader.finish_load(generation, library.unwrap().select(id, None).unwrap());
            assert!(active["media_url"].as_str().is_some());
        });
    }
}
