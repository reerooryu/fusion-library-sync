"""End-to-end sync against a fake Data Panel that duplicates like the real one.

The fake is deliberately unsafe: uploading a same-named file into a folder
creates a second file on a new lineage, silently, exactly as Fusion does.
Every test here asserts that the orchestration never lets that happen.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import github as gh
from core import plan as PL
from core import sync as S
from core.datapanel import FakeDataPanel, UploadFailed
from core.manifest import Manifest, INFLIGHT, PLACED

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
F3D = ("**/*.f3d",)


def load_tree(tag):
    out = {}
    with open(os.path.join(FIX, f"tree_{tag}.tsv"), encoding="utf-8") as fh:
        for line in fh:
            sha, _, path = line.rstrip("\n").partition("\t")
            if path:
                out[path] = sha
    return out


class FakeTransport:
    """Serves a fixture tree and synthesises blob bytes."""

    def __init__(self, tree, commit="c0ffee", fail_paths=()):
        self.tree = tree
        self.commit = commit
        self.fail_paths = set(fail_paths)
        self.json_calls = 0
        self.byte_calls = 0

    def get_json(self, url):
        self.json_calls += 1
        if "/commits/" in url:
            return {"sha": self.commit}, {}
        return ({"truncated": False,
                 "tree": [{"path": p, "type": "blob", "sha": s}
                          for p, s in self.tree.items()]}, {})

    def get_bytes(self, url):
        self.byte_calls += 1
        path = "/".join(url.split("/")[6:])
        import urllib.parse
        path = urllib.parse.unquote(path)
        if path in self.fail_paths:
            raise OSError("simulated network failure")
        return f"content-of:{path}".encode()


@pytest.fixture
def small_tree():
    """A slice small enough to avoid the tarball path, with a hostile name."""
    full = load_tree("v2.0.5")
    picked = {p: s for p, s in list(full.items()) if p.endswith(".f3d")}
    smart = [p for p in picked if "”" in p][:2]
    bang = [p for p in picked if p.rsplit("/", 1)[-1].startswith("!")][:1]
    plain = [p for p in picked if p not in smart + bang][:7]
    return {p: picked[p] for p in smart + bang + plain}


@pytest.fixture
def src():
    return gh.Source("VEX-CAD/VEX-CAD-Fusion-360-Library", "main")


class TestInvariant:
    """Never upload a path already in the manifest."""

    def test_first_sync_places_everything_once(self, small_tree, src, tmp_path):
        panel, tr = FakeDataPanel(), FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert len(rep.added) == len(small_tree)
        assert panel.total_files == len(small_tree)
        assert panel.duplicates() == {}
        assert rep.ok

    def test_second_sync_uploads_nothing(self, small_tree, src, tmp_path):
        panel, tr = FakeDataPanel(), FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        before = panel.total_files
        uploads_before = len(panel.uploads)

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert plan.is_empty
        assert rep.added == []
        assert len(panel.uploads) == uploads_before, "re-uploaded a known path"
        assert panel.total_files == before
        assert panel.duplicates() == {}

    def test_fake_really_does_duplicate(self, small_tree, src, tmp_path):
        """If the fake were safe, the tests above would prove nothing."""
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        panel.upload(folder, "/tmp/x", "Same")
        panel.upload(folder, "/tmp/x", "Same")
        assert panel.total_files == 2
        assert panel.duplicates() == {"folder:1/Same": 2}

    def test_dry_run_writes_nothing(self, small_tree, src, tmp_path):
        panel, tr = FakeDataPanel(), FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=True)

        assert len(plan.add) == len(small_tree)
        assert rep.added == []
        assert panel.total_files == 0
        assert not os.path.exists(mpath)


class TestCrashRecovery:
    """A4: interruption must never produce duplicates."""

    def test_interrupted_sync_leaves_an_inflight_record(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(crash_after=3)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        with pytest.raises(KeyboardInterrupt):
            S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        m = Manifest.load(mpath)
        assert m is not None, "manifest must survive a crash"
        assert len(m.in_state(PLACED)) == 3
        assert len(m.in_state(INFLIGHT)) == 1, "the file being uploaded when we died"

    def test_resume_after_crash_produces_no_duplicates(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(crash_after=3)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")
        with pytest.raises(KeyboardInterrupt):
            S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        panel.crash_after = None                      # machine comes back up
        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert panel.duplicates() == {}, "resume duplicated a file"
        assert panel.total_files == len(small_tree)
        assert Manifest.load(mpath).in_state(INFLIGHT) == {}

    def test_inflight_that_actually_landed_is_adopted_not_re_uploaded(self, src, tmp_path):
        """The dangerous window: upload succeeded, manifest write did not."""
        tree = {"A/Part.f3d": "sha1"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        folder = panel.ensure_folder(("A",))
        landed = panel.upload(folder, "/tmp/x", "Part")   # it's in the cloud
        m = Manifest("s", src.repo)
        m.mark_inflight("A/Part.f3d", placed_name="Part")  # but we only got this far
        m.save(mpath)

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert panel.total_files == 1, "re-uploaded a file that had already landed"
        assert panel.duplicates() == {}
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry.state == PLACED
        assert entry.lineage == landed.lineage
        assert ("A/Part.f3d", "landed") in rep.reconciled

    def test_pre_existing_duplicate_blocks_rather_than_guesses(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha1"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")
        folder = panel.ensure_folder(("A",))
        panel.upload(folder, "/tmp/x", "Part")
        panel.upload(folder, "/tmp/x", "Part")            # already a mess
        m = Manifest("s", src.repo)
        m.mark_inflight("A/Part.f3d", placed_name="Part")
        m.save(mpath)

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert any("resolve by hand" in msg for _, msg in rep.failures)
        assert panel.total_files == 2, "must not add a third"


class TestFailureIsolation:
    """A7: one bad file never aborts a run."""

    def test_upload_failure_is_isolated_and_retryable(self, small_tree, src, tmp_path):
        victim = sorted(small_tree)[2]
        from core import paths as P
        panel = FakeDataPanel(fail_on=[P.map_path(victim).name])
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert len(rep.added) == len(small_tree) - 1
        assert len(rep.failures) == 1
        m = Manifest.load(mpath)
        assert victim not in m, "failed file must be retryable, not recorded"

        panel.fail_on = set()
        plan2, rep2 = S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        assert rep2.added == [victim]
        assert panel.duplicates() == {}

    def test_download_failure_does_not_record_the_file(self, small_tree, src, tmp_path):
        victim = sorted(small_tree)[1]
        panel = FakeDataPanel()
        tr = FakeTransport(small_tree, fail_paths=[victim])
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert victim not in Manifest.load(mpath)
        assert any(p == victim for p, _ in rep.failures)
        assert len(rep.added) == len(small_tree) - 1


class TestCollisions:
    def test_two_paths_one_destination_blocks_the_sync(self, src, tmp_path):
        # Same folder, same stem, different extensions -> one Data Panel name.
        tree = {"A/Part.f3d": "s1", "A/Part.step": "s2"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, ("**/*.f3d", "**/*.step"),
                           dry_run=False)

        assert rep.collisions, "collision must be detected"
        assert panel.total_files == 0, "nothing may be written when blocked"
        assert not rep.ok


class TestHostilePaths:
    def test_smart_quote_file_round_trips(self, small_tree, src, tmp_path):
        panel, tr = FakeDataPanel(), FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        m = Manifest.load(mpath)
        smart = [p for p in small_tree if "”" in p]
        assert smart
        for p in smart:
            assert p in m
            assert m.get(p).state == PLACED

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        assert plan.add == [], "phantom re-add of a non-ASCII path"


class TestStrategy:
    def test_tarball_chosen_only_above_threshold(self):
        assert not gh.should_use_tarball(27)
        assert gh.should_use_tarball(1193)

    def test_truncated_tree_raises_rather_than_lying(self):
        with pytest.raises(gh.TreeTruncated):
            gh.parse_tree({"truncated": True, "tree": []})

    def test_tree_ignores_directories(self):
        payload = {"truncated": False, "tree": [
            {"path": "A", "type": "tree", "sha": "t1"},
            {"path": "A/x.f3d", "type": "blob", "sha": "b1"},
        ]}
        assert gh.parse_tree(payload) == {"A/x.f3d": "b1"}

    def test_one_api_request_for_the_tree(self, small_tree, src, tmp_path):
        panel, tr = FakeDataPanel(), FakeTransport(small_tree)
        S.sync(src, str(tmp_path / "m.json"), panel, tr, F3D, dry_run=True)
        assert tr.json_calls == 2, "one commit resolve, one tree read"

    def test_raw_url_quotes_non_ascii(self, src):
        url = gh.raw_url(src, "Motion/0.5” OD Spacer.f3d", "abc123")
        assert "”" not in url and "%E2%80%9D" in url
        assert url.startswith("https://raw.githubusercontent.com/")
