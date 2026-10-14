//! RunneOS shell for the DeepSeek Harness web UI (dsh).
//!
//! Contract (mirrors the legacy bash wrapper): start the dsh user service on
//! demand, wait for the local port, pair the webview with the dsh access
//! token on first run, then open the UI in a native window. Re-running the
//! entry focuses the existing window; `--pair` forces token re-pairing. No
//! sandbox/TLS flags are changed anywhere: the dsh server keeps its own token
//! fence and binds to 127.0.0.1 only.
//!
//! Remote mode: `--remote <ssh-host>[,<fallback>...]` opens the dsh UI of
//! another machine instead of the local one. The shell brings up an SSH
//! tunnel to that machine's loopback-only dsh server and pairs through it,
//! so the remote server is never exposed on a network interface.
//!
//! Linux/WebKit note: cookies are keyed by host and ignore the port, so a
//! remote UI tunnelled to 127.0.0.1:3081 would share the pairing cookie with
//! the local UI on 127.0.0.1:3080 and the two sessions would fight over it.
//! The remote tunnel therefore listens on a second loopback address
//! (127.0.0.2), which keeps one cookie jar per machine.

use std::io::Read as _;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;
use std::{env, fs};

use serde_json::Value;

const SERVICE: &str = "runneos-deepseek-harness.service";
const LOCAL_HOST: &str = "127.0.0.1";
const LOCAL_PORT: u16 = 3080;
// Second loopback address for tunnelled remote servers (see module docs).
const REMOTE_HOST: &str = "127.0.0.2";
const REMOTE_PORT: u16 = 3080;
// Wayland app_id comes from the binary name (tao), not the Tauri identifier;
// StartupWMClass in the desktop entry must match this. The name is read at
// runtime so a second copy of the binary (e.g. ...-bigpc) gets its own
// app_id and its own window instead of focusing the local one.
const APP_ID_FALLBACK: &str = "runneos-deepseek-harness";
const WINDOW_TITLE: &str = "DeepSeek Harness";
const DSH_DIR: &str = ".local/opt/dsh";

struct Options {
    force_pair: bool,
    /// Candidate SSH hosts, tried in order. Empty means local mode.
    remotes: Vec<String>,
}

fn parse_args() -> Options {
    let mut opts = Options {
        force_pair: false,
        remotes: Vec::new(),
    };
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        let value = match arg.as_str() {
            "--pair" => {
                opts.force_pair = true;
                continue;
            }
            "--remote" => args.next().unwrap_or_default(),
            other => match other.strip_prefix("--remote=") {
                Some(rest) => rest.to_string(),
                None => continue,
            },
        };
        opts.remotes = value
            .split(',')
            .map(|h| h.trim().to_string())
            .filter(|h| !h.is_empty())
            .collect();
    }
    opts
}

fn app_id() -> String {
    env::current_exe()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| APP_ID_FALLBACK.to_string())
}

fn state_dir() -> PathBuf {
    let base = env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/state"));
    base.join("runneos/deepseek-harness")
}

/// Pairing state of the shell's own webview cookie store; the pairing cookie
/// itself lives in the WebKit data directory of the identifier. One marker
/// per target, so pairing locally or remotely does not mark the other done.
fn state_marker(remote: Option<&str>) -> PathBuf {
    match remote {
        Some(host) => state_dir().join(format!("paired-{host}")),
        None => state_dir().join("paired"),
    }
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
    cmd.stdout(Stdio::piped())
        .stderr(Stdio::null())
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

fn port_open(host: &str, port: u16) -> bool {
    TcpStream::connect((host, port)).is_ok()
}

fn wait_port(host: &str, port: u16, attempts: u32) -> bool {
    for _ in 0..attempts {
        if port_open(host, port) {
            return true;
        }
        thread::sleep(Duration::from_millis(200));
    }
    false
}

/// The dsh web UI prints its access-token URL to the unit journal at every
/// start; the token rotates, so always take the latest one.
fn token_from_journal(journal: &str) -> Option<String> {
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

fn latest_token() -> Option<String> {
    let journal = command_output(
        Command::new("journalctl").args(["--user", "-u", SERVICE, "--no-pager", "-n", "50"]),
    )?;
    token_from_journal(&journal)
}

/// Same, but the journal of the tunnelled machine, read over SSH.
fn remote_token(host: &str) -> Option<String> {
    let journal = command_output(Command::new("ssh").args([
        "-o",
        "BatchMode=yes",
        host,
        "journalctl",
        "--user",
        "-u",
        SERVICE,
        "--no-pager",
        "-n",
        "50",
    ]))?;
    token_from_journal(&journal)
}

/// Bring up `127.0.0.2:3080 -> <host>:127.0.0.1:3080`. `ExitOnForwardFailure`
/// makes ssh exit instead of hanging when the local port is taken.
fn spawn_tunnel(host: &str) -> Option<Child> {
    Command::new("ssh")
        .args([
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            &format!("{REMOTE_HOST}:{REMOTE_PORT}:{LOCAL_HOST}:{LOCAL_PORT}"),
            host,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .ok()
}

/// Try each candidate host until one answers with a working tunnel.
fn open_remote(candidates: &[String]) -> Option<(String, Child)> {
    for host in candidates {
        if let Some(child) = spawn_tunnel(host) {
            // A stale tunnel from an earlier run also opens the port; that is
            // fine, it points at the same remote port by construction.
            if wait_port(REMOTE_HOST, REMOTE_PORT, 20) {
                return Some((host.clone(), child));
            }
            let mut child = child;
            let _ = child.kill();
        }
    }
    None
}

fn existing_window(target: &str) -> Option<i64> {
    let windows = command_output(Command::new("niri").args(["msg", "--json", "windows"]))?;
    let parsed: Value = serde_json::from_str(&windows).ok()?;
    parsed.as_array()?.iter().find_map(|w| {
        if w.get("app_id").and_then(Value::as_str) == Some(target) {
            w.get("id").and_then(Value::as_i64)
        } else {
            None
        }
    })
}

fn dsh_local_dir() -> bool {
    // The remote target does not need a local dsh install at all: the shell
    // only opens a tunnel to it.
    home().join(DSH_DIR).exists()
}

fn main() {
    let opts = parse_args();
    let remote_target = if opts.remotes.is_empty() {
        None
    } else {
        Some(opts.remotes[0].clone())
    };
    let window_id = app_id();

    // Launch/focus contract: re-running the entry focuses the window.
    if !opts.force_pair {
        if let Some(id) = existing_window(&window_id) {
            let _ = Command::new("niri")
                .args(["msg", "action", "focus-window", "--id", &id.to_string()])
                .status();
            return;
        }
    }

    let (host, port, title, token, mut tunnel) = match remote_target {
        Some(_) => {
            let candidates = opts.remotes.clone();
            let (host, child) = match open_remote(&candidates) {
                Some(pair) => pair,
                None => {
                    notify(&format!(
                        "Не удалось открыть туннель к {}: проверьте ssh и доступность машины",
                        candidates.join(", ")
                    ));
                    std::process::exit(1);
                }
            };
            let token = remote_token(&host);
            (
                REMOTE_HOST.to_string(),
                REMOTE_PORT,
                format!("{WINDOW_TITLE} — {host}"),
                token,
                Some(child),
            )
        }
        None => {
            if !dsh_local_dir() {
                notify("dsh не найден в ~/.local/opt/dsh");
                std::process::exit(1);
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
            if !wait_port(LOCAL_HOST, LOCAL_PORT, 150) {
                notify(&format!(
                    "Локальный сервер не запустился: journalctl --user -u {SERVICE}"
                ));
                std::process::exit(1);
            }
            (
                LOCAL_HOST.to_string(),
                LOCAL_PORT,
                WINDOW_TITLE.to_string(),
                latest_token(),
                None,
            )
        }
    };

    let marker = state_marker(remote_target.as_deref());
    let paired = marker.exists();
    let mut url: url::Url = format!("http://{host}:{port}/").parse().expect("URL builds");
    if opts.force_pair || !paired {
        match token {
            Some(token) => {
                url.set_query(Some(&format!("token={token}")));
                if let Some(dir) = marker.parent() {
                    let _ = fs::create_dir_all(dir);
                }
                let _ = fs::write(&marker, b"");
            }
            None => notify("Токен доступа не найден в журнале — парное подключение пропущено"),
        }
    }

    let result = tauri::Builder::default()
        .setup(move |app| {
            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::External(url.clone()))
                .title(title.clone())
                .inner_size(1280.0, 800.0)
                .min_inner_size(720.0, 480.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!());

    // The tunnel lives exactly as long as this window.
    if let Some(child) = tunnel.as_mut() {
        let _ = child.kill();
        let _ = child.wait();
    }

    result.expect("error while running tauri application");
}
