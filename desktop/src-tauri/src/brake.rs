//! "Stop All Work" from the system menu, without the page.
//!
//! The window's own button goes through the page, and the page is exactly the
//! part that may be frozen, loading or behind a dialog when somebody reaches for
//! a brake. So the menu item asks the runtime directly, over the same loopback
//! HTTP, with nothing but the standard library.
//!
//! If the runtime does not answer, the stop is written where the runtime reads
//! it - the record beside the data directory, in the format
//! `domain/safety/emergency.py` defines - so the next start comes up stopped
//! and a runtime that is merely slow still reads it before its next effect.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

const REASON: &str = "stopped from the application menu";

pub fn stop_all_work(base_url: &str) -> String {
    match post_stop(base_url) {
        Ok(()) => "all work stopped through the runtime".to_string(),
        Err(error) => match write_record() {
            Ok(path) => format!(
                "the runtime did not answer ({error}); the stop was written to {}",
                path.display()
            ),
            Err(write_error) => {
                format!("the stop could not be delivered ({error}) or written ({write_error})")
            }
        },
    }
}

fn post_stop(base_url: &str) -> Result<(), String> {
    let address = base_url
        .trim_start_matches("http://")
        .trim_end_matches('/')
        .to_string();
    let socket = address
        .parse()
        .map_err(|_| format!("bad address {address}"))?;
    let mut stream = TcpStream::connect_timeout(&socket, Duration::from_millis(500))
        .map_err(|error| error.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|error| error.to_string())?;
    let body = format!("{{\"reason\":\"{REASON}\"}}");
    let request = format!(
        "POST /api/runtime/stop HTTP/1.1\r\nHost: {address}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| error.to_string())?;
    if response.starts_with("HTTP/1.1 200") {
        Ok(())
    } else {
        Err(response.lines().next().unwrap_or("no answer").to_string())
    }
}

fn data_dir() -> Option<PathBuf> {
    if let Ok(configured) = std::env::var("PROMETHEUS_DATA_DIR") {
        return Some(PathBuf::from(configured));
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join(".prometheus"))
}

fn write_record() -> Result<PathBuf, String> {
    let directory = data_dir().ok_or("no home directory")?;
    std::fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let path = directory.join("STOP");
    let staged = directory.join(".STOP.shell.tmp");
    let record = format!("{{\"engaged_by\": \"menu\", \"reason\": \"{REASON}\", \"version\": 1}}");
    {
        let mut file = std::fs::File::create(&staged).map_err(|error| error.to_string())?;
        file.write_all(record.as_bytes())
            .map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
    }
    std::fs::rename(&staged, &path).map_err(|error| error.to_string())?;
    Ok(path)
}
