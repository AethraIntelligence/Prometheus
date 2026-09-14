// The desktop shell. One window, one child process, and no business logic.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod runtime;

use std::time::Duration;

use runtime::{RuntimeHandle, RuntimeStatus, DEFAULT_BASE_URL};
use tauri::{Manager, RunEvent, State};

/// How long the shell waits for a runtime to start answering before it tells
/// the window to go ahead anyway. Long enough for a cold `uv run` on a laptop,
/// short enough that a genuinely missing runtime is reported rather than hung on.
const STARTUP_GRACE: Duration = Duration::from_secs(45);

/// Where the window should talk to. The single fact the shell tells the UI.
#[tauri::command]
fn runtime_status(handle: State<'_, RuntimeHandle>) -> RuntimeStatus {
    let mut status = handle.ensure(base_url().as_str());
    // The window asks this before it draws, so this is the one place that can
    // wait for an engine that is still binding its port without the page
    // having to guess how long starting one takes.
    if !status.ready {
        status.ready = handle.wait_until_ready(&status.base_url, STARTUP_GRACE);
    }
    // Printed because it is the one line that says the window's own code is
    // running: everything else the shell does happens whether the page loaded
    // or not, and a blank window with a healthy runtime looks identical to a
    // working one from outside.
    println!("prometheus: the window is using {}", status.base_url);
    status
}

/// Something in the window failed. Printed where whoever started the shell can
/// see it: a page that cannot draw is otherwise indistinguishable from a page
/// that drew nothing, and both look like a healthy runtime from outside.
#[tauri::command]
fn window_problem(message: String) {
    eprintln!("prometheus: the window reported a problem: {message}");
}

fn base_url() -> String {
    std::env::var("PROMETHEUS_BASE_URL").unwrap_or_else(|_| DEFAULT_BASE_URL.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        // The system's own file dialog. A document is read by the runtime from
        // where it is, so what the window needs is a path - and a person should
        // pick the file, not type where it lives.
        .plugin(tauri_plugin_dialog::init())
        // A plugin's "get a token" link, in the system browser rather than in
        // this window, which only ever shows its own page.
        .plugin(tauri_plugin_opener::init())
        .manage(RuntimeHandle::default())
        .invoke_handler(tauri::generate_handler![runtime_status, window_problem])
        .setup(|app| {
            // Started before the window paints, so the engine is warming up
            // while the greeting renders rather than after the first request.
            app.state::<RuntimeHandle>().ensure(base_url().as_str());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the Prometheus shell")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                app.state::<RuntimeHandle>().shutdown();
            }
        });
}
