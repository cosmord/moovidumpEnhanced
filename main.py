"""MooviDump Enhanced - main module

This module logs into a Moodle instance (via the mobile webservice), enumerates
the user's courses and downloads course files into a local `dumps/` folder.

Key features:
- Supports interactive or .env-based credential modes (handled by `run.py`).
- Uses a requests `Session` with retries and timeouts for robustness.
- Skips files already present unless `--force` is provided.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import re
import json
import logging
import argparse
import shutil
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import getpass
from rich.console import Console
from rich.table import Table

token = None
private_token = None
private_access_key = None
user_id = None
HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "com.moodle.moodlemobile"}

# ========== CONFIG ==========
from dotenv import load_dotenv  # noqa: E402


# Network / runtime defaults
TIMEOUT = 30
RETRIES = 3
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_CHUNK_SIZE = 64 * 1024
DOWNLOAD_CONNECT_TIMEOUT = 15
DOWNLOAD_RETRY_ATTEMPTS = 2
DOWNLOAD_PROGRESS_EVERY_MB = 5
MAX_DOWNLOAD_WORKERS = 4

# Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
console = Console()

SITE = ""
WEBSERVICE_URL = ""
USERNAME = None
PASSWORD = None
FORCE_DOWNLOAD = False
DUMP_ALL = False
FULL_SANITIZER = False


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="MooviDump Enhanced")
    p.add_argument("--force", action="store_true", help="Force re-download of files even if present")
    p.add_argument("--verbose", action="store_true", help="Verbose logging (debug)")
    p.add_argument("--all-courses", action="store_true", help="Download all visible courses without prompting")
    p.add_argument(
        "--courses",
        type=str,
        default="",
        help="Comma-separated list of course indexes/IDs to download without prompting",
    )
    p.add_argument(
        "--report",
        type=str,
        default="",
        help="Write a JSON summary report to the given path after the run finishes",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=MAX_DOWNLOAD_WORKERS,
        help="Maximum number of parallel file downloads",
    )
    p.add_argument(
        "--list-courses",
        action="store_true",
        help="Print visible courses as JSON and exit",
    )
    return p.parse_args(argv)


def parse_course_selection(selection: str, visible_courses: list[dict]) -> list[int]:
    """Convert a comma-separated selection into course IDs.

    Values can be 1-based visible course indexes or explicit course IDs.
    Invalid tokens are ignored with a warning.
    """
    selected_ids: list[int] = []
    seen_ids: set[int] = set()

    for raw_value in [part.strip() for part in selection.split(",") if part.strip()]:
        resolved_id = None

        if raw_value.isdigit():
            index = int(raw_value)
            if 1 <= index <= len(visible_courses):
                resolved_id = visible_courses[index - 1].get("id")
            else:
                resolved_id = int(raw_value)
        else:
            try:
                resolved_id = int(raw_value)
            except ValueError:
                logger.warning("Ignoring invalid course selector: %s", raw_value)

        if resolved_id is None:
            continue

        if resolved_id not in seen_ids:
            seen_ids.add(resolved_id)
            selected_ids.append(resolved_id)

    return selected_ids


def write_run_report(report_path: Path, report_data: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")


def course_display_name(course: dict) -> str:
    full_name = course.get("fullname", "") or ""
    return (full_name.split(":", 1)[1].strip() if ":" in full_name else full_name.strip()) or f"course_{course.get('id')}"


def visible_course_payload(course: dict) -> dict:
    return {
        "id": course.get("id"),
        "fullname": course.get("fullname", ""),
        "display_name": course_display_name(course),
    }


def prompt_for_credentials():
    """Prompt the user for site, username and password and export them to os.environ.

    This helper is used when the user chooses to provide credentials interactively
    instead of using `.env` or `example.env`.
    """
    site = input("MOODLE site URL (e.g. https://moovi.uvigo.gal): ").strip()
    username = input("MOODLE username: ").strip()
    password = getpass.getpass("MOODLE password: ")
    if site:
        os.environ["MOODLE_SITE"] = site
    if username:
        os.environ["MOODLE_USERNAME"] = username
    if password:
        os.environ["MOODLE_PASSWORD"] = password


def choose_config():
    """Load configuration from environment or prompt the user.

    Behavior:
    - If `MOODLE_*` variables are already present (e.g. from `.env`), they are used.
    - If an `example.env` file exists, the user can choose to load it or enter
      temporary credentials.
    - Placeholders in `example.env` force an interactive prompt to avoid accidental
      use of example values.
    """
    ex = Path("example.env")
    # Load local .env first (created by setup) so existing credentials are respected
    load_dotenv()
    # If all required env vars are already provided, use them (non-interactive/CI)
    if os.getenv("MOODLE_SITE") and os.getenv("MOODLE_USERNAME") and os.getenv("MOODLE_PASSWORD"):
        logger.info("Using credentials from environment/.env.")
        return

    if ex.exists():
        console.print("Se ha detectado `example.env`. Elige una opción:", style="yellow")
        console.print("  1) Usar example.env (valores de ejemplo).", style="dim")
        console.print("  2) Introducir credenciales para acceso temporal (no se guardan).", style="dim")
        choice = input("Opción [1/2]: ").strip() or "1"
        if choice == "1":
            load_dotenv(dotenv_path=ex)
            # If example.env contains placeholders, force prompt for credentials
            env_site = os.getenv("MOODLE_SITE", "")
            env_user = os.getenv("MOODLE_USERNAME", "")
            env_pass = os.getenv("MOODLE_PASSWORD", "")
            placeholders = ("PLACEHOLDER", "username", "password", "YOUR_")
            if (
                (not env_site)
                or (not env_user)
                or (not env_pass)
                or any(p in env_user.upper() for p in [ph.upper() for ph in placeholders])
                or any(p in env_pass.upper() for p in [ph.upper() for ph in placeholders])
            ):
                console.print("El example.env contiene valores de ejemplo o vacíos. Introduce credenciales reales:", style="red")
                prompt_for_credentials()
            else:
                logger.info("Cargado example.env.")
        else:
            prompt_for_credentials()
    else:
        logger.info("No se encontró example.env. Introduce credenciales para acceso temporal.")
        prompt_for_credentials()


def create_http_session() -> requests.Session:
    session_obj = requests.Session()
    retries = Retry(
        total=RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
    )  # type: ignore
    adapter = HTTPAdapter(max_retries=retries)
    session_obj.mount("https://", adapter)
    session_obj.mount("http://", adapter)
    session_obj.headers.update(HEADERS)
    return session_obj


# Setup requests session with retries
session = create_http_session()

COURSE_ALIASES = {
    1678: "FMI",
    1679: "AM",
    1680: "PROI",
    1681: "SD",
    1682: "TCL",
    1683: "AL",
    1684: "AEDI I",
    1685: "ACI I",
    1686: "PROII",
    1687: "Estadística",
    1689: "Sistemas Operativos I",
    1690: "Enxeñería de Software I",
    1691: "ACII",
    1692: "Sistemas Operativos II",
    1693: "Redes de Computadores I",
    1694: "Enxeñaría do Software II",
    1695: "Bases de datos I",
    1696: "Arquitecturas paralelas",
    1697: "Lóxica para a computación",
    1698: "Redes de computadoras II",
    1699: "Bases de datos II",
    1700: "Interfaces de usuario",
    1701: "Centros de datos",
    1702: "Dirección e xestión de proxectos",
    1703: "Teoría de autómatas e linguaxes formais",
    1704: "Concorrencia e distribución",




    # TODO add all the courses you want to dump here, using their course ID as key and the desired folder name as value.
    # If a course is not listed here, it will use its full name (sanitized) as folder name.
    # ...
}
# ========== CONFIG ==========


def sanitize(name, max_len=80):
    s = str(name).strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)
    if FULL_SANITIZER:
        s = re.sub(r"\s+", "_", s)
    s = s.rstrip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "item"


# /tokenpluginfile.php/{private_access_key}/{context_id}/mod_{"resource"|"folder"}/content/0/{file_name}
def pluginfile_to_token_url(file_url, private_access_key):
    if not file_url or not private_access_key:
        return None
    parsed = urlparse(file_url)
    new_path = parsed.path.replace("/webservice/pluginfile.php/", f"/tokenpluginfile.php/{private_access_key}/", 1)
    return urlunparse(parsed._replace(path=new_path, query=""))


def remove_empty_dirs(root_path):
    """Recorre `root_path` de forma descendente y elimina directorios vacíos.

    Devuelve el número de directorios eliminados.
    """
    root = Path(root_path)
    if not root.exists():
        return 0

    removed = 0
    # Recorremos en modo bottom-up para eliminar subdirectorios antes que padres
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        try:
            # si no hay entradas (ni ficheros ni subdirs), rmdir funciona
            if not any(p.iterdir()):
                p.rmdir()
                logger.info("Removed empty directory: %s", p)
                removed += 1
        except Exception as e:
            logger.debug("Could not remove %s: %s", p, e)
    return removed


def collapse_single_file_dirs(root_path, min_depth=3):
    """Aplana carpetas hoja con un unico archivo moviendolo al directorio padre.

    Solo colapsa directorios con profundidad relativa >= ``min_depth`` para
    evitar mover carpetas de nivel superior (por ejemplo, las de curso).

    Devuelve el numero de carpetas colapsadas.
    """
    root = Path(root_path)
    if not root.exists():
        return 0

    collapsed = 0

    for dirpath, _, _ in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue

        rel_depth = len(p.relative_to(root).parts)
        if rel_depth < min_depth:
            continue

        try:
            entries = list(p.iterdir())
        except Exception as e:
            logger.debug("Could not inspect %s: %s", p, e)
            continue

        files = [e for e in entries if e.is_file()]
        dirs = [e for e in entries if e.is_dir()]

        if len(files) != 1 or dirs:
            continue

        src = files[0]
        dst = p.parent / src.name

        if dst.exists():
            logger.warning("Cannot collapse %s; destination exists: %s", p, dst)
            continue

        try:
            shutil.move(str(src), str(dst))
            p.rmdir()
            logger.info("Collapsed single-file directory: %s -> %s", p, dst)
            collapsed += 1
        except Exception as e:
            logger.debug("Could not collapse %s: %s", p, e)

    return collapsed


def download_to_path(download_url, target_path, request_session=None, resume=True):
    """Descarga robusta a disco usando streaming y archivo temporal.

    Devuelve (ok, bytes_written). En caso de fallo limpia el temporal para evitar
    ficheros corruptos parciales.
    """
    temp_path = target_path.with_suffix(f"{target_path.suffix}.part")
    http_session = request_session or session

    if not resume and temp_path.exists():
        try:
            temp_path.unlink()
        except Exception:
            pass

    existing_size = temp_path.stat().st_size if resume and temp_path.exists() else 0

    for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
        try:
            headers = {}
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            with http_session.get(
                download_url,
                headers=headers,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT),
                stream=True,
            ) as response:
                if response.status_code not in (200, 206):
                    logger.warning(
                        "HTTP %s when downloading %s (attempt %d/%d)",
                        response.status_code,
                        target_path.name,
                        attempt,
                        DOWNLOAD_RETRY_ATTEMPTS,
                    )
                    continue

                append_mode = response.status_code == 206 and existing_size > 0
                if existing_size > 0 and not append_mode:
                    logger.debug("Server did not resume %s; restarting download from scratch", target_path.name)
                    existing_size = 0

                content_length = response.headers.get("Content-Length", "").strip()
                expected_size = int(content_length) if content_length.isdigit() else None
                if append_mode and expected_size is not None:
                    expected_size += existing_size
                if expected_size is not None and expected_size > 0:
                    logger.info(
                        "Starting %s (%.2f MB)",
                        target_path.name,
                        expected_size / (1024 * 1024),
                    )

                bytes_written = existing_size if append_mode else 0
                last_progress_bytes = 0
                start_time = time.monotonic()

                with open(temp_path, "ab" if append_mode else "wb") as f:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        bytes_written += len(chunk)

                        if bytes_written - last_progress_bytes >= DOWNLOAD_PROGRESS_EVERY_MB * 1024 * 1024:
                            elapsed = max(time.monotonic() - start_time, 0.001)
                            speed_mb_s = (bytes_written / (1024 * 1024)) / elapsed
                            logger.info(
                                "Downloading %s: %.2f MB written (%.2f MB/s)",
                                target_path.name,
                                bytes_written / (1024 * 1024),
                                speed_mb_s,
                            )
                            last_progress_bytes = bytes_written

                if expected_size is not None and bytes_written != expected_size:
                    logger.warning(
                        "Size mismatch for %s (expected %s bytes, got %s bytes) (attempt %d/%d)",
                        target_path.name,
                        expected_size,
                        bytes_written,
                        attempt,
                        DOWNLOAD_RETRY_ATTEMPTS,
                    )
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue

                temp_path.replace(target_path)
                return True, bytes_written
        except requests.exceptions.Timeout:
            logger.warning("Timeout downloading %s (attempt %d/%d)", target_path.name, attempt, DOWNLOAD_RETRY_ATTEMPTS)
        except requests.exceptions.ConnectionError:
            logger.warning("Connection error downloading %s (attempt %d/%d)", target_path.name, attempt, DOWNLOAD_RETRY_ATTEMPTS)
        except requests.exceptions.RequestException as e:
            logger.warning("Request error downloading %s (attempt %d/%d): %s", target_path.name, attempt, DOWNLOAD_RETRY_ATTEMPTS, e)
        except OSError as e:
            logger.warning("Filesystem error writing %s: %s", target_path, e)
            break
        except Exception as e:
            logger.exception("Unexpected error downloading %s: %s", target_path.name, e)

        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return False, 0


def login(username, password):
    global token, private_token

    logger.info("Attempting login to %s...", SITE)
    try:
        response = session.post(
            f"{SITE}/login/token.php?lang=en",
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
            timeout=TIMEOUT,
        )

        if response.status_code == 200:
            try:
                data = response.json()
            except json.JSONDecodeError:
                logger.error("Login: invalid JSON response")
                return False

            # Check for Moodle error responses
            if "error" in data:
                logger.error("Login error: %s", data.get('error', 'Unknown error'))
                if "errorcode" in data:
                    logger.debug("Error code: %s", data['errorcode'])
                if "stacktrace" in data:
                    logger.debug("Details: %s", data.get('message', ''))
                return False

            token = data.get("token")
            private_token = data.get("privatetoken")

            if not token:
                logger.error("No token received from server")
                logger.debug("Response data: %s", json.dumps(data, indent=2))
                return False

            logger.info("Token received: %s...", token[:20])
            return True
        else:
            logger.error("HTTP error %s: %s", response.status_code, response.text)
            return False
    except requests.exceptions.Timeout:
        logger.error("Connection timeout. Check your internet connection and MOODLE_SITE URL.")
        return False
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to %s. Check the URL in your .env file.", SITE)
        return False
    except Exception as e:
        logger.exception("Unexpected error during login: %s", e)
        return False


def post_webservice(function, arguments=None):
    global token

    params = {"moodlewsrestformat": "json", "wsfunction": function, "wstoken": token}

    if arguments:
        params.update(arguments)

    try:
        response = session.post(
            WEBSERVICE_URL,
            params=params,
            data={"moodlewssettingfilter": "true", "moodlewssettingfileurl": "true", "moodlewssettinglang": "en"},
            timeout=TIMEOUT,
        )

        if response.status_code == 200:
            try:
                data = response.json()
            except json.JSONDecodeError:
                logger.error("Invalid JSON from webservice %s", function)
                return None

            # Check for Moodle error in response
            if isinstance(data, dict) and "exception" in data:
                logger.error("API Error calling %s: %s", function, data.get('exception', 'Unknown'))
                logger.debug("API message: %s", data.get('message', 'No message'))
                if "errorcode" in data:
                    logger.debug("Error code: %s", data['errorcode'])
                return None

            return data
        else:
            logger.error("HTTP %s calling %s: %s", response.status_code, function, response.text[:200])
            return None
    except requests.exceptions.Timeout:
        logger.error("Timeout calling %s", function)
        return None
    except Exception as e:
        logger.exception("Error calling %s: %s", function, e)
        return None


def get_site_info():
    return post_webservice("core_webservice_get_site_info")


def call_moodle_mobile_functions(requests_list):
    global token

    data = {
        "moodlewsrestformat": "json",
        "wsfunction": "tool_mobile_call_external_functions",
        "wstoken": token,
        "moodlewssettinglang": "en",
    }

    for i, req in enumerate(requests_list):
        data[f"requests[{i}][function]"] = req["function"]
        data[f"requests[{i}][arguments]"] = json.dumps(req.get("arguments", {}))
        data[f"requests[{i}][settingfilter]"] = str(req.get("settingfilter", 1))
        data[f"requests[{i}][settingfileurl]"] = str(req.get("settingfileurl", 1))

    response = session.post(WEBSERVICE_URL, data=data, timeout=TIMEOUT)
    try:
        return response.json()
    except json.JSONDecodeError:
        logger.error("Invalid JSON from tool_mobile_call_external_functions")
        return None


def build_download_tasks(courses, selected_ids, dumps_dir, force_download):
    tasks = []
    skipped_count = 0
    failed_count = 0
    skipped_files: list[str] = []
    failed_files: list[dict[str, str]] = []
    processed_courses: list[dict[str, str | int]] = []

    for course in courses or []:
        if course.get("hidden"):
            continue

        course_id = course["id"]
        alias = COURSE_ALIASES.get(course_id)
        if alias:
            cleaned_name = alias
        else:
            full_name = course.get("fullname", "") or ""
            cleaned_name = (full_name.split(":", 1)[1].strip() if ":" in full_name else full_name.strip()) or f"course_{course_id}"

        folder_name = sanitize(cleaned_name)
        if DUMP_ALL:
            folder_name = f"{course_id}_{sanitize(cleaned_name)}"
        course_dir = dumps_dir / folder_name
        course_dir.mkdir(parents=True, exist_ok=True)
        processed_courses.append({"id": course_id, "name": cleaned_name, "folder": str(course_dir.relative_to(dumps_dir))})
        logger.info("Processing course [%s] %s", course_id, cleaned_name)
        logger.debug("Output directory: %s", course_dir)

        contents = post_webservice("core_course_get_contents", {"courseid": course_id})
        if not contents:
            logger.warning("No contents found for course %s", course_id)
            continue

        if DUMP_ALL:
            with open(course_dir / "contents.json", "w", encoding="utf-8") as f:
                json.dump(contents, f, indent=2, ensure_ascii=False)

        sections_root = course_dir / "sections" if DUMP_ALL else course_dir
        sections_root.mkdir(parents=True, exist_ok=True)

        for section in contents or []:
            section_number = section.get("section", 0)
            section_name = section.get("name")

            section_folder_name = sanitize(section_name or f"section_{section_number}")
            if DUMP_ALL:
                section_folder_name = f"{int(section_number):02d}_{sanitize(section_name or f'section_{section_number}') }"
            section_dir = sections_root / section_folder_name
            section_dir.mkdir(parents=True, exist_ok=True)

            if DUMP_ALL:
                with open(section_dir / "section.json", "w", encoding="utf-8") as f:
                    json.dump(section, f, indent=2, ensure_ascii=False)

            for module_index, module in enumerate(section.get("modules", [])):
                module_name = module.get("name")
                module_folder_name = sanitize(module_name or f"module_{module_index}")
                if DUMP_ALL:
                    module_folder_name = f"{module_index:03d}_{sanitize(module_name or f'module_{module_index}')}"
                module_dir = section_dir / module_folder_name
                module_dir.mkdir(parents=True, exist_ok=True)

                if DUMP_ALL:
                    with open(module_dir / "module.json", "w", encoding="utf-8") as f:
                        json.dump(module, f, indent=2, ensure_ascii=False)

                for content in module.get("contents", []):
                    if content.get("type") != "file":
                        continue

                    file_name = sanitize(content.get("filename") or "file")
                    target_path = module_dir / file_name
                    relative_target = str(target_path.relative_to(dumps_dir))

                    if target_path.exists() and not force_download:
                        logger.info("Skipping download; file already exists: %s", target_path)
                        skipped_count += 1
                        skipped_files.append(relative_target)
                        continue

                    file_url = content.get("fileurl")
                    download_url = pluginfile_to_token_url(file_url, private_access_key)
                    if not download_url:
                        logger.warning("Skipping download: missing access key or URL for %s", file_name)
                        failed_count += 1
                        failed_files.append({"path": relative_target, "reason": "missing_access_key_or_url"})
                        continue

                    tasks.append(
                        {
                            "download_url": download_url,
                            "target_path": target_path,
                            "relative_target": relative_target,
                            "file_name": file_name,
                            "resume": not force_download,
                        }
                    )

    return tasks, {
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        "processed_courses": processed_courses,
    }


def download_task_worker(task):
    worker_session = create_http_session()
    ok, bytes_written = download_to_path(
        task["download_url"],
        task["target_path"],
        request_session=worker_session,
        resume=task["resume"],
    )
    return {
        "ok": ok,
        "bytes_written": bytes_written,
        "relative_target": task["relative_target"],
        "file_name": task["file_name"],
    }


def execute_download_tasks(tasks, max_workers):
    downloaded_count = 0
    failed_count = 0
    downloaded_files: list[str] = []
    failed_files: list[dict[str, str]] = []

    if not tasks:
        return {
            "downloaded_count": 0,
            "failed_count": 0,
            "downloaded_files": downloaded_files,
            "failed_files": failed_files,
        }

    worker_count = max(1, min(max_workers, len(tasks)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(download_task_worker, task): task for task in tasks}
        for future in as_completed(future_map):
            task = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                failed_count += 1
                failed_files.append({"path": task["relative_target"], "reason": str(exc)})
                logger.exception("Unexpected error downloading %s", task["relative_target"])
                continue

            if result["ok"]:
                downloaded_count += 1
                downloaded_files.append(result["relative_target"])
                size_mb = result["bytes_written"] / (1024 * 1024)
                logger.info("Downloaded %s (%.2f MB)", result["file_name"], size_mb)
            else:
                failed_count += 1
                failed_files.append({"path": result["relative_target"], "reason": "download_failed"})

    return {
        "downloaded_count": downloaded_count,
        "failed_count": failed_count,
        "downloaded_files": downloaded_files,
        "failed_files": failed_files,
    }


def main(argv=None):
    global SITE, WEBSERVICE_URL, USERNAME, PASSWORD, FORCE_DOWNLOAD, DUMP_ALL, FULL_SANITIZER
    global private_access_key, user_id

    runtime_args = parse_args(argv)
    if runtime_args.verbose:
        logger.setLevel(logging.DEBUG)

    FORCE_DOWNLOAD = bool(getattr(runtime_args, "force", False))

    choose_config()

    SITE = os.getenv("MOODLE_SITE")
    if not SITE:
        logger.error("Missing MOODLE_SITE in environment.")
        sys.exit(1)

    SITE = SITE.rstrip("/")
    WEBSERVICE_URL = f"{SITE}/webservice/rest/server.php"
    USERNAME = os.getenv("MOODLE_USERNAME")
    PASSWORD = os.getenv("MOODLE_PASSWORD")

    if token is None:
        if USERNAME and PASSWORD:
            if login(USERNAME, PASSWORD):
                logger.info("login successful")
            else:
                logger.error("login failed")
                sys.exit(1)
        else:
            logger.error("Missing MOODLE_USERNAME or MOODLE_PASSWORD. Set them in .env.")
            sys.exit(1)
    else:
        logger.debug("using token: %s...", (token or "")[:20])

    logger.info("Fetching site info...")
    site_info = get_site_info()

    if site_info is None:
        logger.error("Failed to fetch site info. Check your credentials and permissions.")
        sys.exit(1)

    user_id = site_info.get("userid")
    private_access_key = site_info.get("userprivateaccesskey")

    if not user_id:
        logger.error("No user ID in site info")
        logger.debug("Site info: %s", json.dumps(site_info, indent=2))
        sys.exit(1)

    logger.info("User ID: %s", user_id)
    if private_access_key:
        logger.info("Private access key available (truncated): %s...", private_access_key[:20])
    else:
        logger.warning("No private access key (file downloads may fail)")

    logger.info("Fetching courses for user %s...", user_id)
    courses = post_webservice("core_enrol_get_users_courses", {"userid": user_id, "returnusercount": "0"})

    if not courses:
        logger.error("No courses found or error fetching courses.")
        sys.exit(1)

    logger.info("Found %d course(s)", len(courses))

    visible_courses = [c for c in courses if not c.get("hidden")]
    if runtime_args.list_courses:
        payload = {"courses": [visible_course_payload(course) for course in visible_courses]}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()
        return

    logger.info("Cursos disponibles:")
    table = Table(show_header=True, header_style="bold green")
    table.add_column("#", width=4)
    table.add_column("Course ID", width=10)
    table.add_column("Name")
    for i, c in enumerate(visible_courses, start=1):
        table.add_row(str(i), str(c.get("id")), course_display_name(c))
    console.print(table)

    selected_ids = []
    if runtime_args.all_courses:
        selected_ids = [c.get("id") for c in visible_courses]
        logger.info("Modo no interactivo: descargando todos los cursos visibles.")
    elif runtime_args.courses:
        selected_ids = parse_course_selection(runtime_args.courses, visible_courses)
        logger.info("Modo no interactivo: seleccionados %d curso(s).", len(selected_ids))
    else:
        choice = input("\nDescargar todos los cursos? [y/N]: ").strip().lower() or "n"
        if choice == "y":
            selected_ids = [c.get("id") for c in visible_courses]
        else:
            sel = input("Introduce números (1,2,3) o IDs separados por comas (vacío para cancelar): ").strip()
            if not sel:
                console.print("Operación cancelada.", style="yellow")
                sys.exit(0)
            selected_ids = parse_course_selection(sel, visible_courses)

    if not selected_ids:
        logger.warning("No hay cursos seleccionados. Finalizando.")
        sys.exit(0)

    selected_course_set = set(selected_ids)
    selected_courses = [c for c in courses if c.get("id") in selected_course_set]

    dumps_dir = Path("dumps")
    dumps_dir.mkdir(parents=True, exist_ok=True)

    tasks, preflight = build_download_tasks(selected_courses, selected_ids, dumps_dir, FORCE_DOWNLOAD)
    download_summary = execute_download_tasks(tasks, runtime_args.jobs)

    skipped_count = preflight["skipped_count"]
    failed_count = preflight["failed_count"] + download_summary["failed_count"]
    downloaded_count = download_summary["downloaded_count"]
    downloaded_files = download_summary["downloaded_files"]
    skipped_files = preflight["skipped_files"]
    failed_files = preflight["failed_files"] + download_summary["failed_files"]
    processed_courses = preflight["processed_courses"]

    logger.info("Colapsando carpetas de un solo archivo en %s...", dumps_dir)
    collapsed = collapse_single_file_dirs(dumps_dir, min_depth=3)
    logger.info("Carpetas colapsadas: %d", collapsed)

    logger.info("Eliminando carpetas vacías en %s...", dumps_dir)
    removed = remove_empty_dirs(dumps_dir)
    logger.info("Carpetas eliminadas: %d", removed)

    logger.info(
        "Resumen descarga -> descargados: %d, omitidos: %d, fallidos: %d",
        downloaded_count,
        skipped_count,
        failed_count,
    )

    if runtime_args.report:
        report_path = Path(runtime_args.report).expanduser()
        report_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "site": SITE,
            "dumps_dir": str(dumps_dir.resolve()),
            "selected_course_ids": selected_ids,
            "processed_courses": processed_courses,
            "counts": {
                "downloaded": downloaded_count,
                "skipped": skipped_count,
                "failed": failed_count,
                "collapsed_dirs": collapsed,
                "removed_empty_dirs": removed,
            },
            "files": {
                "downloaded": downloaded_files,
                "skipped": skipped_files,
                "failed": failed_files,
            },
        }
        write_run_report(report_path, report_data)
        logger.info("JSON report written to %s", report_path)


if __name__ == "__main__":
    main()
