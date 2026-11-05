//! Loopback audio transport. A grant owns an already-verified file, never a path.
use std::{
    fs::File,
    io,
    net::{Ipv4Addr, TcpListener},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use axum::{
    Router,
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, Response, StatusCode, header},
};
use futures_util::stream;

const CHUNK: u64 = 64 * 1024;

#[cfg(target_os = "macos")]
pub const READER_ORIGIN: &str = "tauri://localhost";
#[cfg(target_os = "windows")]
pub const READER_ORIGIN: &str = "http://tauri.localhost";

struct Grant {
    route: String,
    file: File,
    size: u64,
    mime: &'static str,
    revoked: AtomicBool,
}

pub struct Media {
    host: String,
    grant: Mutex<Option<Arc<Grant>>>,
}

impl Media {
    pub fn listen() -> io::Result<(Arc<Self>, TcpListener)> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
        listener.set_nonblocking(true)?;
        let media = Self {
            host: listener.local_addr()?.to_string(),
            grant: Mutex::new(None),
        };
        Ok((Arc::new(media), listener))
    }

    pub fn origin(&self) -> String {
        format!("http://{}", self.host)
    }

    pub fn csp(&self) -> String {
        format!(
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; \
             media-src {}; connect-src 'self' ipc: http://ipc.localhost; \
             frame-src 'none'; frame-ancestors 'none'; object-src 'none'; \
             base-uri 'none'; form-action 'none'",
            self.origin()
        )
    }

    pub async fn serve(self: Arc<Self>, listener: TcpListener) -> io::Result<()> {
        let listener = tokio::net::TcpListener::from_std(listener)?;
        let router = Router::new().fallback(serve).with_state(self);
        axum::serve(listener, router).await
    }

    pub fn revoke(&self) {
        if let Some(grant) = self.grant.lock().unwrap().take() {
            grant.revoked.store(true, Ordering::Release);
        }
    }

    pub fn issue(&self, file: File, mime: &'static str) -> io::Result<String> {
        let size = file.metadata()?.len();
        let route = format!("/media/{}", token()?);
        let url = self.origin() + &route;
        let mut slot = self.grant.lock().unwrap();
        if let Some(grant) = slot.replace(Arc::new(Grant {
            route,
            file,
            size,
            mime,
            revoked: AtomicBool::new(false),
        })) {
            grant.revoked.store(true, Ordering::Release);
        }
        Ok(url)
    }
}

pub fn token() -> io::Result<String> {
    let mut bytes = [0_u8; 24];
    getrandom::fill(&mut bytes).map_err(io::Error::other)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

// A header must be absent or have exactly one valid value.
fn single<'a>(headers: &'a HeaderMap, name: &str) -> Result<Option<&'a str>, ()> {
    let all = headers.get_all(name);
    let mut values = all.iter();
    let first = values
        .next()
        .map(HeaderValue::to_str)
        .transpose()
        .map_err(|_| ())?;
    if values.next().is_some() {
        return Err(());
    }
    Ok(first)
}

fn admitted(headers: &HeaderMap, host: &str) -> bool {
    single(headers, "host") == Ok(Some(host))
        && matches!(single(headers, "origin"), Ok(None | Some(READER_ORIGIN)))
        && matches!(
            single(headers, "sec-fetch-mode"),
            Ok(None | Some("no-cors" | "cors"))
        )
        && matches!(
            single(headers, "sec-fetch-dest"),
            Ok(None | Some("audio" | "video" | "empty"))
        )
}

fn span(range: Option<&str>, size: u64) -> Option<(u64, u64)> {
    let Some(range) = range else {
        return Some((0, size));
    };
    let (first, last) = range.strip_prefix("bytes=")?.split_once('-')?;
    let number = |value: &str| {
        value
            .bytes()
            .all(|byte| byte.is_ascii_digit())
            .then(|| value.parse::<u64>().ok())
            .flatten()
    };
    if first.is_empty() {
        let suffix = number(last)?;
        return (suffix > 0 && size > 0).then(|| (size.saturating_sub(suffix), size));
    }
    let start = number(first)?;
    let end = if last.is_empty() {
        size
    } else {
        number(last)?.saturating_add(1).min(size)
    };
    (start < end).then_some((start, end))
}

fn response(status: StatusCode) -> axum::http::response::Builder {
    Response::builder()
        .status(status)
        .header(header::CACHE_CONTROL, "no-store")
        .header("x-content-type-options", "nosniff")
        .header("x-frame-options", "DENY")
}

async fn serve(State(media): State<Arc<Media>>, request: Request) -> Response<Body> {
    let empty = |status| response(status).body(Body::empty()).unwrap();
    if !matches!(*request.method(), Method::GET | Method::HEAD) {
        return empty(StatusCode::METHOD_NOT_ALLOWED);
    }
    let headers = request.headers();
    if !admitted(headers, &media.host) {
        return empty(StatusCode::FORBIDDEN);
    }
    let grant = media.grant.lock().unwrap().clone();
    let Some(grant) = grant.filter(|grant| {
        request
            .uri()
            .path_and_query()
            .is_some_and(|part| part.as_str() == grant.route)
            && !grant.revoked.load(Ordering::Acquire)
    }) else {
        return empty(StatusCode::NOT_FOUND);
    };
    let range = single(headers, "range");
    let Some((start, end)) = range.ok().and_then(|range| span(range, grant.size)) else {
        return response(StatusCode::RANGE_NOT_SATISFIABLE)
            .header(header::CONTENT_RANGE, format!("bytes */{}", grant.size))
            .body(Body::empty())
            .unwrap();
    };
    let partial = matches!(range, Ok(Some(_)));
    let mut result = response(if partial {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    })
    .header(header::ACCEPT_RANGES, "bytes")
    .header(header::CONTENT_TYPE, grant.mime)
    .header(header::CONTENT_LENGTH, end - start);
    if partial {
        result = result.header(
            header::CONTENT_RANGE,
            format!("bytes {start}-{}/{}", end - 1, grant.size),
        );
    }
    if single(headers, "origin") == Ok(Some(READER_ORIGIN)) {
        result = result
            .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, READER_ORIGIN)
            .header(header::VARY, "Origin");
    }
    let body = if request.method() == Method::HEAD {
        Body::empty()
    } else {
        Body::from_stream(stream::try_unfold(
            (grant, start),
            move |(grant, offset)| async move {
                if offset >= end {
                    return Ok(None);
                }
                if grant.revoked.load(Ordering::Acquire) {
                    return Err(io::Error::other("Revoked media grant"));
                }
                let count = (end - offset).min(CHUNK) as usize;
                let reader = grant.clone();
                let bytes = tokio::time::timeout(
                    Duration::from_secs(5),
                    tokio::task::spawn_blocking(move || {
                        let mut bytes = vec![0; count];
                        read_exact_at(&reader.file, &mut bytes, offset)?;
                        Ok::<_, io::Error>(bytes)
                    }),
                )
                .await
                .map_err(io::Error::other)?
                .map_err(io::Error::other)??;
                if grant.revoked.load(Ordering::Acquire) {
                    return Err(io::Error::other("Revoked media grant"));
                }
                Ok(Some((bytes, (grant, offset + count as u64))))
            },
        ))
    };
    result.body(body).unwrap()
}

#[cfg(target_os = "macos")]
fn read_exact_at(file: &File, bytes: &mut [u8], offset: u64) -> io::Result<()> {
    use std::os::unix::fs::FileExt;
    file.read_exact_at(bytes, offset)
}

#[cfg(target_os = "windows")]
fn read_exact_at(file: &File, mut bytes: &mut [u8], mut offset: u64) -> io::Result<()> {
    use std::os::windows::fs::FileExt;
    while !bytes.is_empty() {
        let count = file.seek_read(bytes, offset)?;
        if count == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        bytes = &mut bytes[count..];
        offset += count as u64;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Seek, SeekFrom, Write};
    use std::net::TcpStream;

    #[test]
    fn loopback_reads_only_the_granted_range_and_revokes_the_open_handle() {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"only granted recording").unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        let (media, listener) = Media::listen().unwrap();
        let addr = listener.local_addr().unwrap();
        let url = media.issue(file, "audio/wav").unwrap();
        let path = url.strip_prefix(&media.origin()).unwrap().to_owned();
        let server = media.clone();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async move {
            let service = tokio::spawn(server.serve(listener));
            tokio::task::spawn_blocking(move || {
                let request = |method: &str, route: &str, host: &str, extra: &str| {
                    let mut connection = TcpStream::connect(addr).unwrap();
                    connection.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                    write!(connection, "{method} {route} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n{extra}\r\n").unwrap();
                    let mut output = String::new();
                    connection.read_to_string(&mut output).unwrap();
                    let (headers, body) = output.split_once("\r\n\r\n").unwrap();
                    (headers.to_ascii_lowercase(), body.to_owned())
                };
                let host = addr.to_string();
                for (range, expected, content_range) in [
                    ("", "only granted recording", ""),
                    ("bytes=0-", "only granted recording", "bytes 0-21/22"),
                    ("bytes=5-11", "granted", "bytes 5-11/22"),
                    ("bytes=-9", "recording", "bytes 13-21/22"),
                ] {
                    let header = if range.is_empty() { String::new() } else { format!("Range: {range}\r\n") };
                    let (headers, bytes) = request("GET", &path, &host, &header);
                    assert_eq!(bytes, expected);
                    assert!(headers.contains("cache-control: no-store"));
                    if content_range.is_empty() {
                        assert!(headers.starts_with("http/1.1 200"));
                    } else {
                        assert!(headers.starts_with("http/1.1 206"));
                        assert!(headers.contains(content_range));
                    }
                }
                let (head, body) = request("HEAD", &path, &host, "");
                assert!(head.contains("content-length: 22"));
                assert!(body.is_empty());
                let (cors, _) = request("HEAD", &path, &host, &format!("Origin: {READER_ORIGIN}\r\n"));
                assert!(cors.contains(&format!("access-control-allow-origin: {READER_ORIGIN}")));

                for (method, route, host, extra, status) in [
                    ("GET", "/media/forged", host.as_str(), "", 404),
                    ("GET", path.as_str(), "localhost", "", 403),
                    ("GET", path.as_str(), host.as_str(), "Origin: null\r\n", 403),
                    ("GET", path.as_str(), host.as_str(), "Origin: https://foreign.invalid\r\n", 403),
                    ("GET", path.as_str(), host.as_str(), "Sec-Fetch-Mode: navigate\r\n", 403),
                    ("GET", path.as_str(), host.as_str(), "Sec-Fetch-Dest: iframe\r\n", 403),
                    ("GET", path.as_str(), host.as_str(), "Range: bytes=99-\r\n", 416),
                    ("GET", path.as_str(), host.as_str(), "Range: bytes=0-1,4-7\r\n", 416),
                    ("POST", path.as_str(), host.as_str(), "", 405),
                ] {
                    let (headers, body) = request(method, route, host, extra);
                    assert!(headers.starts_with(&format!("http/1.1 {status}")), "{headers}");
                    assert!(body.is_empty());
                }

                media.revoke();
                let (headers, body) = request("GET", &path, &host, "");
                assert!(headers.starts_with("http/1.1 404"));
                assert!(body.is_empty());
            }).await.unwrap();
            service.abort();
        });
    }
}
