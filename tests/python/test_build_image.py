"""Release provenance guards never follow external source or overwrite evidence."""

import contextlib
import hashlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BUILDER_PATH = Path(__file__).resolve().parents[2] / "deploy" / "build_image.py"
SPEC = importlib.util.spec_from_file_location("bessemer_image_builder", BUILDER_PATH)
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class ImageProvenanceTests(unittest.TestCase):
    def repository(self, root):
        for name in BUILDER.ROOT_INPUTS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        for directory in BUILDER.SOURCE_DIRECTORIES:
            (root / directory).mkdir(exist_ok=True)

    def test_source_directory_symlinks_are_rejected_before_reading_outside(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "repository"
            root.mkdir()
            self.repository(root)
            external = Path(base) / "external"
            external.mkdir()
            (external / "credential.py").write_text("fixture-only-private-data")
            (root / "python").rmdir()
            (root / "python").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                BUILDER.source_manifest(root)

    def test_root_input_parent_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "repository"
            root.mkdir()
            self.repository(root)
            external = Path(base) / "external-public-fixture"
            (root / "deploy").rename(external)
            (root / "deploy").symlink_to(external, target_is_directory=True)
            self.assertFalse(
                (root / "deploy/requirements-production.lock").is_symlink()
            )
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                BUILDER.source_manifest(root)

    def test_root_input_leaf_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "repository"
            root.mkdir()
            self.repository(root)
            external = Path(base) / "public-readme-fixture"
            (root / "README.md").rename(external)
            (root / "README.md").symlink_to(external)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                BUILDER.source_manifest(root)

    def test_frozen_snapshot_keeps_bytes_and_excludes_environment_directories(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "repository"
            root.mkdir()
            self.repository(root)
            source = root / "python" / "application.py"
            source.write_bytes(b"original-source")
            hidden = root / "python" / ".env.private"
            hidden.mkdir()
            (hidden / "credential").write_text("fixture-only-private-data")
            snapshot = Path(base) / "snapshot"
            manifest = BUILDER.source_manifest(root, snapshot)
            source.write_bytes(b"changed-after-snapshot")
            self.assertEqual(
                (snapshot / "python/application.py").read_bytes(), b"original-source"
            )
            self.assertFalse((snapshot / "python/.env.private").exists())
            recorded = next(
                item
                for item in manifest["files"]
                if item["path"] == "python/application.py"
            )
            self.assertEqual(
                recorded["sha256"], hashlib.sha256(b"original-source").hexdigest()
            )

    def test_existing_output_is_preserved_without_invoking_docker(self):
        with tempfile.TemporaryDirectory() as base:
            output = Path(base) / "evidence"
            output.mkdir()
            evidence = output / "manifest.json"
            evidence.write_text("original-evidence")
            arguments = [
                "build_image.py",
                "--image",
                "unused:test",
                "--output",
                str(output),
            ]
            with (
                patch("sys.argv", arguments),
                patch.object(BUILDER.subprocess, "run") as process,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                BUILDER.main()
            process.assert_not_called()
            self.assertEqual(evidence.read_text(), "original-evidence")
            self.assertFalse((output / "context").exists())


if __name__ == "__main__":
    unittest.main()
