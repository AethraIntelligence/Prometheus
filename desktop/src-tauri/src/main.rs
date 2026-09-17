// The desktop shell. One window, one child process, and no business logic.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod brake;
mod runtime;

use std::time::Duration;

use runtime::{RuntimeHandle, RuntimeStatus, DEFAULT_BASE_URL};
use tauri::menu::{Menu, MenuItem, Submenu};
use tauri::{Manager, RunEvent, State};

/// How long the shell waits for a runtime to start answering before it tells
/// the window to go ahead anyway. Long enough for a cold start that migrates a
/// database first, short enough that a genuinely missing runtime is reported
/// rather than hung on.
const STARTUP_GRACE: Duration = Duration::from_secs(90);

/// The menu item that stops all work. Its id is what the event carries.
const STOP_ALL_WORK: &str = "stop-all-work";

/// Where the window should talk to. The single fact the shell tells the UI.
#[tauri::command]
fn runtime_status(app: tauri::AppHandle, handle: State<'_, RuntimeHandle>) -> RuntimeStatus {
    let mut status = handle.ensure(&app, base_url().as_str());
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

/// The updater, when this build was made with an update channel.
///
/// The release workflow compiles with `PROMETHEUS_UPDATER_ENABLED` and merges
/// `tauri.release.conf.json`, which carries the channel's endpoint and the
/// public key every package is verified against. A build without them - every
/// developer build - has no updater at all, and the window says so, rather than
/// an updater with nothing to verify against.
fn updater() -> Option<tauri::plugin::TauriPlugin<tauri::Wry, tauri_plugin_updater::Config>> {
    option_env!("PROMETHEUS_UPDATER_ENABLED")?;
    Some(tauri_plugin_updater::Builder::new().build())
}

fn focus_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn main() {
    let mut builder = tauri::Builder::default()
        // First, so a second launch is turned away before it starts anything:
        // one window, one runtime, one owner of the data directory.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main(app);
        }))
        .plugin(tauri_plugin_http::init())
        // The system's own file dialog. A document is read by the runtime from
        // where it is, so what the window needs is a path - and a person should
        // pick the file, not type where it lives.
        .plugin(tauri_plugin_dialog::init())
        // A plugin's "get a token" link, in the system browser rather than in
        // this window, which only ever shows its own page.
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init());
    if let Some(plugin) = updater() {
        builder = builder.plugin(plugin);
    }
    builder
        .manage(RuntimeHandle::default())
        .invoke_handler(tauri::generate_handler![runtime_status, window_problem])
        .setup(|app| {
            // The brake in the system's own menu, with a shortcut: it works
            // while the page is frozen, loading or showing a dialog, which is
            // exactly when a button on the page does not.
            let stop = MenuItem::with_id(
                app,
                STOP_ALL_WORK,
                "Stop All Work",
                true,
                Some("CmdOrCtrl+Shift+."),
            )?;
            let menu = Menu::default(app.handle())?;
            menu.append(&Submenu::with_items(app, "Work", true, &[&stop])?)?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, event| {
                if event.id() == STOP_ALL_WORK {
                    let outcome = brake::stop_all_work(&base_url());
                    eprintln!("prometheus: {outcome}");
                    focus_main(app);
                }
            });
            // Started before the window paints, so the engine is warming up
            // while the greeting renders rather than after the first request.
            app.state::<RuntimeHandle>()
                .ensure(app.handle(), base_url().as_str());
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
