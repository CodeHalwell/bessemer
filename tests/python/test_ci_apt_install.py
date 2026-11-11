"""The CI package installer retries, falls back to another mirror and stops.

`scripts/ci_apt_install.sh` only meets a failing archive when the archive
fails, which is the one time nobody is watching it. These tests run it with
stand-ins for apt and dpkg, so each path is taken on every run of the suite.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_apt_install.sh"
BASH = shutil.which("bash")

# Records each call. A download succeeds from the attempt named for its source
# onwards (0: never); an install marks the package installed.
APT_GET = r"""#!/usr/bin/env bash
echo "$*" >>"$STUB/calls"
source=primary
[[ " $* " == *" Dir::Etc::sourcelist="* ]] && source=fallback
case " $* " in
*" update "*) exit "${STUB_UPDATE:-0}" ;;
*" --download-only "*)
    count=$(($(cat "$STUB/$source" 2>/dev/null || echo 0) + 1))
    echo "$count" >"$STUB/$source"
    from=STUB_${source^^}_FROM
    ((${!from:-0} > 0 && count >= ${!from})) && exit 0
    exit 100
    ;;
*" install "*)
    touch "$STUB/installed"
    exit 0
    ;;
esac
exit 2
"""

STUBS = {
    "apt-get": APT_GET,
    "dpkg-query": '#!/usr/bin/env bash\n[[ -e "$STUB/installed" ]] && printf installed || exit 1\n',
    "dpkg": "#!/usr/bin/env bash\necho amd64\n",
    "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
    "timeout": '#!/usr/bin/env bash\nshift\nexec "$@"\n',
    "sleep": '#!/usr/bin/env bash\necho "sleep $*" >>"$STUB/calls"\n',
}


@unittest.skipUnless(
    sys.platform == "linux" and BASH and Path("/etc/os-release").exists(),
    "the installer is for Linux CI runners",
)
class CiAptInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stub = Path(directory.name)
        for name, body in STUBS.items():
            path = self.stub / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        (self.stub / "keyring.gpg").write_bytes(b"")

    def run_script(self, *packages, **settings):
        environment = {
            **os.environ,
            "PATH": f"{self.stub}{os.pathsep}{os.environ['PATH']}",
            "STUB": str(self.stub),
            "TMPDIR": str(self.stub),
            "CI_APT_KEYRING": str(self.stub / "keyring.gpg"),
            "CI_APT_FALLBACK_MIRROR": "https://mirror.example/ubuntu",
            **{name: str(value) for name, value in settings.items()},
        }
        return subprocess.run(
            [BASH, str(SCRIPT), *packages],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self):
        path = self.stub / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def downloads(self):
        return [
            "fallback" if "Dir::Etc::sourcelist=" in call else "primary"
            for call in self.calls()
            if "--download-only" in call
        ]

    def installs(self):
        return [
            call
            for call in self.calls()
            if " install " in f" {call} " and "--download-only" not in call
        ]

    def test_a_working_archive_is_asked_once(self):
        result = self.run_script("redis-server", STUB_PRIMARY_FROM=1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary"])
        (install,) = self.installs()
        self.assertNotIn("Dir::Etc::sourcelist=", install)
        self.assertTrue(install.endswith("install -y redis-server"), install)
        self.assertNotIn("::warning::", result.stdout)
        self.assertFalse([call for call in self.calls() if call.startswith("sleep")])

    def test_installed_packages_need_no_archive(self):
        (self.stub / "installed").touch()

        result = self.run_script("redis-server")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_fallback_mirror_serves_what_the_runner_mirrors_cannot(self):
        result = self.run_script("redis-server", STUB_FALLBACK_FROM=1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary", "fallback"])
        # The install reads the index the download used, and nothing else.
        (install,) = self.installs()
        self.assertIn("Dir::Etc::sourcelist=", install)
        self.assertIn("Dir::Etc::sourceparts=-", install)
        self.assertIn("Dir::State::lists=", install)
        (sources,) = self.stub.glob("tmp.*/sources.list")
        lines = sources.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertTrue(
                line.startswith(
                    f"deb [signed-by={self.stub / 'keyring.gpg'}] https://mirror.example/ubuntu "
                ),
                line,
            )
        self.assertIn("::warning::Attempt 1", result.stdout)

    def test_a_failed_index_refresh_does_not_stop_the_download(self):
        result = self.run_script("redis-server", STUB_UPDATE=124, STUB_PRIMARY_FROM=1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary"])
        self.assertIn("was not refreshed", result.stdout)

    def test_attempts_repeat_until_one_works(self):
        result = self.run_script("redis-server", "valgrind", STUB_PRIMARY_FROM=3)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary", "fallback"] * 2 + ["primary"])
        self.assertEqual(
            [call for call in self.calls() if call.startswith("sleep")],
            ["sleep 15", "sleep 30"],
        )
        (install,) = self.installs()
        self.assertTrue(install.endswith("install -y redis-server valgrind"), install)

    def test_it_gives_up_at_the_deadline_without_installing(self):
        result = self.run_script("redis-server", CI_APT_PATIENCE=0)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary", "fallback"])
        self.assertEqual(self.installs(), [])
        self.assertIn("::error::Could not download redis-server", result.stdout)

    def test_no_fallback_is_tried_when_none_is_configured(self):
        result = self.run_script(
            "redis-server", CI_APT_PATIENCE=0, CI_APT_FALLBACK_MIRROR=""
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.downloads(), ["primary"])

    def test_packages_must_be_named(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
