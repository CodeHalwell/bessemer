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
