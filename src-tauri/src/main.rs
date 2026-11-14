//! Casca desktop da Distopic.
//!
//! Abre uma janela com a tela de carregamento, inicia o backend Python como
//! processo filho e, quando ele responde, navega para a interface servida por
//! ele em http://127.0.0.1. O backend continua sendo a única fonte da interface.

// Sem janela de console nas versões de release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::net::{SocketAddr, TcpStream};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent, WebviewWindow};

const DEFAULT_PORT: u16 = 8000;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Processo do backend, para encerrá-lo junto com o aplicativo.
struct Backend(Mutex<Option<Child>>);

fn main() {
    let port = std::env::var("DISTOPIC_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(DEFAULT_PORT);

    let app = tauri::Builder::default()
        // Abrir o aplicativo de novo apenas traz a janela existente para a frente.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(Backend(Mutex::new(None)))
        .setup(move |app| {
            let window = app
                .get_webview_window("main")
                .expect("janela principal ausente");
            let handle = app.handle().clone();
            thread::spawn(move || start_backend(handle, window, port));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("falha ao iniciar o Distopic");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Backend>().0.lock().unwrap().take() {
                let _ = child.kill();
            }
        }
    });
}

/// Inicia o backend, espera a porta responder e leva a janela para a interface.
#[allow(unused_mut)]
fn start_backend(app: AppHandle, mut window: WebviewWindow, port: u16) {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let reachable = || TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok();

    if reachable() {
        return fail(
            &window,
            format!(
                "A porta {port} já está em uso. Feche o servidor da Distopic que está aberto \
                 em outro terminal (ou outro programa nessa porta) e abra o aplicativo de novo."
            ),
        );
    }

    match spawn_backend(port) {
        Ok(child) => *app.state::<Backend>().0.lock().unwrap() = Some(child),
        Err(error) => return fail(&window, format!("Não foi possível iniciar o backend: {error}")),
    }

    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if reachable() {
            // A interface pede a senha (ou a criação dela, no primeiro acesso).
            let url = format!("http://127.0.0.1:{port}/");
            if let Err(error) = window.navigate(url.parse().expect("URL local válida")) {
                fail(&window, format!("Falha ao abrir a interface: {error}"));
            }
            return;
        }
        let exited = {
            let backend = app.state::<Backend>();
            let mut guard = backend.0.lock().unwrap();
            matches!(guard.as_mut().map(|child| child.try_wait()), Some(Ok(Some(_))))
        };
        if exited {
            return fail(
                &window,
                format!(
                    "O backend encerrou ao iniciar. Veja o motivo em {}",
                    log_path().display()
                ),
            );
        }
        thread::sleep(Duration::from_millis(200));
    }
    fail(&window, "O backend não respondeu em 60 segundos.".to_string());
}

/// Pasta do backend no projeto, usada em desenvolvimento (`cargo run`).
fn backend_dir() -> PathBuf {
    std::env::var_os("DISTOPIC_BACKEND_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            manifest.parent().unwrap_or(&manifest).join("backend")
        })
}

fn log_path() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    base.join("Distopic").join("backend.log")
}

/// Backend empacotado, instalado ao lado do executável do aplicativo.
fn bundled_backend() -> Option<PathBuf> {
    let path = std::env::current_exe().ok()?.parent()?.join("distopic-backend.exe");
    path.is_file().then_some(path)
}

fn spawn_backend(port: u16) -> std::io::Result<Child> {
    // Instalado: usa o backend empacotado. Em desenvolvimento: o Python da .venv do projeto.
    let mut command = match bundled_backend() {
        Some(exe) => Command::new(exe),
        None => {
            let dir = backend_dir();
            let python = dir.join(".venv").join("Scripts").join("python.exe");
            if !python.is_file() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::NotFound,
                    format!("Backend não encontrado (procurei {})", python.display()),
                ));
            }
            let mut command = Command::new(python);
            command.args(["-m", "app"]).current_dir(dir);
            command
        }
    };

    let log = log_path();
    if let Some(parent) = log.parent() {
        fs::create_dir_all(parent)?;
    }
    let stdout = File::create(&log)?;
    let stderr = stdout.try_clone()?;

    command
        .env("DISTOPIC_PORT", port.to_string())
        // O backend se encerra sozinho quando este processo deixa de existir,
        // mesmo que o aplicativo trave ou seja finalizado à força.
        .env("DISTOPIC_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
}

/// Mostra o erro na tela de carregamento.
fn fail(window: &WebviewWindow, message: String) {
    // Dá tempo de a tela de carregamento terminar de abrir.
    thread::sleep(Duration::from_millis(600));
    let script = format!(
        "showError({})",
        serde_json::to_string(&message).unwrap_or_else(|_| "\"Erro\"".into())
    );
    let _ = window.eval(&script);
}
