//! Starting the local Prometheus runtime, and getting out of its way.
//!
//! The shell is an interface. It does not plan, does not call a model, does not
//! touch the database and does not know what an employee is - it owns a window
//! and, on this machine, the lifetime of the process behind it. Everything the
//! window shows arrives over the same local HTTP the browser page uses, which
//! is what keeps the two views of one engine rather than two engines.
//!
//! Three decisions are worth stating.
//!
//! **A runtime that is already up is used, never replaced.** A developer with
//! `prometheus serve` running in a terminal opens this and gets that engine, with
//! its database and its running work. Starting a second one against the same
//! SQLite file would be two writers and one file.
//!
//! **The child is killed when the window closes**, but only if this process
//! started it. Killing an engine somebody else launched would take their
//! running work down with a window they merely closed.
//!
//! **The command is configurable and defaults to the repository.** A packaged
//! application will point `PROMETHEUS_RUNTIME_CMD` at an installed interpreter;
//! during development the default is what a developer already has working.

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;

/// Where the runtime listens. The same default as `Settings.ui_host`/`ui_port`.
pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8765";

/// What the shell reports to the window while it waits for the engine.
#[derive(Debug, Clone, Serialize)]
pub struct RuntimeStatus {
    pub base_url: String,
    /// True when this process started the engine, rather than finding one.
    pub started_here: bool,
    /// Whether anything is answering there yet. False is not fatal - the window
    /// says so and keeps trying - but it is the difference between "starting"
    /// and "there is nothing to talk to", which the page cannot tell on its own.
    pub ready: bool,
}

#[derive(Default)]
pub struct RuntimeHandle {
    child: Mutex<Option<Child>>,
}

impl RuntimeHandle {
    /// Wait until the runtime answers, or give up after `timeout`.
    ///
    /// The window asks where to talk *before* it draws, and the engine it just
    /// started needs a second or two to bind its port. Without this the first
    /// request goes out into a closed socket, the page says "error sending
    /// request", and nothing retries - a window that is broken for the whole
    /// session because it was half a second early.
    pub fn wait_until_ready(&self, base_url: &str, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if answering(base_url) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        false
    }

    /// Start the runtime unless one is already answering, or we already started one.
    ///
    /// Asked more than once: at startup, and again by the window as it resolves
    /// where to talk. The second check arrives while the first engine is still
    /// binding its port, so "is anything answering" is not enough on its own -
    /// it said no, and a second engine was started that could only fail with
    /// "address already in use". Remembering our own child is what makes this
    /// idempotent.
    pub fn ensure(&self, base_url: &str) -> RuntimeStatus {
        let started_here = self.child.lock().unwrap().is_some();
        if started_here || answering(base_url) {
            return RuntimeStatus {
                base_url: base_url.to_string(),
                started_here,
                ready: answering(base_url),
            };
        }
        let started = self.spawn();
        RuntimeStatus {
            base_url: base_url.to_string(),
            started_here: started,
            ready: answering(base_url),
        }
    }

    fn spawn(&self) -> bool {
        let command = std::env::var("PROMETHEUS_RUNTIME_CMD")
            .unwrap_or_else(|_| "uv run prometheus serve".to_string());
        let mut parts = command.split_whitespace();
        let Some(program) = parts.next() else {
            return false;
        };
        let mut command = Command::new(program);
        command
            .args(parts)
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
        // Its own process group, so that stopping it reaches the process that
        // actually serves. The default command is `uv run`, which starts the
        // runtime as a child of its own: killing `uv` left that child running,
        // answering on the port with the code it was started with, and the
        // next window attached to it - a stale engine nobody could see.
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let spawned = command.spawn();
        match spawned {
            Ok(child) => {
                *self.child.lock().unwrap() = Some(child);
                true
            }
            Err(error) => {
                // Not fatal. The window says the runtime is not answering and
                // the person can start it themselves - which is a better
                // outcome than a shell that refuses to open.
                eprintln!("prometheus: could not start the runtime: {error}");
                false
            }
        }
    }

    /// Stop the engine this process started. Leaves anybody else's alone.
    pub fn shutdown(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            #[cfg(unix)]
            stop_group(&mut child);
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Ask the whole group to stop, then insist.
///
/// SIGTERM first, because the runtime closes its running work on it and leaves
/// nothing half-written; SIGKILL to the group after a grace period, because a
/// window that will not close is worse than a run `resume` can pick up.
#[cfg(unix)]
fn stop_group(child: &mut Child) {
    let group = format!("-{}", child.id());
    let signal = |name: &str| {
        let _ = Command::new("kill")
            .args([name, "--", &group])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    };
    signal("-TERM");
    let deadline = Instant::now() + Duration::from_secs(12);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    signal("-KILL");
}

/// Whether anything is listening. Deliberately a connection, not a request:
/// the shell must not need to know the runtime's routes to see that it is up.
fn answering(base_url: &str) -> bool {
    let address = base_url
        .trim_start_matches("http://")
        .trim_start_matches("https://");
    std::net::TcpStream::connect_timeout(
        &match address.trim_end_matches('/').parse() {
            Ok(parsed) => parsed,
            Err(_) => return false,
        },
        std::time::Duration::from_millis(250),
    )
    .is_ok()
}
