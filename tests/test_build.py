"""The build ID and the version note."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detent_core.build import build_id, version_line


def make_addin(root, sync_body="x = 1\n"):
    os.makedirs(os.path.join(root, "detent_core"), exist_ok=True)
    files = {"Detent.py": "VERSION = '1'\n",
             "detent_core/__init__.py": "VERSION = '1'\n",
             "detent_core/sync.py": sync_body}
    for name, text in files.items():
        with open(os.path.join(root, *name.split("/")), "w", newline="") as fh:
            fh.write(text)
    return root


class TestBuildId:
    def test_same_code_same_id(self, tmp_path):
        a = make_addin(str(tmp_path / "a"))
        b = make_addin(str(tmp_path / "b"))
        assert build_id(a) == build_id(b)
        assert len(build_id(a)) == 7

    def test_a_changed_core_file_changes_the_id(self, tmp_path):
        """The case that went unnoticed: a fix in sync.py, same version number."""
        a = make_addin(str(tmp_path / "a"))
        b = make_addin(str(tmp_path / "b"), sync_body="x = 2\n")
        assert build_id(a) != build_id(b)

    def test_a_changed_detent_py_changes_the_id(self, tmp_path):
        a = make_addin(str(tmp_path / "a"))
        before = build_id(a)
        with open(os.path.join(a, "Detent.py"), "a") as fh:
            fh.write("# edit\n")
        assert build_id(a) != before

    def test_a_new_core_file_changes_the_id(self, tmp_path):
        a = make_addin(str(tmp_path / "a"))
        before = build_id(a)
        with open(os.path.join(a, "detent_core", "extra.py"), "w") as fh:
            fh.write("y = 1\n")
        assert build_id(a) != before

    def test_ignores_files_that_are_not_code(self, tmp_path):
        """Records, config and caches change all the time. They aren't the build."""
        a = make_addin(str(tmp_path / "a"))
        before = build_id(a)
        os.makedirs(os.path.join(a, "state"))
        os.makedirs(os.path.join(a, "detent_core", "__pycache__"))
        for name in ("config.json", "state/x.manifest.json",
                     "detent_core/__pycache__/sync.cpython-311.pyc"):
            with open(os.path.join(a, *name.split("/")), "w") as fh:
                fh.write("anything")
        assert build_id(a) == before

    def test_windows_line_endings_give_the_same_id(self, tmp_path):
        a = make_addin(str(tmp_path / "a"), sync_body="x = 1\ny = 2\n")
        b = make_addin(str(tmp_path / "b"), sync_body="x = 1\r\ny = 2\r\n")
        assert build_id(a) == build_id(b)

    def test_the_repo_has_an_id(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert len(build_id(root)) == 7


class TestVersionLine:
    def test_up_to_date(self):
        assert version_line("0.4.2", "0.4.2", "abc1234", "abc1234") == \
            "Detent 0.4.2 (build abc1234)"

    def test_files_updated_but_fusion_not_restarted(self):
        """Same version number, different code: only the build shows it."""
        line = version_line("0.4.2", "0.4.2", "abc1234", "def5678")
        assert "OUT OF DATE" in line and "Restart Fusion" in line
        assert "abc1234" in line and "def5678" in line

    def test_version_mismatch_still_reported(self):
        line = version_line("0.4.2", "0.4.1", "abc1234", "abc1234")
        assert "STALE MODULE" in line


class TestAddinUsesIt:
    def source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return open(os.path.join(root, "Detent.py"), encoding="utf-8").read()

    def test_build_is_read_once_at_load(self):
        assert "\nLOADED_BUILD = build_id(_HERE)\n" in self.source()

    def test_note_compares_loaded_build_with_disk(self):
        assert ("version_line(VERSION, CORE_VERSION, LOADED_BUILD, "
                "build_id(_HERE))") in self.source()
