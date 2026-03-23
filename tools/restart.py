#!/usr/bin/env python3

import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_APP_DIR = REPO_ROOT / ".hermes-proxy-runtime"
NODE_EXTRA_PATHS = ["/Users/money/.local/node/bin/node", "/opt/homebrew/bin/node", "/usr/local/bin/node"]
PNPM_EXTRA_PATHS = ["/Users/money/.local/node/bin/pnpm", "/opt/homebrew/bin/pnpm", "/usr/local/bin/pnpm"]
GO_EXTRA_PATHS = [
    "/opt/homebrew/bin/go",
    "/usr/local/go/bin/go",
    "/usr/local/bin/go",
    str(Path.home() / "go" / "bin" / "go"),
]
POSTGRES_EXTRA_PATHS = [
    "/opt/homebrew/bin/postgres",
    "/opt/homebrew/bin/pg_ctl",
    "/usr/local/bin/postgres",
    "/usr/local/bin/pg_ctl",
    "/opt/homebrew/opt/postgresql@17/bin/postgres",
    "/opt/homebrew/opt/postgresql@17/bin/pg_ctl",
    "/opt/homebrew/opt/postgresql@16/bin/postgres",
    "/opt/homebrew/opt/postgresql@16/bin/pg_ctl",
    "/opt/homebrew/opt/postgresql@15/bin/postgres",
    "/opt/homebrew/opt/postgresql@15/bin/pg_ctl",
    "/usr/local/opt/postgresql@17/bin/postgres",
    "/usr/local/opt/postgresql@17/bin/pg_ctl",
    "/usr/local/opt/postgresql@16/bin/postgres",
    "/usr/local/opt/postgresql@16/bin/pg_ctl",
    "/usr/local/opt/postgresql@15/bin/postgres",
    "/usr/local/opt/postgresql@15/bin/pg_ctl",
]
REDIS_EXTRA_PATHS = [
    "/opt/homebrew/bin/redis-server",
    "/opt/homebrew/bin/redis-cli",
    "/usr/local/bin/redis-server",
    "/usr/local/bin/redis-cli",
]
POSTGRES_APP_VERSIONS = ["18", "17", "16", "15"]
LOCAL_POSTGRES_GUARD_PORTS = (5432, 5433)


def print_step(message: str) -> None:
    print(f"[restart] {message}")


def fail(message: str) -> "NoReturn":
    print(f"[restart] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_app_dir(override: str) -> Path:
    if override:
        return Path(override).expanduser().resolve()

    if os.environ.get("HERMES_APP_DIR"):
        return Path(os.environ["HERMES_APP_DIR"]).expanduser().resolve()

    return DEFAULT_APP_DIR


def find_tool(cli_name: str, env_var: str, override: str, extra_paths: list[str]) -> str:
    candidates: list = []
    if override:
        candidates.append(override)
    if os.environ.get(env_var):
        candidates.append(os.environ[env_var])
    if shutil.which(cli_name):
        candidates.append(shutil.which(cli_name) or "")
    candidates.extend(extra_paths)

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)

    return ""


def resolve_tool(cli_name: str, env_var: str, override: str, extra_paths: list[str]) -> str:
    tool_path = find_tool(cli_name, env_var, override, extra_paths)
    if tool_path:
        return tool_path

    joined = ", ".join(extra_paths)
    fail(
        f"cannot find `{cli_name}`. Set `{env_var}` or pass `--{cli_name}-bin`. "
        f"Checked PATH and common locations: {joined}"
    )


def bootstrap_runtime_files(app_dir: Path) -> None:
    if not app_dir.exists():
        app_dir.mkdir(parents=True, exist_ok=True)
        print_step(f"created app dir: {app_dir}")


def command_output(command: list[str]) -> str:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return (result.stdout or result.stderr).strip()


def parse_major_minor(version_output: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\.(\d+)", version_output)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def missing_dependency_message(
    cli_name: str,
    env_var: str,
    cli_flag: str,
    extra_paths: list[str],
    install_hint: str,
) -> str:
    checked_locations = ", ".join(["PATH", *extra_paths])
    return (
        f"`{cli_name}`: not found. {install_hint}. "
        f"Set `{env_var}` or pass `--{cli_flag}` if it is installed elsewhere. "
        f"Checked {checked_locations}"
    )


def broken_dependency_message(cli_name: str, env_var: str, cli_flag: str, tool_path: str, command_hint: str) -> str:
    return (
        f"`{cli_name}`: found at `{tool_path}` but `{command_hint}` failed. "
        f"Reinstall it or point `{env_var}` / `--{cli_flag}` to a working binary"
    )


def outdated_dependency_message(cli_name: str, detected_version: str, minimum_version: str) -> str:
    return f"`{cli_name}`: version `{detected_version}` is too old. `{minimum_version}` or newer is required"


def missing_file_message(path: Path, hint: str) -> str:
    return f"`{path}`: not found. {hint}"


def frontend_dependencies_installed() -> bool:
    return (FRONTEND_DIR / "node_modules").exists()


def local_runtime_dependency_installed(display_name: str) -> bool:
    candidates: list[tuple[str, list[str]]] = []
    if display_name == "PostgreSQL":
        candidates = [
            ("postgres", postgres_extra_paths("postgres")),
            ("pg_ctl", postgres_extra_paths("pg_ctl")),
            ("psql", postgres_extra_paths("psql")),
        ]
    elif display_name == "Redis":
        candidates = [
            ("redis-server", REDIS_EXTRA_PATHS),
            ("redis-cli", REDIS_EXTRA_PATHS),
        ]

    for cli_name, extra_paths in candidates:
        if find_tool(cli_name, "", "", extra_paths):
            return True
    return False


def read_config_sections(config_path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    if not config_path.exists():
        return sections

    current_section = ""
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not raw_line.startswith((" ", "\t")):
            current_section = stripped[:-1] if stripped.endswith(":") else ""
            if current_section:
                sections.setdefault(current_section, {})
            continue

        if not current_section or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        sections[current_section][key.strip()] = value.strip().strip("'\"")

    return sections


def read_section_host_port(config_path: Path, section_name: str, default_host: str, default_port: int) -> tuple[str, int]:
    sections = read_config_sections(config_path)
    values = sections.get(section_name, {})
    host = values.get("host", default_host) or default_host
    port = default_port
    if values.get("port"):
        try:
            port = int(values["port"])
        except ValueError as exc:
            raise ValueError(f"invalid port in {config_path} -> {section_name}.port: {values['port']}") from exc
    return host, port


def read_database_settings(config_path: Path) -> dict[str, str]:
    values = read_config_sections(config_path).get("database", {})
    return {
        "host": values.get("host", "127.0.0.1") or "127.0.0.1",
        "port": values.get("port", "5432") or "5432",
        "user": values.get("user", "postgres") or "postgres",
        "password": values.get("password", "postgres"),
        "dbname": values.get("dbname", "hermes-proxy") or "hermes-proxy",
        "managed_by_runtime": values.get("managed_by_runtime", "true").lower(),
    }


def read_redis_settings(config_path: Path) -> dict[str, str]:
    values = read_config_sections(config_path).get("redis", {})
    return {
        "host": values.get("host", "127.0.0.1") or "127.0.0.1",
        "port": values.get("port", "6379") or "6379",
        "password": values.get("password", ""),
    }


def postgres_extra_paths(binary_name: str) -> list[str]:
    paths = [
        f"/opt/homebrew/bin/{binary_name}",
        f"/usr/local/bin/{binary_name}",
        f"/opt/homebrew/opt/postgresql@17/bin/{binary_name}",
        f"/opt/homebrew/opt/postgresql@16/bin/{binary_name}",
        f"/opt/homebrew/opt/postgresql@15/bin/{binary_name}",
        f"/usr/local/opt/postgresql@17/bin/{binary_name}",
        f"/usr/local/opt/postgresql@16/bin/{binary_name}",
        f"/usr/local/opt/postgresql@15/bin/{binary_name}",
    ]
    paths.extend(
        f"/Applications/Postgres.app/Contents/Versions/{version}/bin/{binary_name}"
        for version in POSTGRES_APP_VERSIONS
    )
    return paths


def resolve_pg_ctl_bin() -> str:
    return resolve_tool("pg_ctl", "PG_CTL_BIN", "", postgres_extra_paths("pg_ctl"))


def resolve_initdb_bin() -> str:
    return resolve_tool("initdb", "INITDB_BIN", "", postgres_extra_paths("initdb"))


def resolve_psql_bin() -> str:
    return resolve_tool("psql", "PSQL_BIN", "", postgres_extra_paths("psql"))


def resolve_redis_server_bin() -> str:
    return resolve_tool("redis-server", "REDIS_SERVER_BIN", "", REDIS_EXTRA_PATHS)


def is_local_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"", "127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}


def is_tcp_port_open(host: str, port: int, timeout_seconds: float = 0.5) -> bool:
    try:
        with socket.create_connection((probe_host(host), port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def collect_build_tool_issues(args: argparse.Namespace) -> list[str]:
    issues: list[str] = []

    node_path = find_tool("node", "NODE_BIN", args.node_bin, NODE_EXTRA_PATHS)
    if not node_path:
        issues.append(
            missing_dependency_message(
                "node",
                "NODE_BIN",
                "node-bin",
                NODE_EXTRA_PATHS,
                "Install Node.js 18+ first",
            )
        )
    else:
        node_version = command_output([node_path, "--version"])
        if not node_version:
            issues.append(broken_dependency_message("node", "NODE_BIN", "node-bin", node_path, "`node --version`"))
        elif parse_major_minor(node_version) < (18, 0):
            issues.append(outdated_dependency_message("node", node_version, "Node.js 18"))

    pnpm_path = find_tool("pnpm", "PNPM_BIN", args.pnpm_bin, PNPM_EXTRA_PATHS)
    if not pnpm_path:
        issues.append(
            missing_dependency_message(
                "pnpm",
                "PNPM_BIN",
                "pnpm-bin",
                PNPM_EXTRA_PATHS,
                "Install pnpm first (for example: `npm install -g pnpm` after installing Node.js 18+)",
            )
        )
    elif not command_output([pnpm_path, "--version"]):
        issues.append(broken_dependency_message("pnpm", "PNPM_BIN", "pnpm-bin", pnpm_path, "`pnpm --version`"))

    go_path = find_tool("go", "GO_BIN", args.go_bin, GO_EXTRA_PATHS)
    if not go_path:
        issues.append(
            missing_dependency_message(
                "go",
                "GO_BIN",
                "go-bin",
                GO_EXTRA_PATHS,
                "Install Go 1.21+ first",
            )
        )
    else:
        go_version = command_output([go_path, "version"])
        if not go_version:
            issues.append(broken_dependency_message("go", "GO_BIN", "go-bin", go_path, "`go version`"))
        elif parse_major_minor(go_version) < (1, 21):
            issues.append(outdated_dependency_message("go", go_version, "Go 1.21"))

    if not frontend_dependencies_installed():
        issues.append(
            f"frontend dependencies are not installed in `{FRONTEND_DIR}`. "
            f"Run `pnpm install` in `{FRONTEND_DIR}` first"
        )

    return issues


def collect_runtime_installation_issues() -> list[str]:
    issues: list[str] = []
    if not local_runtime_dependency_installed("PostgreSQL"):
        issues.append(
            "`PostgreSQL`: local installation not detected. Install PostgreSQL 15+ first and ensure `postgres`, `pg_ctl`, or `psql` is available"
        )
    if not local_runtime_dependency_installed("Redis"):
        issues.append(
            "`Redis`: local installation not detected. Install Redis 7+ first and ensure `redis-server` or `redis-cli` is available"
        )

    return issues


def collect_preflight_issues(
    args: argparse.Namespace,
    binary_path: Path,
    config_path: Path,
) -> list[str]:
    issues: list[str] = []

    if args.restart_only and not binary_path.exists():
        issues.append(missing_file_message(binary_path, "Run without `--restart-only` once to build it first"))

    if not config_path.exists():
        if args.restart_only:
            issues.append(
                missing_file_message(
                    config_path,
                    "Run once without `--restart-only` to enter the Setup Wizard, or create the config manually first",
                )
            )

    issues.extend(collect_runtime_installation_issues())

    if args.restart_only:
        return issues

    issues.extend(collect_build_tool_issues(args))
    return issues


def ensure_preflight_ready(
    args: argparse.Namespace,
    binary_path: Path,
    config_path: Path,
) -> None:
    missing = collect_preflight_issues(args, binary_path, config_path)
    if not missing:
        return

    fail("missing required dependencies before restart:\n  - " + "\n  - ".join(missing))


def run_command(command: list[str], cwd: Path) -> None:
    print_step(f"run: {' '.join(command)} (cwd={cwd})")
    subprocess.run(command, cwd=str(cwd), check=True)


def read_server_config(config_path: Path) -> tuple[str, int]:
    try:
        return read_section_host_port(config_path, "server", "127.0.0.1", 8080)
    except ValueError as exc:
        fail(str(exc))


def start_detached_process(command: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print_step(f"run detached: {' '.join(command)} (cwd={cwd})")
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


def listening_pids(port: int) -> list[int]:
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    result = subprocess.run(
        [lsof, "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def stop_existing_processes(port: int) -> None:
    pids = listening_pids(port)
    if not pids:
        print_step(f"no listening process found on port {port}")
        return

    print_step(f"stopping existing process(es) on port {port}: {', '.join(map(str, pids))}")
    for pid in pids:
        os.kill(pid, signal.SIGTERM)

    deadline = time.time() + 10
    while time.time() < deadline:
        remaining = [pid for pid in pids if process_exists(pid)]
        if not remaining:
            return
        time.sleep(0.2)

    remaining = [pid for pid in pids if process_exists(pid)]
    if remaining:
        print_step(f"force killing remaining process(es): {', '.join(map(str, remaining))}")
        for pid in remaining:
            os.kill(pid, signal.SIGKILL)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def probe_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "", "::"} else host


def wait_until_listening(host: str, port: int, timeout_seconds: float = 15) -> None:
    target = probe_host(host)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((target, port))
                return
            except OSError:
                time.sleep(0.3)
    fail(f"new process did not start listening on {target}:{port} within {timeout_seconds} seconds")


def wait_until_postgres_ready(
    psql_bin: str,
    host: str,
    port: int,
    user: str,
    password: str,
    dbname: str,
    timeout_seconds: float = 20,
) -> None:
    target = probe_host(host)
    deadline = time.time() + timeout_seconds
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)
    command = [
        psql_bin,
        "-h",
        target,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        dbname,
        "-Atqc",
        "select 1",
    ]
    while time.time() < deadline:
        result = subprocess.run(command, check=False, capture_output=True, text=True, env=env)
        if result.returncode == 0 and result.stdout.strip() == "1":
            return
        time.sleep(0.3)
    fail(f"postgres did not become ready on {target}:{port} within {timeout_seconds} seconds")


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_psql_command(psql_bin: str, host: str, port: int, sql: str, *, capture_output: bool = False) -> str:
    command = [
        psql_bin,
        "-h",
        probe_host(host),
        "-p",
        str(port),
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-Atqc",
        sql,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        fail(result.stderr.strip() or f"`psql` failed while executing SQL: {sql}")
    if capture_output:
        return result.stdout.strip()
    return ""


def bootstrap_local_postgres_database(
    psql_bin: str,
    host: str,
    port: int,
    user: str,
    password: str,
    dbname: str,
) -> None:
    role_name = sql_identifier(user)
    role_literal = sql_literal(user)

    run_psql_command(
        psql_bin,
        host,
        port,
        "\n".join(
            [
                "DO $$",
                "BEGIN",
                f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN",
                f"        CREATE ROLE {role_name} LOGIN;",
                "    END IF;",
                "END",
                "$$;",
            ]
        ),
    )

    if password:
        run_psql_command(
            psql_bin,
            host,
            port,
            f"ALTER ROLE {role_name} WITH LOGIN PASSWORD {sql_literal(password)};",
        )
    else:
        run_psql_command(psql_bin, host, port, f"ALTER ROLE {role_name} WITH LOGIN;")

    if dbname == "postgres":
        return

    database_literal = sql_literal(dbname)
    database_name = sql_identifier(dbname)
    exists = run_psql_command(
        psql_bin,
        host,
        port,
        f"SELECT 1 FROM pg_database WHERE datname = {database_literal};",
        capture_output=True,
    )
    if exists != "1":
        run_psql_command(psql_bin, host, port, f"CREATE DATABASE {database_name} OWNER {role_name};")


def read_local_database_counts(
    psql_bin: str,
    host: str,
    port: int,
    user: str,
    dbname: str,
    password: str,
) -> Optional[dict[str, int]]:
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)

    sql = """
    SELECT
      CASE WHEN to_regclass('public.users') IS NULL THEN 0 ELSE (SELECT COUNT(*) FROM users) END,
      CASE WHEN to_regclass('public.accounts') IS NULL THEN 0 ELSE (SELECT COUNT(*) FROM accounts) END,
      CASE WHEN to_regclass('public.api_keys') IS NULL THEN 0 ELSE (SELECT COUNT(*) FROM api_keys) END;
    """
    command = [
        psql_bin,
        "-h",
        probe_host(host),
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        dbname,
        "-v",
        "ON_ERROR_STOP=1",
        "-AtF",
        ",",
        "-c",
        sql,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        return None

    parts = result.stdout.strip().split(",")
    if len(parts) != 3:
        return None

    try:
        users, accounts, api_keys = [int(value) for value in parts]
    except ValueError:
        return None

    return {"users": users, "accounts": accounts, "api_keys": api_keys}


def local_database_selection_looks_wrong(current: dict[str, int], alternative: dict[str, int]) -> bool:
    current_operational = current["accounts"] + current["api_keys"]
    alternative_operational = alternative["accounts"] + alternative["api_keys"]

    if alternative_operational <= 0:
        return False

    if current["users"] == 0 and alternative["users"] > 0:
        return True

    if current_operational == 0 and alternative["users"] >= current["users"]:
        return True

    return False


def build_local_database_guard_issue(config_path: Path) -> Optional[str]:
    if os.environ.get("HERMES_SKIP_LOCAL_DB_GUARD") == "1":
        return None

    settings = read_database_settings(config_path)
    host = settings["host"]
    if not is_local_host(host):
        return None

    try:
        current_port = int(settings["port"])
    except ValueError:
        return None

    psql_bin = resolve_psql_bin()
    current = read_local_database_counts(
        psql_bin,
        host,
        current_port,
        settings["user"],
        settings["dbname"],
        settings["password"],
    )
    if not current:
        return None

    for candidate_port in LOCAL_POSTGRES_GUARD_PORTS:
        if candidate_port == current_port:
            continue
        if not is_tcp_port_open(host, candidate_port):
            continue

        alternative = read_local_database_counts(
            psql_bin,
            host,
            candidate_port,
            settings["user"],
            settings["dbname"],
            settings["password"],
        )
        if not alternative:
            continue

        if local_database_selection_looks_wrong(current, alternative):
            target = f"{probe_host(host)}:{current_port}/{settings['dbname']}"
            alternative_target = f"{probe_host(host)}:{candidate_port}/{settings['dbname']}"
            return (
                f"configured database {target} looks much emptier than local database {alternative_target} "
                f"(current: users={current['users']}, accounts={current['accounts']}, api_keys={current['api_keys']}; "
                f"alternative: users={alternative['users']}, accounts={alternative['accounts']}, api_keys={alternative['api_keys']}). "
                f"Update config.yaml if you meant to use the populated database, or set HERMES_SKIP_LOCAL_DB_GUARD=1 to bypass."
            )

    return None


def ensure_local_redis_running(app_dir: Path, config_path: Path) -> None:
    settings = read_redis_settings(config_path)
    host = settings["host"]
    try:
        port = int(settings["port"])
    except ValueError as exc:
        fail(f"invalid port in {config_path} -> redis.port: {settings['port']}")
        raise exc

    if not is_local_host(host):
        print_step(f"skip redis autostart for non-local host: {host}:{port}")
        return
    if is_tcp_port_open(host, port):
        print_step(f"redis already listening on {probe_host(host)}:{port}")
        return

    data_dir = (app_dir / "redis").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        resolve_redis_server_bin(),
        "--dir",
        str(data_dir),
        "--dbfilename",
        "dump.rdb",
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
        "--save",
        "60",
        "1",
        "--port",
        str(port),
    ]
    if settings["password"]:
        command.extend(["--requirepass", settings["password"]])

    log_path = app_dir / "data" / "redis.stdout.log"
    start_detached_process(command, app_dir, log_path)
    wait_until_listening(host, port)


def ensure_local_postgres_running(app_dir: Path, config_path: Path) -> None:
    settings = read_database_settings(config_path)
    host = settings["host"]
    try:
        port = int(settings["port"])
    except ValueError as exc:
        fail(f"invalid port in {config_path} -> database.port: {settings['port']}")
        raise exc

    if not is_local_host(host):
        print_step(f"skip postgres autostart for non-local host: {host}:{port}")
        return
    if is_tcp_port_open(host, port):
        print_step(f"postgres already listening on {probe_host(host)}:{port}")
        return

    pg_ctl_bin = resolve_pg_ctl_bin()
    initdb_bin = resolve_initdb_bin()
    psql_bin = resolve_psql_bin()
    data_dir = (app_dir / "postgres").resolve()
    log_path = (app_dir / "data" / "postgres.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not (data_dir / "PG_VERSION").exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        print_step(f"initializing postgres cluster in {data_dir}")
        subprocess.run(
            [
                initdb_bin,
                "-D",
                str(data_dir),
                "-U",
                "postgres",
                "--auth=trust",
            ],
            check=True,
        )

    print_step(f"starting postgres cluster from {data_dir} on port {port}")
    subprocess.run(
        [
            pg_ctl_bin,
            "-D",
            str(data_dir),
            "-l",
            str(log_path),
            "-o",
            f"-p {port} -c listen_addresses=localhost",
            "start",
        ],
        check=True,
    )
    wait_until_postgres_ready(
        psql_bin,
        host,
        port,
        settings["user"],
        settings["password"],
        settings["dbname"],
    )
    bootstrap_local_postgres_database(
        psql_bin,
        host,
        port,
        settings["user"],
        settings["password"],
        settings["dbname"],
    )


def ensure_runtime_services(app_dir: Path, config_path: Path) -> None:
    database_settings = read_database_settings(config_path)
    postgres_managed_by_runtime = database_settings.get("managed_by_runtime", "true") != "false"

    if postgres_managed_by_runtime:
        ensure_local_postgres_running(app_dir, config_path)
    else:
        host = database_settings["host"]
        try:
            port = int(database_settings["port"])
        except ValueError as exc:
            fail(f"invalid port in {config_path} -> database.port: {database_settings['port']}")
            raise exc
        if not is_tcp_port_open(host, port):
            fail(
                f"configured external postgres is not listening on {probe_host(host)}:{port}; "
                "restart.py is configured to not auto-start a runtime-managed PostgreSQL for this project"
            )
        print_step(f"external postgres already listening on {probe_host(host)}:{port}")

    ensure_local_redis_running(app_dir, config_path)
    issue = build_local_database_guard_issue(config_path)
    if issue:
        fail(issue)


def start_process(app_dir: Path, binary_path: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DATA_DIR"] = str(app_dir)

    print_step(f"starting {binary_path} (data_dir={app_dir}, log={log_path})")
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [str(binary_path)],
            cwd=str(app_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


def build_frontend(pnpm_bin: str) -> None:
    run_command([pnpm_bin, "build"], FRONTEND_DIR)


def build_backend(go_bin: str, binary_path: Path) -> None:
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([go_bin, "build", "-tags", "embed", "-o", str(binary_path), "./cmd/server"], BACKEND_DIR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild and restart the local hermes-proxy runtime binary.")
    parser.add_argument(
        "--restart-only",
        action="store_true",
        help="skip frontend/backend builds and only restart the current runtime binary",
    )
    parser.add_argument(
        "--app-dir",
        help="runtime state directory for the built binary, config.yaml, and logs (default: .hermes-proxy-runtime)",
    )
    parser.add_argument("--go-bin", help="path to the Go executable")
    parser.add_argument("--node-bin", help="path to the Node.js executable")
    parser.add_argument("--pnpm-bin", help="path to the pnpm executable")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_dir = resolve_app_dir(args.app_dir)
    binary_path = app_dir / "hermes-proxy"
    log_path = app_dir / "data" / "hermes-proxy.stdout.log"
    config_path = app_dir / "config.yaml"

    bootstrap_runtime_files(app_dir)
    ensure_preflight_ready(args, binary_path, config_path)
    ensure_runtime_services(app_dir, config_path)

    print_step(f"using app dir: {app_dir}")

    host, port = read_server_config(config_path)
    print_step(f"runtime config: host={host} port={port}")

    if not args.restart_only:
        pnpm_bin = resolve_tool(
            "pnpm",
            "PNPM_BIN",
            args.pnpm_bin,
            PNPM_EXTRA_PATHS,
        )
        go_bin = resolve_tool(
            "go",
            "GO_BIN",
            args.go_bin,
            GO_EXTRA_PATHS,
        )
        build_frontend(pnpm_bin)
        build_backend(go_bin, binary_path)
    elif not binary_path.exists():
        fail(f"binary not found: {binary_path}")

    stop_existing_processes(port)
    pid = start_process(app_dir, binary_path, log_path)
    wait_until_listening(host, port)
    print_step(f"done: pid={pid}, binary={binary_path}, url=http://{probe_host(host)}:{port}")


if __name__ == "__main__":
    main()
