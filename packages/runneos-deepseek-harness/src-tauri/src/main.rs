//! RunneOS shell for the DeepSeek Harness web UI (dsh).
//!
//! Contract (mirrors the legacy bash wrapper): start the dsh user service
//! on demand, wait for the local port, pair the webview with the dsh
//! access token on first run, then open the UI in a native window.
//! Re-running the entry focuses the existing window; `--pair` forces
//! token re-pairing. No sandbox/TLS flags are changed anywhere: the dsh
//! server keeps its own token fence and binds to 127.0.0.1 only.

use std::io::Read as _;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::Command;
use std::thread;
use std::time::Duration;
use std::{env, fs};

use serde_json::Value;

const SERVICE: &str = "runneos-deepseek-harness.service";
const HOST: &str = "127.0.0.1";
const PORT: u16 = 3080;
const URL: &str = "http://127.0.0.1:3080/";
// Wayland app_id comes from the binary name (tao), not the Tauri
// identifier; StartupWMClass in the desktop entry must match this.
const APP_ID: &str = "runneos-deepseek-harness";
const WINDOW_TITLE: &str = "DeepSeek Harness";
const DSH_BIN: &str = ".local/opt/dsh/node_modules/.bin/dsh";

fn state_marker() -> PathBuf {
    // Pairing state of the shell's own webview cookie store; the pairing
    // cookie itself lives in the WebKit data directory of the identifier.
    let base = env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/state"));
    base.join("runneos/deepseek-harness/paired")
}

fn home() -> PathBuf {
    PathBuf::from(env::var_os("HOME").unwrap_or_default())
}

fn notify(message: &str) {
    if Command::new("notify-send")
        .args(["-a", WINDOW_TITLE, message])
        .status()
        .is_err()
    {
        eprintln!("{WINDOW_TITLE}: {message}");
    }
}

fn command_output(cmd: &mut Command) -> Option<String> {
    let mut out = String::new();
    cmd.stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?
        .stdout
        .take()?
        .read_to_string(&mut out)
        .ok()?;
    Some(out)
}

fn service_running() -> bool {
    Command::new("systemctl")
        .args(["--user", "is-active", "--quiet", SERVICE])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn wait_port() -> bool {
    for _ in 0..150 {
        if TcpStream::connect((HOST, PORT)).is_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(200));
    }
    false
}

/// The dsh web UI prints its access-token URL to the unit journal at
/// every start; the token rotates, so always take the latest one.
fn latest_token() -> Option<String> {
    let journal = command_output(
        Command::new("journalctl")
            .args(["--user", "-u", SERVICE, "--no-pager", "-n", "50"]),
    )?;
    journal
        .rsplit("token=")
        .next()
        .map(|tail| {
            tail.split_whitespace()
                .next()
                .unwrap_or_default()
                .trim_end_matches(|c: char| c == '"' || c == ')')
                .to_string()
        })
        .filter(|t| !t.is_empty())
}

fn existing_window() -> Option<i64> {
    let windows = command_output(Command::new("niri").args(["msg", "--json", "windows"]))?;
    let parsed: Value = serde_json::from_str(&windows).ok()?;
    parsed.as_array()?.iter().find_map(|w| {
        if w.get("app_id").and_then(Value::as_str) == Some(APP_ID) {
            w.get("id").and_then(Value::as_i64)
        } else {
            None
        }
    })
}

fn main() {
    let force_pair = env::args().nth(1).as_deref() == Some("--pair");

    if !home().join(DSH_BIN).exists() {
        notify("dsh не найден в ~/.local/opt/dsh");
        std::process::exit(1);
    }

    // Launch/focus contract: re-running the entry focuses the window.
    if !force_pair {
        if let Some(id) = existing_window() {
            let _ = Command::new("niri")
                .args(["msg", "action", "focus-window", "--id", &id.to_string()])
                .status();
            return;
        }
    }

    if !service_running() {
        let started = Command::new("systemctl")
            .args(["--user", "start", SERVICE])
            .status()
            .ok()
            .map(|s| s.success())
            == Some(true);
        if !started {
            notify("Не удалось запустить пользовательскую службу dsh");
            std::process::exit(1);
        }
    }

    if !wait_port() {
        notify(&format!("Локальный сервер не запустился: journalctl --user -u {SERVICE}"));
        std::process::exit(1);
    }

    let paired = state_marker().exists();
    let mut url: url::Url = URL.parse().expect("static URL parses");
    if force_pair || !paired {
        if let Some(token) = latest_token() {
            url.set_query(Some(&format!("token={token}")));
            if let Some(dir) = state_marker().parent() {
                let _ = fs::create_dir_all(dir);
            }
            let _ = fs::write(state_marker(), b"");
        }
    }

    tauri::Builder::default()
        .setup(move |app| {
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url.clone()),
            )
            .title(WINDOW_TITLE)
            .inner_size(1280.0, 800.0)
            .min_inner_size(720.0, 480.0)
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
