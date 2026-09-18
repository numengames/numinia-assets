# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Exercise the real REUSE gate; never use an asset's label as proof of rights."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LicensingGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="numinia-assets-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "repo"
        shutil.copytree(
            ROOT,
            self.project,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
        )

    def lint(self):
        return subprocess.run(
            [sys.executable, "-m", "reuse", "--root", str(self.project), "lint"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_repository_has_no_unlicensed_files(self):
        result = self.lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unlicensed_media_is_rejected_in_every_category(self):
        # Deliberately invalid media: these bytes only exercise declaration
        # coverage, not a parser, a real resource or ownership verification.
        for category, suffix in (
            ("images", ".png"), ("audio", ".mp3"), ("models", ".glb"),
            ("avatars", ".vrm"), ("video", ".mp4"), ("originals", ".blend"),
        ):
            with self.subTest(category=category):
                resource = self.project / "content" / category / ("test-only" + suffix)
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_bytes(b"\x00synthetic declaration-test fixture\xff")
                result = self.lint()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(resource.name, result.stdout + result.stderr)
                resource.unlink()

    # REUSE-IgnoreStart
    # The following SPDX strings describe synthetic test files, not this code.
    def test_explicit_sidecar_is_accepted(self):
        resource = self.project / "content" / "test-only.bin"
        resource.write_bytes(b"\x00synthetic declaration-test fixture\xff")
        resource.with_suffix(".bin.license").write_text(
            "SPDX-FileCopyrightText: Test fixture only\n"
            "SPDX-License-Identifier: CC0-1.0\n",
            encoding="utf-8",
        )
        result = self.lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_licence_without_rightsholder_declaration_is_rejected(self):
        resource = self.project / "content" / "test-only.bin"
        resource.write_bytes(b"\x00synthetic declaration-test fixture\xff")
        resource.with_suffix(".bin.license").write_text(
            "SPDX-License-Identifier: CC0-1.0\n", encoding="utf-8",
        )
        result = self.lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(resource.name, result.stdout + result.stderr)

    # REUSE-IgnoreEnd

    def test_unlisted_root_file_is_not_implicitly_licensed(self):
        resource = self.project / "unreviewed.txt"
        resource.write_text("Synthetic test content, not an imported resource.\n", encoding="utf-8")
        result = self.lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(resource.name, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
