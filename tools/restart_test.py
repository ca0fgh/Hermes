import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RESTART_PATH = Path(__file__).resolve().parent / "restart.py"
SPEC = importlib.util.spec_from_file_location("restart", RESTART_PATH)
restart = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restart)


def make_args(*, restart_only: bool = False, app_dir=None, go_bin=None, node_bin=None, pnpm_bin=None):
    return argparse.Namespace(
        restart_only=restart_only,
        app_dir=app_dir,
        go_bin=go_bin,
        node_bin=node_bin,
        pnpm_bin=pnpm_bin,
    )


class CollectMissingDependenciesTest(unittest.TestCase):
    def test_build_mode_reports_all_missing_dependencies_before_start(self):
        args = make_args()

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            binary_path = app_dir / "hermes-proxy"
            config_path = app_dir / "config.yaml"

            with mock.patch.object(restart, "find_tool", return_value="") as find_tool:
                with mock.patch.object(restart, "frontend_dependencies_installed", return_value=False):
                    with mock.patch.object(restart, "local_runtime_dependency_installed", return_value=False):
                        issues = restart.collect_preflight_issues(args, binary_path, config_path)

        self.assertEqual(3, find_tool.call_count)
        joined = "\n".join(issues)
        self.assertIn("`node`", joined)
        self.assertIn("`pnpm`", joined)
        self.assertIn("`go`", joined)
        self.assertIn("PostgreSQL", joined)
        self.assertIn("Redis", joined)
        self.assertIn("frontend dependencies", joined)
        self.assertNotIn("config.yaml", joined)

    def test_restart_only_skips_build_tool_checks_but_requires_runtime_files(self):
        args = make_args(restart_only=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            binary_path = app_dir / "hermes-proxy"
            config_path = app_dir / "config.yaml"

            with mock.patch.object(restart, "find_tool") as find_tool:
                with mock.patch.object(restart, "frontend_dependencies_installed") as frontend_dependencies_installed:
                    with mock.patch.object(restart, "local_runtime_dependency_installed", return_value=False):
                        issues = restart.collect_preflight_issues(args, binary_path, config_path)

        joined = "\n".join(issues)
        self.assertIn("hermes-proxy`: not found", joined)
        self.assertIn("config.yaml", joined)
        self.assertIn("PostgreSQL", joined)
        self.assertIn("Redis", joined)
        find_tool.assert_not_called()
        frontend_dependencies_installed.assert_not_called()

    def test_runtime_checks_report_postgres_and_redis_installation_before_start(self):
        args = make_args(restart_only=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            binary_path = app_dir / "hermes-proxy"
            binary_path.write_text("", encoding="utf-8")
            config_path = app_dir / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "database:",
                        "    host: 127.0.0.1",
                        "    port: 5432",
                        "redis:",
                        "    host: 127.0.0.1",
                        "    port: 6379",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(restart, "local_runtime_dependency_installed", return_value=False):
                issues = restart.collect_preflight_issues(args, binary_path, config_path)

        joined = "\n".join(issues)
        self.assertIn("PostgreSQL", joined)
        self.assertIn("Redis", joined)

    def test_runtime_checks_use_installation_hints(self):
        args = make_args(restart_only=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            binary_path = app_dir / "hermes-proxy"
            binary_path.write_text("", encoding="utf-8")
            config_path = app_dir / "config.yaml"
            config_path.write_text("server:\n    port: 8080\n", encoding="utf-8")

            with mock.patch.object(restart, "local_runtime_dependency_installed", return_value=False):
                issues = restart.collect_preflight_issues(args, binary_path, config_path)

        joined = "\n".join(issues)
        self.assertIn("Install PostgreSQL 15+ first", joined)
        self.assertIn("Install Redis 7+ first", joined)


class BootstrapRuntimeFilesTest(unittest.TestCase):
    def test_bootstrap_runtime_files_creates_app_dir_without_forcing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir) / ".hermes-proxy-runtime"
            config_path = app_dir / "config.yaml"

            restart.bootstrap_runtime_files(app_dir)

            self.assertTrue(app_dir.exists())
            self.assertFalse(config_path.exists())

    def test_collect_preflight_issues_allows_first_run_without_config_in_build_mode(self):
        args = make_args()

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir) / ".hermes-proxy-runtime"
            binary_path = app_dir / "hermes-proxy"
            config_path = app_dir / "config.yaml"

            restart.bootstrap_runtime_files(app_dir)
            with mock.patch.object(restart, "find_tool", return_value="/mock/tool"):
                with mock.patch.object(
                    restart,
                    "command_output",
                    side_effect=["v18.0.0", "9.0.0", "go version go1.21.5 darwin/arm64"],
                ):
                    with mock.patch.object(restart, "frontend_dependencies_installed", return_value=True):
                        with mock.patch.object(restart, "local_runtime_dependency_installed", return_value=True):
                            issues = restart.collect_preflight_issues(args, binary_path, config_path)

        self.assertEqual([], issues)


class EnsurePreflightReadyTest(unittest.TestCase):
    def test_ensure_preflight_ready_fails_with_combined_hint(self):
        args = make_args()

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            binary_path = app_dir / "hermes-proxy"
            config_path = app_dir / "config.yaml"

            with mock.patch.object(
                restart,
                "collect_preflight_issues",
                return_value=["`pnpm`: missing", "`go`: missing"],
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exc:
                        restart.ensure_preflight_ready(args, binary_path, config_path)

        self.assertEqual(1, exc.exception.code)
        self.assertIn("missing required dependencies before restart", stderr.getvalue())
        self.assertIn("`pnpm`: missing", stderr.getvalue())
        self.assertIn("`go`: missing", stderr.getvalue())


class EnsureRuntimeServicesTest(unittest.TestCase):
    def test_ensure_runtime_services_starts_local_postgres_and_redis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            config_path = app_dir / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "database:",
                        "    host: 127.0.0.1",
                        "    port: 5432",
                        "    user: hermes-proxy",
                        "    password: hermes-proxy",
                        "    dbname: hermes-proxy",
                        "redis:",
                        "    host: 127.0.0.1",
                        "    port: 6379",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(restart, "ensure_local_postgres_running") as ensure_postgres:
                with mock.patch.object(restart, "ensure_local_redis_running") as ensure_redis:
                    restart.ensure_runtime_services(app_dir, config_path)

        ensure_postgres.assert_called_once_with(app_dir, config_path)
        ensure_redis.assert_called_once_with(app_dir, config_path)

    def test_ensure_local_redis_running_starts_repo_scoped_server(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            config_path = app_dir / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "redis:",
                        "    host: 127.0.0.1",
                        "    port: 6379",
                        "    password: secret",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(restart, "is_tcp_port_open", return_value=False):
                with mock.patch.object(restart, "resolve_redis_server_bin", return_value="/mock/redis-server"):
                    with mock.patch.object(restart.subprocess, "Popen") as popen:
                        with mock.patch.object(restart, "wait_until_listening") as wait_until_listening:
                            restart.ensure_local_redis_running(app_dir, config_path)

        command = popen.call_args.args[0]
        self.assertEqual("/mock/redis-server", command[0])
        self.assertIn("--port", command)
        self.assertIn("6379", command)
        self.assertIn("--dir", command)
        self.assertIn(str((app_dir / "redis").resolve()), command)
        self.assertIn("--requirepass", command)
        self.assertIn("secret", command)
        wait_until_listening.assert_called_once_with("127.0.0.1", 6379)

    def test_ensure_local_postgres_running_initializes_cluster_and_bootstraps_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            config_path = app_dir / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "database:",
                        "    host: 127.0.0.1",
                        "    port: 5432",
                        "    user: hermes-proxy",
                        "    password: secret",
                        "    dbname: hermes-proxy",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(restart, "is_tcp_port_open", return_value=False):
                with mock.patch.object(restart, "resolve_pg_ctl_bin", return_value="/mock/pg_ctl"):
                    with mock.patch.object(restart, "resolve_initdb_bin", return_value="/mock/initdb"):
                        with mock.patch.object(restart, "resolve_psql_bin", return_value="/mock/psql"):
                            with mock.patch.object(restart.subprocess, "run") as run_cmd:
                                with mock.patch.object(restart, "wait_until_postgres_ready") as wait_ready:
                                    with mock.patch.object(restart, "bootstrap_local_postgres_database") as bootstrap_db:
                                        restart.ensure_local_postgres_running(app_dir, config_path)

        first_command = run_cmd.call_args_list[0].args[0]
        second_command = run_cmd.call_args_list[1].args[0]
        self.assertEqual("/mock/initdb", first_command[0])
        self.assertIn(str((app_dir / "postgres").resolve()), first_command)
        self.assertEqual("/mock/pg_ctl", second_command[0])
        self.assertIn("start", second_command)
        wait_ready.assert_called_once_with("/mock/psql", "127.0.0.1", 5432)
        bootstrap_db.assert_called_once_with(
            "/mock/psql",
            "127.0.0.1",
            5432,
            "hermes-proxy",
            "secret",
            "hermes-proxy",
        )


if __name__ == "__main__":
    unittest.main()
