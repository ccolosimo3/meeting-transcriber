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

use library::{Audio, Error, Library, Listing};
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

impl Reader {
    fn begin(&self, replacing: bool) -> (u64, Option<Arc<Library>>) {
        let mut session = self.session.lock().unwrap();
        self.media.revoke();
        session.generation += 1;
        let previous = if replacing {
            session.library.take()
        } else {
            session.library.clone()
        };
        (session.generation, previous)
    }
}

fn failure(error: Error) -> Value {
    json!({"error": error.message, "code": error.code})
}

async fn scan(reader: &Reader, chosen: Option<PathBuf>) -> Value {
    let (generation, previous) = reader.begin(chosen.is_some());
    let loaded = tauri::async_runtime::spawn_blocking(move || {
        let result = if let Some(path) = chosen {
            Library::choose(path)?.scan()
        } else if let Some(previous) = previous {
            previous.scan()
        } else {
            return Ok(None);
        }?;
        Ok(Some(result))
    })
    .await;

    let mut session = reader.session.lock().unwrap();
    if session.generation != generation {
        return failure(Error::stale());
    }
    match loaded {
        Ok(Ok(Some((library, listing)))) => {
            session.library = Some(Arc::new(library));
            json!(listing)
        }
        Ok(Ok(None)) => json!(Listing::default()),
        Ok(Err(error)) => {
            session.library = None;
            failure(error)
        }
        Err(_) => {
            session.library = None;
            failure(Error::unavailable())
        }
    }
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
        .pick_folder(move |choice| {
            let _ = sender.send(choice);
        });
    let Some(choice) = result.await.map_err(|_| ())? else {
        return Ok(json!({"cancelled": true}));
    };
    match choice.into_path() {
        Ok(path) => Ok(scan(&reader, Some(path)).await),
        Err(_) => {
            reader.begin(true);
            Ok(failure(Error::unavailable()))
        }
    }
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
    let (generation, library) = reader.begin(false);
    let Some(library) = library else {
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

    // The same lock protects selection generation changes and media grants.
    let session = reader.session.lock().unwrap();
    if session.generation != generation {
        return Ok(failure(Error::stale()));
    }
    let mut media_state = selection.audio.state();
    let mut media_error = selection.media_error;
    let mut media_url = None;
    if let Audio::Available(file, name) = selection.audio {
        match mime(&name).map(|mime| reader.media.issue(file, mime)) {
            Some(Ok(url)) => media_url = Some(url),
            result => {
                media_state = "missing";
                media_error = Some(Error {
                    code: "unavailable_source",
                    message: if result.is_none() {
                        "This recording format is unavailable in the reader."
                    } else {
                        "Audio changed or is unavailable. Refresh to try again."
                    },
                });
            }
        }
    }
    Ok(json!({
        "title": selection.title,
        "added": selection.added,
        "status": selection.status,
        "reader_transcript": selection.transcript,
        "historical": selection.historical,
        "media_state": media_state,
        "media_error": media_error,
        "media_url": media_url,
    }))
}

fn mime(name: &OsStr) -> Option<&'static str> {
    let extension = Path::new(name).extension()?.to_str()?.to_ascii_lowercase();
    match extension.as_str() {
        "wav" => Some("audio/wav"),
        "mp3" => Some("audio/mpeg"),
        "m4a" | "mp4" => Some("audio/mp4"),
        "aac" => Some("audio/aac"),
        "aif" | "aiff" => Some("audio/aiff"),
        "flac" => Some("audio/flac"),
        "ogg" | "oga" | "opus" => Some("audio/ogg"),
        "webm" => Some("audio/webm"),
        _ => None,
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
