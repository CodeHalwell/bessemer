"""Release metadata stays consistent so a tag cannot publish a mismatched build."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_version.py"
SPEC = importlib.util.spec_from_file_location("bessemer_release_version", SCRIPT_PATH)
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseVersionTests(unittest.TestCase):
    def test_repository_versions_agree_and_have_release_notes(self):
        version = RELEASE.check()
        self.assertTrue(RELEASE.release_notes(version).strip())

    def test_installed_package_matches_repository_version(self):
        import bessemer

        self.assertEqual(bessemer.__version__, RELEASE.check())

    def test_frozen_public_api_is_only_extended(self):
        import bessemer

        frozen_1_0 = {
            "APIRouter",
            "App",
            "BackgroundTasks",
            "Body",
            "CORS",
            "Compression",
            "Cookie",
            "Depends",
            "EventSourceResponse",
            "FastAPI",
            "File",
            "Form",
            "HTMLResponse",
            "HTTPException",
            "Header",
            "JSONResponse",
            "JWTAuth",
            "NoMatchFound",
            "OpenTelemetry",
            "Path",
            "PlainTextResponse",
            "Principal",
            "Query",
            "RedirectResponse",
            "Request",
            "RequestValidationError",
            "Response",
            "Router",
            "Security",
            "State",
            "StreamingResponse",
            "UploadFile",
            "WebSocket",
            "WebSocketDisconnect",
            "WebSocketException",
            "require_scope",
        }
        # Names added since the freeze are frozen from the release that
        # shipped them.
        added_in_1_x = {"FileResponse", "ResponseValidationError"}
        missing = (frozen_1_0 | added_in_1_x) - set(bessemer.__all__)
        self.assertFalse(missing, f"1.x removed public names: {sorted(missing)}")
        for name in bessemer.__all__:
            self.assertTrue(hasattr(bessemer, name), name)

    def test_frozen_settings_and_cli_flags_are_only_extended(self):
        # `BESSEMER_*` variables and `[tool.bessemer]` keys are these names;
        # each also has a `bessemer run` flag.
        import contextlib
        import io

        from bessemer.cli import main
        from bessemer.config import DEFAULTS

        frozen_settings = {
            "event_loop",
            "graceful_timeout",
            "header_timeout",
            "host",
            "http",
            "io_backend",
            "loops",
            "max_body_size",
            "max_connections",
            "max_header_size",
            "max_headers",
            "max_in_flight",
            "max_lifetime",
            "max_request_bytes_in_flight",
            "max_request_target_size",
            "max_requests",
            "max_response_bytes_in_flight",
            "max_response_size",
            "max_rss",
            "max_upload_bytes_in_flight",
            "max_upload_size",
            "min_send_rate",
            "port",
            "request_timeout",
            "restart_backoff",
            "restart_limit",
            "startup_timeout",
            "sync_workers",
            "tls_cert",
            "tls_client_auth",
            "tls_client_ca",
            "tls_client_crl",
            "tls_key",
            "tls_min",
            "tls_ocsp_response",
            "tls_reload_interval",
            "upload_dir",
            "websocket_compression",
            "worker_timeout",
            "workers",
        }
        missing = frozen_settings - set(DEFAULTS)
        self.assertFalse(missing, f"1.x removed settings: {sorted(missing)}")
        usage = io.StringIO()
        with contextlib.redirect_stdout(usage), self.assertRaises(SystemExit):
            main(["run", "--help"])
        flags = set(usage.getvalue().replace(",", " ").split())
        for name in frozen_settings:
            self.assertIn("--" + name.replace("_", "-"), flags)

    def test_cargo_spelling_of_prereleases(self):
        self.assertEqual(RELEASE.cargo_version("1.0.0"), "1.0.0")
        self.assertEqual(RELEASE.cargo_version("1.0.0rc1"), "1.0.0-rc.1")
        self.assertEqual(RELEASE.cargo_version("0.2.0a3"), "0.2.0-alpha.3")
        self.assertEqual(RELEASE.cargo_version("1.1.0b2"), "1.1.0-beta.2")
        with self.assertRaises(ValueError):
            RELEASE.cargo_version("1.0")

    def test_prerelease_detection(self):
        self.assertFalse(RELEASE.is_prerelease("1.0.0"))
        self.assertTrue(RELEASE.is_prerelease("1.0.0rc1"))

    def test_mismatched_tag_is_rejected(self):
        version = RELEASE.check()
        RELEASE.check(f"v{version}")
        with self.assertRaises(ValueError):
            RELEASE.check("v99.0.0")

    def test_github_outputs_and_notes(self):
        version = RELEASE.check()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            notes = Path(directory) / "notes.md"
            status = RELEASE.main(
                [
                    "--tag",
                    f"v{version}",
                    "--github-output",
                    str(output),
                    "--notes",
                    str(notes),
                ]
            )
            self.assertEqual(status, 0)
            lines = output.read_text().splitlines()
            self.assertIn(f"version={version}", lines)
            self.assertIn(
                f"prerelease={'true' if RELEASE.is_prerelease(version) else 'false'}",
                lines,
            )
            self.assertEqual(notes.read_text(), RELEASE.release_notes(version))
            self.assertEqual(RELEASE.main(["--tag", "v99.0.0"]), 1)


if __name__ == "__main__":
    unittest.main()
