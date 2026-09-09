#!/usr/bin/env python3
"""Launcher that lets the user choose between terminal, Python GUI, or browser mode."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path


logger = logging.getLogger("run")
DEFAULT_SITE = "https://moovi.uvigo.gal"


def parse_launcher_args() -> argparse.Namespace:
    """Parse optional launcher arguments to bypass interactive prompts."""
    parser = argparse.ArgumentParser(description="MooviDump Enhanced launcher")
    parser.add_argument("--mode", choices=["1", "2", "3"], help="Launcher mode: 1=terminal, 2=gui, 3=browser")
    parser.add_argument(
        "--terminal-cred-mode",
        choices=["1", "2", "3"],
        help="Credential mode for terminal launch: 1=stored .env, 2=temporary, 3=save",
    )
    return parser.parse_args()


def prompt_execution_mode() -> str:
    """Ask how the user wants to launch the app."""
    while True:
        logger.info("Elige el modo de ejecución:")
        logger.info("  1) Terminal")
        logger.info("  2) Interfaz gráfica Python")
        logger.info("  3) Navegador (local)")

        try:
            raw_choice = input("Modo [1/2/3]: ").strip()
        except KeyboardInterrupt:
            # Bubble up so main() can terminate cleanly with exit code 130.
            raise
        except EOFError:
            logger.error("\nNo se pudo leer entrada interactiva. Cerrando launcher.")
            sys.exit(1)

        choice = raw_choice or "1"
        # Be tolerant with pasted/noisy input, e.g. "1..." or "2 extra".
        if choice and choice[0] in {"1", "2", "3"}:
            return choice[0]

        if choice in {"1", "2", "3"}:
            return choice
        logger.error("Modo inválido. Usa 1, 2 o 3.")


def prompt_credentials(default_site: str = DEFAULT_SITE):
    """Prompt user for site, username and password. Returns tuple (site, username, password)."""
    site_input = input(f"   MOODLE_SITE [{default_site}]: ").strip()
    site = site_input if site_input else default_site

    username = input("   MOODLE_USERNAME: ").strip()
    if not username:
        logger.error("   ❌ Username is required!")
        return None

    password = getpass.getpass("   MOODLE_PASSWORD: ")
    if not password:
        logger.error("   ❌ Password is required!")
        return None

    return site, username, password


def write_env_file(env_file: Path, site: str, username: str, password: str | None = None) -> bool:
    """Write `.env`. If password is None, write empty password field (not saved)."""
    try:
        def quote_env_value(value: str) -> str:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
            return f'"{escaped}"'

        pw_field = quote_env_value(password) if password is not None else '""'
        env_content = f"""MOODLE_SITE={quote_env_value(site)}
MOODLE_USERNAME={quote_env_value(username)}
MOODLE_PASSWORD={pw_field}
"""
        env_file.write_text(env_content, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("   ❌ Error creating .env file: %s", exc)
        return False


def install_requirements(script_dir: Path) -> None:
    """Install Python dependencies for terminal and GUI modes."""
    logger.info("\n[1/2] Installing dependencies...")
    try:
        # Prefer uv's native sync flow when available; fallback to pip for compatibility.
        uv_check = subprocess.run(["uv", "--version"], cwd=script_dir, capture_output=True, text=True, check=False)
        if uv_check.returncode == 0:
            result = subprocess.run(["uv", "sync"], cwd=script_dir, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                logger.info("✓ Dependencies synced successfully with uv")
                return
            logger.warning("uv sync failed; falling back to pip install")
            logger.debug(result.stderr)

        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=script_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            logger.info("✓ Dependencies installed successfully with pip")
        else:
            logger.warning("Some dependencies may have failed to install")
            logger.debug(result.stderr)
    except KeyboardInterrupt:
        logger.error("Instalación interrumpida por el usuario.")
        sys.exit(130)
    except Exception as exc:
        logger.error("❌ Error installing dependencies: %s", exc)
        sys.exit(1)


def prompt_terminal_credential_mode(script_dir: Path, preset_mode: str | None = None):
    """Handle the old three-way credential flow for terminal launches."""
    env_file = script_dir / ".env"

    logger.info("\nCredenciales para ejecución en terminal:")
    logger.info("  1) Usar credenciales almacenadas en .env (si existe).")
    logger.info("  2) Introducir credenciales para esta ejecución (no guardar).")
    logger.info("  3) Introducir credenciales y guardarlas en .env.")
    if preset_mode in {"1", "2", "3"}:
        mode = preset_mode
        logger.info("Modo de credenciales seleccionado por argumento: %s", mode)
    else:
        mode = input("Modo [1/2/3]: ").strip() or "1"

    pw_for_run = None
    env_vars = None

    if mode == "1":
        if env_file.exists():
            logger.info("✓ .env file found; using stored credentials.")
        else:
            logger.info(".env no encontrada. Vamos a crearla ahora.")
            creds = prompt_credentials()
            if not creds:
                sys.exit(1)
            site, username, password = creds
            save = input("Guardar credenciales en .env? [y/N]: ").strip().lower() == "y"
            if save:
                if not write_env_file(env_file, site, username, password):
                    sys.exit(1)
                logger.info("\n✓ .env file created and credentials saved.")
            else:
                if not write_env_file(env_file, site, username, None):
                    sys.exit(1)
                pw_for_run = password
                logger.info("\n✓ .env file created (password not saved).")
    elif mode == "2":
        creds = prompt_credentials()
        if not creds:
            sys.exit(1)
        site, username, password = creds
        env_vars = {"MOODLE_SITE": site, "MOODLE_USERNAME": username, "MOODLE_PASSWORD": password}
        logger.info("Using temporary credentials for this run (not saved).")
    elif mode == "3":
        creds = prompt_credentials()
        if not creds:
            sys.exit(1)
        site, username, password = creds
        if not write_env_file(env_file, site, username, password):
            sys.exit(1)
        logger.info("\n✓ .env file created and credentials saved.")
    else:
        logger.error("Modo inválido.")
        sys.exit(1)

    return env_vars, pw_for_run


def launch_terminal(script_dir: Path, env: dict[str, str]) -> int:
    """Launch the existing CLI workflow in main.py."""
    logger.info("\n[2/2] Running main.py in terminal mode...")
    logger.info("-" * 60)

    try:
        force = input("Forzar redescarga de archivos existentes? [y/N]: ").strip().lower() == "y"
        cmd = [sys.executable, "main.py"]
        if force:
            cmd.append("--force")
        logger.info("Launching main.py... %s", "(force)" if force else "")
        result = subprocess.run(cmd, cwd=script_dir, check=False, env=env)
        logger.info("main.py exited with code %s", result.returncode)
        return int(result.returncode or 0)
    except Exception as exc:
        logger.error("❌ Error running main.py: %s", exc)
        return 1


def launch_gui(script_dir: Path, env: dict[str, str]) -> int:
    """Launch the Python GUI launcher."""
    logger.info("\nLanzando interfaz gráfica Python...")
    try:
        result = subprocess.run([sys.executable, "run_gui.py"], cwd=script_dir, check=False, env=env)
        logger.info("run_gui.py exited with code %s", result.returncode)
        return int(result.returncode or 0)
    except Exception as exc:
        logger.error("❌ Error launching GUI: %s", exc)
        return 1


def launch_browser(script_dir: Path, env: dict[str, str]) -> int:
    """Launch a local browser-based frontend if the web workspace exists."""
    import queue
    import re
    import socket
    import threading
    import time

    def ensure_pnpm_available() -> bool:
        """Ensure pnpm exists in PATH; try installing it with npm if missing."""
        try:
            check = subprocess.run(["pnpm", "--version"], cwd=web_dir, check=False, capture_output=True, text=True)
            if check.returncode == 0:
                return True
        except FileNotFoundError:
            pass

        logger.warning("`pnpm` no está disponible. Intentando instalarlo automáticamente...")

        try:
            npm_check = subprocess.run(["npm", "--version"], cwd=web_dir, check=False, capture_output=True, text=True)
        except FileNotFoundError:
            logger.error("`npm` tampoco está disponible en PATH.")
            logger.info("Instala Node.js (incluye npm) y vuelve a intentarlo: https://nodejs.org")
            return False

        if npm_check.returncode != 0:
            logger.error("No se pudo ejecutar `npm` correctamente para instalar `pnpm`.")
            logger.debug(npm_check.stderr)
            return False

        logger.info("Instalando `pnpm` globalmente con npm...")
        install = subprocess.run(["npm", "install", "-g", "pnpm"], cwd=web_dir, check=False)
        if install.returncode != 0:
            logger.error("Falló la instalación automática de `pnpm`.")
            return False

        try:
            recheck = subprocess.run(["pnpm", "--version"], cwd=web_dir, check=False, capture_output=True, text=True)
            if recheck.returncode == 0:
                logger.info("✓ `pnpm` instalado correctamente")
                return True
        except FileNotFoundError:
            pass

        logger.error("`pnpm` se instaló pero aún no está disponible en esta sesión.")
        logger.info("Cierra y abre de nuevo la terminal, luego vuelve a ejecutar el launcher.")
        return False

    def collect_open_ports() -> set[int]:
        """Collect currently open local ports in the expected dev-server range."""
        open_ports: set[int] = set()
        for p in range(3000, 3010):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(0.15)
                if sock.connect_ex(("127.0.0.1", p)) == 0:
                    open_ports.add(p)
            finally:
                sock.close()
        return open_ports

    def start_stream_reader(stream, lines_queue: queue.Queue[str], stream_name: str) -> None:
        """Read process stream asynchronously to prevent pipe buffer blocking."""

        def _reader() -> None:
            try:
                for line in iter(stream.readline, ""):
                    if line:
                        lines_queue.put(f"[{stream_name}] {line.rstrip()}")
            finally:
                stream.close()

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

    web_dir = None
    for candidate in ("web-server", "web", "frontend"):
        candidate_path = script_dir / candidate
        if candidate_path.exists():
            web_dir = candidate_path
            break

    if web_dir is None:
        logger.error(
            "Modo navegador aún no está preparado. Crea una carpeta web-server/ (o web/) con el frontend local antes de usarlo."
        )
        return 1

    if not ensure_pnpm_available():
        return 1

    node_modules = web_dir / "node_modules"
    if not node_modules.exists():
        logger.info("Instalando dependencias del frontend web...")
        result = subprocess.run(["pnpm", "install"], cwd=web_dir, check=False)
        if result.returncode != 0:
            logger.error("No se pudieron instalar las dependencias del frontend web.")
            return int(result.returncode or 1)

    logger.info("Arrancando servidor web local (auto-detectando puerto disponible)...")

    open_ports_before = collect_open_ports()

    try:
        process = subprocess.Popen(
            ["pnpm", "run", "dev"],
            cwd=web_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        logger.error("No se pudo ejecutar `pnpm run dev` en esta sesión de terminal.")
        logger.info("Reinicia la terminal y vuelve a ejecutar el launcher.")
        return 1

    lines_queue: queue.Queue[str] = queue.Queue()
    recent_lines: list[str] = []
    if process.stdout:
        start_stream_reader(process.stdout, lines_queue, "stdout")
    if process.stderr:
        start_stream_reader(process.stderr, lines_queue, "stderr")

    # Auto-detect port from server output or scan for available port
    port = None
    port_pattern = re.compile(r"http://(?:127\.0\.0\.1|localhost):(\d+)")
    start_time = time.time()
    max_wait = 30

    while time.time() - start_time < max_wait and port is None:
        try:
            while True:
                line = lines_queue.get_nowait()
                recent_lines.append(line)
                if len(recent_lines) > 20:
                    recent_lines = recent_lines[-20:]
                match = port_pattern.search(line)
                if match:
                    port = int(match.group(1))
                    break

        except queue.Empty:
            pass
        except Exception:
            pass

        if port is None:
            newly_open_ports = sorted(collect_open_ports() - open_ports_before)
            if len(newly_open_ports) == 1:
                port = newly_open_ports[0]

        if process.poll() is not None and port is None:
            break

        time.sleep(0.2)

    if port is None:
        logger.error("Error: No se pudo determinar el puerto del servidor web")
        if recent_lines:
            logger.error("Salida reciente del servidor:")
            for line in recent_lines[-5:]:
                logger.error("  %s", line)
        if process.poll() is None:
            process.kill()
        return 1

    web_url = f"http://127.0.0.1:{port}"
    logger.info("✓ Servidor web listo en %s", web_url)
    webbrowser.open(web_url)
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        logger.warning("\nEjecución interrumpida por el usuario. Cerrando servidor web...")
        if process.poll() is None:
            process.terminate()
        return 130
    logger.info("Servidor web finalizado con código %s", return_code)
    return int(return_code or 0)


def main() -> None:
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        args = parse_launcher_args()
        logger.info("MooviDump Enhanced - Launcher")
        execution_mode = args.mode or prompt_execution_mode()

        env = os.environ.copy()

        if execution_mode == "1":
            logger.info("Iniciando flujo de terminal...")
            env_vars, pw_for_run = prompt_terminal_credential_mode(script_dir, args.terminal_cred_mode)
            install_requirements(script_dir)
            if env_vars:
                env.update(env_vars)
            if pw_for_run:
                env["MOODLE_PASSWORD"] = pw_for_run
            sys.exit(launch_terminal(script_dir, env))

        if execution_mode == "2":
            logger.info("Iniciando flujo de GUI Python...")
            install_requirements(script_dir)
            sys.exit(launch_gui(script_dir, env))

        logger.info("Iniciando flujo de navegador local...")
        sys.exit(launch_browser(script_dir, env))
    except KeyboardInterrupt:
        logger.warning("\nEjecución interrumpida por el usuario.")
        sys.exit(130)


if __name__ == "__main__":
    main()
