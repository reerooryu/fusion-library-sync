"""End-to-end sync against a fake Data Panel that duplicates like the real one.

The fake is deliberately unsafe: uploading a same-named file into a folder
creates a second file on a new lineage, silently, exactly as Fusion does.
Every test here asserts that the orchestration never lets that happen.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detent_core import github as gh
from detent_core import plan as PL
from detent_core import sync as S
from detent_core.datapanel import FakeDataPanel, UploadFailed
from detent_core.manifest import Manifest, INFLIGHT, PLACED

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

    def get_bytes(self, url, on_chunk=None):
        self.byte_calls += 1
        if on_chunk is not None:
            on_chunk(1, 1)
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
        # Uploads are fired as a batch and resolved afterwards, so a crash
        # during the fire phase leaves everything inflight, nothing placed.
        # Those files may still have landed; reconcile_inflight settles that
        # on the next run by looking at the folder.
        assert len(m.in_state(PLACED)) == 0
        assert len(m.in_state(INFLIGHT)) == 4, "3 fired plus the one that died"

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
        from detent_core import paths as P
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

    def test_an_altered_name_is_always_reported(self, src, tmp_path):
        """A name we changed and did not mention becomes tomorrow's duplicate:
        the manifest keys on the repo path, the panel keys on the name."""
        tree = {"A/Part .f3d": "sha1", "A/Plain.f3d": "sha2"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        _plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert rep.renamed == [("A/Part .f3d", "Part")], rep.renamed
        assert "Part" in [f.name for f in panel.list_folder(panel.folders[("A",)])]

    def test_preview_reports_alterations_before_writing(self, src, tmp_path):
        tree = {"A/Part .f3d": "sha1"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)

        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel, tr, F3D,
                            dry_run=True)

        assert rep.renamed == [("A/Part .f3d", "Part")]
        assert panel.total_files == 0, "preview must not write"


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


class TestAdopt:
    """Claiming a library the user already imported. Uploads nothing."""

    def _panel_with(self, tree, src):
        from detent_core import paths as P
        panel = FakeDataPanel()
        for repo_path in tree:
            pp = P.map_path(repo_path)
            folder = panel.ensure_folder(pp.folders)
            panel.upload(folder, "/tmp/x", pp.name)
        panel.uploads.clear()          # pretend these predate us
        return panel

    def test_adopt_matches_and_uploads_nothing(self, small_tree, src, tmp_path):
        panel = self._panel_with(small_tree, src)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        m, stats = S.adopt_existing(src, mpath, panel, tr, "v2.0.5", F3D, dry_run=False)

        assert stats["matched"] == len(small_tree)
        assert stats["missing"] == 0
        assert panel.uploads == [], "adopt must not upload"
        assert all(e.blob is not None for e in m.files.values())

    def test_adopt_then_sync_is_a_no_op(self, small_tree, src, tmp_path):
        panel = self._panel_with(small_tree, src)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")
        S.adopt_existing(src, mpath, panel, tr, "v2.0.5", F3D, dry_run=False)

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert plan.add == [], "adopted files must not be re-uploaded"
        assert panel.uploads == []
        assert panel.duplicates() == {}

    def test_unknown_release_still_adopts_the_files(self, small_tree, src, tmp_path):
        """Regression: passing at_ref=None used to match against an empty tree
        and silently record an EMPTY manifest, so the next sync re-uploaded
        the user's entire library."""
        panel = self._panel_with(small_tree, src)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        m, stats = S.adopt_existing(src, mpath, panel, tr, None, F3D, dry_run=False)

        assert stats["matched"] == len(small_tree), "adopted nothing"
        assert len(m.files) == len(small_tree)
        assert all(e.blob is None for e in m.files.values()), "must not claim content"

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        assert plan.add == [], "re-uploaded an adopted library"
        assert len(plan.unverified) == len(small_tree)
        assert panel.duplicates() == {}

    def test_partial_library_reports_the_gap(self, small_tree, src, tmp_path):
        subset = dict(list(small_tree.items())[:5])
        panel = self._panel_with(subset, src)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        m, stats = S.adopt_existing(src, mpath, panel, tr, "v2.0.5", F3D, dry_run=False)

        assert stats["matched"] == 5
        assert stats["missing"] == len(small_tree) - 5

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        assert len(rep.added) == len(small_tree) - 5, "should add only the gap"
        assert panel.duplicates() == {}

    def test_dry_run_adopt_writes_no_manifest(self, small_tree, src, tmp_path):
        panel = self._panel_with(small_tree, src)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")
        S.adopt_existing(src, mpath, panel, tr, "v2.0.5", F3D, dry_run=True)
        assert not os.path.exists(mpath)


class TestEstimates:
    """The confirm dialog must state cost, not just a file count."""

    def test_bootstrap_states_files_size_and_time(self):
        text = gh.estimate(1198, 2_362_232_012)
        assert "1198 file" in text
        assert "2.2 GB" in text, "size is the part that surprises people"
        assert "about" in text

    def test_estimate_grows_with_the_work(self):
        import re

        def secs(n):
            t = gh.estimate(n)
            v = float(re.search(r"about ([\d.]+)", t).group(1))
            return v * (60 if " min" in t else 3600 if " hours" in t else 1)

        assert secs(2) < secs(45) < secs(1198)

    def test_threshold_marks_a_bootstrap(self):
        assert 1198 >= gh.TARBALL_THRESHOLD
        assert 25 < gh.TARBALL_THRESHOLD


class TestCancel:
    """A long download must be interruptible, and leave nothing half-written."""

    class CancellingTransport(FakeTransport):
        def get_bytes(self, url, on_chunk=None):
            if on_chunk is not None:
                on_chunk(1 << 20, 4 << 20)      # caller says stop
                raise gh.Cancelled("cancelled")
            return super().get_bytes(url)

    def test_cancel_during_download_writes_nothing(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        tr = self.CancellingTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        with pytest.raises(gh.Cancelled):
            S.sync(src, mpath, panel, tr, F3D, dry_run=False, threshold=1)

        assert panel.total_files == 0
        assert panel.duplicates() == {}

    def test_cancel_does_not_stamp_the_manifest(self, small_tree, src, tmp_path):
        """An aborted run has not checked anything. Saying it has would make
        the next run trust a tree it never finished reading."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        head, rest = dict(list(small_tree.items())[:3]), dict(small_tree)

        settled = FakeTransport(head)
        settled.commit = "commit_AAA"
        S.sync(src, mpath, panel, settled, F3D, dry_run=False)
        was = Manifest.load(mpath).synced_at
        assert was

        # More files upstream now, and the download is interrupted part way.
        aborted = self.CancellingTransport(rest)
        aborted.commit = "commit_BBB"
        with pytest.raises(gh.Cancelled):
            S.sync(src, mpath, panel, aborted, F3D, dry_run=False, threshold=1)

        m = Manifest.load(mpath)
        assert m.synced_at == was
        assert m.synced_commit == "commit_AAA"


class TestManifestHeader:
    """The header answers 'when did this last run, against what'. Every field
    in it was stale in some way: synced_at was never assigned at all, ref was
    frozen at creation, and synced_commit only moved when a file happened to
    be uploaded."""

    def test_a_sync_records_when_it_ran(self, small_tree, src, tmp_path):
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, FakeDataPanel(), FakeTransport(small_tree), F3D,
               dry_run=False)
        assert Manifest.load(mpath).synced_at

    def test_a_run_with_nothing_to_add_still_records_the_check(
            self, small_tree, src, tmp_path):
        """The steady state. If only uploads stamp the manifest, a library that
        is up to date looks like one that was never checked."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")

        first = FakeTransport(small_tree)
        first.commit = "commit_AAA"
        S.sync(src, mpath, panel, first, F3D, dry_run=False)

        moved = FakeTransport(small_tree)          # repo advanced, same files
        moved.commit = "commit_BBB"
        plan, _ = S.sync(src, mpath, panel, moved, F3D, dry_run=False)

        assert plan.add == []
        m = Manifest.load(mpath)
        assert m.synced_commit == "commit_BBB"

    def test_ref_follows_the_config(self, small_tree, tmp_path):
        """Editing ref in config.json must not leave the manifest lying about
        which release it tracks."""
        repo = "VEX-CAD/VEX-CAD-Fusion-360-Library"
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")

        S.sync(gh.Source(repo, "v2.0.3"), mpath, panel,
               FakeTransport(small_tree), F3D, dry_run=False)
        assert Manifest.load(mpath).ref == "v2.0.3"

        S.sync(gh.Source(repo, "v2.0.5"), mpath, panel,
               FakeTransport(small_tree), F3D, dry_run=False)
        assert Manifest.load(mpath).ref == "v2.0.5"

    def test_preview_never_stamps(self, small_tree, src, tmp_path):
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, FakeDataPanel(), FakeTransport(small_tree), F3D,
               dry_run=True)
        m = Manifest.load(mpath)
        assert m is None or m.synced_at is None

    def test_stamping_does_not_disturb_the_entries(self, small_tree, src,
                                                   tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        before = {p: e.blob for p, e in Manifest.load(mpath).files.items()}

        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)

        assert {p: e.blob for p, e in Manifest.load(mpath).files.items()} == before
        assert panel.duplicates() == {}


class TestAsyncUpload:
    """Fusion completes uploads on the event loop, so a fired upload lands in
    the folder while its future still reports Processing. Blocking on that
    future is what turned a ~10s upload into a 300s timeout."""

    def test_deferred_uploads_are_resolved_by_looking_at_the_folder(
            self, small_tree, src, tmp_path):
        panel = FakeDataPanel(deferred=True)      # begin_upload returns None
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert len(rep.added) == len(small_tree)
        assert panel.total_files == len(small_tree)
        assert panel.duplicates() == {}
        m = Manifest.load(mpath)
        assert m.in_state(INFLIGHT) == {}, "everything must resolve to placed"
        assert all(e.lineage for e in m.files.values()), "lineage must be recorded"

    def test_deferred_sync_is_still_idempotent(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(deferred=True)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        S.sync(src, mpath, panel, tr, F3D, dry_run=False)
        n = panel.total_files
        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert plan.is_empty
        assert panel.total_files == n
        assert panel.duplicates() == {}

    def test_upload_that_never_resolves_is_reported_not_silently_lost(
            self, src, tmp_path):
        class Stuck(FakeDataPanel):
            def begin_upload(self, folder_id, local_path, name):
                return {"placed": None, "polls": 0}   # nothing ever lands
            def poll_upload(self, handle):
                return None                           # forever processing

        panel, tr = Stuck(), FakeTransport({"A/Part.f3d": "sha"})
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=2.0)

        assert rep.added == []
        assert any("unresolved" in msg for _, msg in rep.failures)

    def test_lineage_comes_from_our_own_upload_not_a_name_match(
            self, src, tmp_path):
        """Polling the future gives the identity of the file WE created, so a
        same-named neighbour cannot be mistaken for ours."""
        tree = {"A/Part.f3d": "sha"}
        panel, tr = FakeDataPanel(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        folder = panel.ensure_folder(("A",))
        decoy = panel.upload(folder, "/tmp/x", "Part")   # someone else's file

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        # Two files named Part: the scan cannot tell them apart, so the
        # future is consulted and its lineage wins.
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None, "gave up instead of asking the future"
        assert entry.lineage != decoy.lineage, "recorded the decoy"
        assert entry.state == PLACED

    def test_upload_failure_during_settle_is_reported(self, src, tmp_path):
        from detent_core.datapanel import UploadFailed as UF

        class Failing(FakeDataPanel):
            def begin_upload(self, folder_id, local_path, name):
                return {"placed": None, "polls": 0}    # nothing lands
            def poll_upload(self, handle):
                raise UF("upload state 2")

        panel, tr = Failing(), FakeTransport({"A/Part.f3d": "sha"})
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=5.0)

        assert any("state 2" in msg for _, msg in rep.failures)
        # begin_upload was already called, so the file may be in the cloud.
        # Dropping the entry here is what let a later run upload a second
        # copy; it stays inflight and the next run looks before retrying.
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None and entry.state == INFLIGHT

    def test_no_blocking_wait_on_the_future(self):
        """Regression guard for the actual bug: sync must not call the
        blocking upload() path."""
        import inspect
        src_text = inspect.getsource(S.apply_plan)
        assert "begin_upload" in src_text
        assert "on_wait" not in src_text
        assert ".upload(" not in src_text


class TestSettleResilience:
    """settle() polls a live API. It must be frugal and must not die on a
    transient error."""

    def test_one_listing_per_folder_per_pass(self, src, tmp_path):
        """Regression: asking per file meant a refresh() round trip for every
        pending path, every second."""
        tree = {f"A/Part{i}.f3d": f"s{i}" for i in range(20)}

        class Counting(FakeDataPanel):
            listings = 0

            def list_folder(self, folder_id):
                Counting.listings += 1
                return super().list_folder(folder_id)

        Counting.listings = 0
        panel, tr = Counting(deferred=True), FakeTransport(tree)
        S.sync(src, str(tmp_path / "m.json"), panel, tr, F3D, dry_run=False)

        assert Counting.listings <= 3, (
            f"{Counting.listings} listings for 20 files in one folder")

    def test_transient_panel_error_is_retried_not_fatal(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha"}

        class Flaky(FakeDataPanel):
            calls = 0

            def list_folder(self, folder_id):
                Flaky.calls += 1
                if Flaky.calls <= 2:
                    raise RuntimeError("RuntimeError: 3 : transient")
                return super().list_folder(folder_id)

        Flaky.calls = 0
        panel, tr = Flaky(deferred=True), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=30.0)

        assert rep.added == ["A/Part.f3d"], "should recover and record"
        assert Manifest.load(mpath).get("A/Part.f3d").state == PLACED

    def test_settle_scans_first_and_polls_as_fallback(self):
        """Measured: a fired file appears in dataFiles at t=0, while
        uploadState takes 18-50s. So scan, and only poll what the scan
        misses."""
        import inspect
        body = inspect.getsource(S.settle)
        assert "list_folder" in body, "the fast path is the folder scan"
        assert "poll_upload" in body, "the future is the fallback"
        assert body.index("list_folder") < body.index("poll_upload")

    def test_nothing_calls_refresh_on_a_folder(self):
        """DataFolder has no refresh(). Parse the AST rather than grep the
        text, so a docstring mentioning it cannot pass or fail us."""
        import ast as _ast
        import inspect
        from detent_core import datapanel as DP

        for mod in (S, DP):
            tree = _ast.parse(inspect.getsource(mod))
            calls = [n for n in _ast.walk(tree)
                     if isinstance(n, _ast.Call)
                     and isinstance(n.func, _ast.Attribute)
                     and n.func.attr == "refresh"]
            assert not calls, f"{mod.__name__} calls .refresh()"


class TestFusionPollLogic:
    """FusionDataPanel.poll_upload touches no adsk API, so its state machine
    IS testable — with a fake future. Previously it was not covered at all,
    and a mutation flipping the Processing check passed every test."""

    def _panel(self):
        from detent_core.datapanel import FusionDataPanel
        return object.__new__(FusionDataPanel)      # skip the adsk import

    class Future:
        def __init__(self, state, data_file=None, raises=False):
            self._state, self._df, self._raises = state, data_file, raises

        @property
        def uploadState(self):
            if self._raises:
                raise RuntimeError("InternalValidationError")
            return self._state

        @property
        def dataFile(self):
            return self._df

    class DF:
        name, id, versionNumber = "Part", "urn:adsk:lineage:1", 1

    def test_processing_returns_none(self):
        h = {"future": self.Future(0), "name": "Part"}
        assert self._panel().poll_upload(h) is None

    def test_finished_returns_the_placed_file(self):
        h = {"future": self.Future(1, self.DF()), "name": "Part"}
        placed = self._panel().poll_upload(h)
        assert placed.lineage == "urn:adsk:lineage:1"
        assert placed.name == "Part"

    def test_failed_state_raises(self):
        from detent_core.datapanel import UploadFailed
        h = {"future": self.Future(2), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)

    def test_finished_without_a_datafile_raises(self):
        from detent_core.datapanel import UploadFailed
        h = {"future": self.Future(1, None), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)

    def test_a_raising_future_becomes_UploadFailed_not_a_crash(self):
        from detent_core.datapanel import UploadFailed
        h = {"future": self.Future(0, raises=True), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)


class TestScanIsTheFastPath:
    """The folder scan is not decoration. Measured: a fired file is in
    dataFiles at t=0 while uploadState takes 18-50s. If the scan stops being
    used, everything still works — just many times slower — so assert it is
    actually doing the resolving."""

    def test_deferred_upload_resolves_without_waiting_for_the_future(
            self, small_tree, src, tmp_path):
        polls = {"n": 0}

        class SlowFuture(FakeDataPanel):
            def poll_upload(self, handle):
                polls["n"] += 1
                return None          # the future never helps, as at t=0

        panel, tr = SlowFuture(), FakeTransport(small_tree)
        plan, rep = S.sync(src, str(tmp_path / "m.json"), panel, tr, F3D,
                           dry_run=False, settle_wait=5.0)

        assert len(rep.added) == len(small_tree), "scan failed to resolve them"
        assert polls["n"] == 0, "fell back to the future when the scan sufficed"

    def test_resolution_happens_in_a_single_pass(self, small_tree, src, tmp_path):
        passes = {"n": 0}

        class Counting(FakeDataPanel):
            def list_folder(self, folder_id):
                passes["n"] += 1
                return super().list_folder(folder_id)

        panel, tr = Counting(), FakeTransport(small_tree)
        S.sync(src, str(tmp_path / "m.json"), panel, tr, F3D, dry_run=False)

        folders = {p.rsplit("/", 1)[0] for p in small_tree}
        assert passes["n"] <= len(folders) + 1, (
            f"{passes['n']} listings for {len(folders)} folders — "
            "should resolve on the first pass")


class TestDrift:
    """The manifest is bookkeeping. When it disagrees with the Data Panel, the
    Data Panel is right - it is the thing the user can actually see."""

    def test_the_reported_case(self, src, tmp_path):
        """Sync at v2.0.3, sync at v2.0.5, revert the ref, delete the folder.
        Before drift detection this reported '0 to add' over an empty folder:
        a confident all-clear with nothing on the shelf.
        """
        repo = "VEX-CAD/VEX-CAD-Fusion-360-Library"
        old = {f"Field/{i}.f3d": f"sha{i}" for i in range(15)}
        new = dict(old, **{f"Field/Override/{i}.f3d": f"o{i}" for i in range(33)})
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")

        S.sync(gh.Source(repo, "v2.0.3"), mpath, panel, FakeTransport(old),
               F3D, dry_run=False)
        S.sync(gh.Source(repo, "v2.0.5"), mpath, panel, FakeTransport(new),
               F3D, dry_run=False)
        assert len(Manifest.load(mpath).files) == 48

        # user deletes the whole folder by hand
        for fid in panel.contents:
            panel.contents[fid] = []

        plan, _ = S.sync(gh.Source(repo, "v2.0.3"), mpath, panel,
                         FakeTransport(old), F3D, dry_run=True)

        assert plan.orphan and len(plan.orphan) == 33, "33 really are gone upstream"
        assert plan.add == [], "nothing is new; they are re-placements"
        assert len(plan.missing) == 15, (
            "the 15 files that should exist at v2.0.3 are not there")
        assert not plan.is_empty

    def test_a_sync_puts_missing_files_back_exactly_once(self, small_tree, src,
                                                         tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        for fid in panel.contents:
            panel.contents[fid] = []

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False)

        assert len(rep.replaced) == len(small_tree)
        assert panel.total_files == len(small_tree)
        assert panel.duplicates() == {}, "re-placing must not double anything"

        _p2, rep2 = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False)
        assert rep2.added == [], "and it settles again straight away"
        assert panel.duplicates() == {}

    def test_only_the_deleted_file_comes_back(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        before = panel.total_files

        victim = None
        for fid, files in panel.contents.items():
            if files:
                victim = files.pop(0)
                break

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False)

        assert len(rep.replaced) == 1
        assert panel.total_files == before
        assert panel.duplicates() == {}
        assert victim is not None

    def test_a_name_already_taken_is_never_overwritten(self, src, tmp_path):
        """Someone deleted our file and put their own there under the same
        name. Re-placing would sit a second file beside it - the exact
        duplicate this tool exists to prevent. Report, do not upload."""
        tree = {"A/Part.f3d": "sha1"}
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False)

        folder = panel.folders[("A",)]
        panel.contents[folder] = []                  # ours is deleted
        impostor = panel.upload(folder, "/tmp/x", "Part")   # theirs arrives

        plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                           dry_run=False)

        assert plan.conflict == ["A/Part.f3d"]
        assert plan.missing == []
        assert rep.conflicts and not rep.ok
        assert panel.total_files == 1, "we uploaded nothing"
        assert panel.list_folder(folder) == [impostor]
        assert panel.duplicates() == {}

    def test_a_failed_scan_never_re_uploads_the_library(self, small_tree, src,
                                                        tmp_path):
        """Reading a broken scan as 'everything is gone' would duplicate an
        entire library on one transient API error."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        before = panel.total_files

        def boom():
            raise RuntimeError("Data Panel unreachable")
        panel.scan = boom

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False)

        assert plan.missing == [] and plan.conflict == []
        assert panel.total_files == before
        assert panel.duplicates() == {}

    def test_preview_detects_but_writes_nothing(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        for fid in panel.contents:
            panel.contents[fid] = []

        plan, _ = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                         dry_run=True)

        assert len(plan.missing) == len(small_tree)
        assert panel.total_files == 0, "preview re-placed nothing"

    def test_verify_placed_off_keeps_the_old_blindness(self, small_tree, src,
                                                       tmp_path):
        """Opting out is allowed - it is a per-folder listing - but it is
        exactly the blindness that caused this, so it must be deliberate."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        for fid in panel.contents:
            panel.contents[fid] = []

        plan, _ = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                         dry_run=True, verify_placed=False)

        assert plan.missing == []
        assert plan.is_empty


class TestSettleNeverDropsAFiredUpload:
    """begin_upload has been called, so the file may already be in the cloud.
    The manifest entry is the only thing stopping a second copy; nothing in
    settle may remove it. reconcile_inflight settles it next run by looking."""

    class Ambiguous(FakeDataPanel):
        def poll_upload(self, handle):
            return None                      # future never resolves

    def test_unresolvable_upload_does_not_duplicate_next_run(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha1"}
        panel = self.Ambiguous()
        folder = panel.ensure_folder(("A",))
        stranger = panel.upload(folder, "/tmp/x", "Part")   # a stranger sits there
        mpath = str(tmp_path / "m.json")

        before = panel.total_files
        for _ in range(3):
            S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                   dry_run=False, settle_wait=1.2)

        assert panel.total_files == before + 1, (
            "one upload total across three runs; the entry must survive")
        # And it is ours, not the stranger's, even though the future never
        # resolved - the pre-existing lineage is what rules the stranger out.
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry.lineage not in {f.lineage
                                     for f in panel.list_folder(folder)
                                     if f.lineage == stranger.lineage}

    def test_upload_error_leaves_the_entry_inflight(self, src, tmp_path):
        class Erroring(FakeDataPanel):
            """Fires, nothing appears in the folder, and the future errors -
            so neither the scan nor the future can settle it."""
            def begin_upload(self, folder_id, local_path, name):
                return {"placed": None, "polls": 0}
            def poll_upload(self, handle):
                raise UploadFailed("uploadState raised: RuntimeError")

        tree = {"A/Part.f3d": "sha1"}
        panel, mpath = Erroring(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               settle_wait=1.2)
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None and entry.state == INFLIGHT


    def test_ambiguous_unresolved_upload_is_not_dropped(self, src, tmp_path):
        """Two same-named files appear that were BOTH absent when we started,
        and the future never says which is ours. Dropping the entry here is
        what let the next run add a third."""
        class DoubleLanding(FakeDataPanel):
            def begin_upload(self, folder_id, local_path, name):
                a = FakeDataPanel.upload(self, folder_id, local_path, name)
                FakeDataPanel.upload(self, folder_id, local_path, name)
                return {"placed": a, "polls": 0}
            def poll_upload(self, handle):
                return None                    # never identifies itself

        tree = {"A/Part.f3d": "sha1"}
        panel, mpath = DoubleLanding(), str(tmp_path / "m.json")

        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               settle_wait=1.2)
        after_first = panel.total_files
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None and entry.state == INFLIGHT, (
            "a fired upload must stay recorded even when unresolvable")

        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               settle_wait=1.2)
        assert panel.total_files == after_first, "re-uploaded an unresolved path"

class TestSettleClaimsOnlyItsOwnUploads:
    def test_a_file_that_was_already_there_is_never_claimed(self, src, tmp_path):
        """One name hit is not proof of ownership. Identity is the lineage."""
        class LateArrival(FakeDataPanel):
            def begin_upload(self, folder, local_path, name):
                return {"f": folder, "p": local_path, "n": name, "done": False}
            def poll_upload(self, h):
                if not h["done"]:
                    h["done"] = True
                    h["placed"] = FakeDataPanel.upload(self, h["f"], h["p"], h["n"])
                return h["placed"]

        tree = {"A/Part.f3d": "sha1"}
        panel = LateArrival()
        folder = panel.ensure_folder(("A",))
        stranger = panel.upload(folder, "/tmp/x", "Part")
        mpath = str(tmp_path / "m.json")

        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               settle_wait=3.0)

        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry.lineage != stranger.lineage, "recorded someone else's file"


class TestReconciledBlobIsReal:
    def test_a_recovered_file_is_not_changed_forever(self, src, tmp_path):
        """blob="" is neither None nor any real SHA, so diff() called it
        'changed upstream' on every sync from then on."""
        tree = {"A/Part.f3d": "sha1"}
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        folder = panel.ensure_folder(("A",))
        panel.upload(folder, "/tmp/x", "Part")
        m = Manifest("s", src.repo)
        m.mark_inflight("A/Part.f3d", placed_name="Part")
        m.save(mpath)

        for _ in range(2):
            plan, _rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                                dry_run=False)
            assert plan.change == []
        assert Manifest.load(mpath).get("A/Part.f3d").blob == "sha1"


class TestBlockedRunDoesNotClaimToHaveChecked:
    def test_a_collision_blocks_the_stamp(self, src, tmp_path):
        collide = {"A/Part.f3d": "s1", "A/Part.step": "s2"}   # one panel name
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(collide),
                            ("**/*",), dry_run=False)

        assert rep.collisions and panel.total_files == 0
        m = Manifest.load(mpath)
        assert m is None or m.synced_commit is None, (
            "a run that placed nothing must not record a successful check")


class TestTransientFailures:
    """GitHub builds codeload archives on demand and the big ones time out.
    Reported live: HTTP 504 on a first full-library sync."""

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError("http://x", code, "boom", {}, None)

    def test_a_504_is_retried_and_then_succeeds(self):
        import urllib.request
        calls = {"n": 0}

        class Body:
            headers = {"Content-Length": "2"}
            def read(self, *a): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._http_error(504)
            return Body()

        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            t = gh.UrllibTransport(sleep=lambda s: None)
            assert t.get_bytes("http://x") == b"ok"
        finally:
            urllib.request.urlopen = real
        assert calls["n"] == 3, "should have retried twice then succeeded"

    def test_a_404_is_not_retried(self):
        import urllib.request, urllib.error
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise self._http_error(404)

        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            t = gh.UrllibTransport(sleep=lambda s: None)
            with pytest.raises(urllib.error.HTTPError):
                t.get_bytes("http://x")
        finally:
            urllib.request.urlopen = real
        assert calls["n"] == 1, "a 404 will never become a 200"

    def test_giving_up_names_the_url(self):
        import urllib.request
        def fake_urlopen(req, timeout=None):
            raise self._http_error(504)
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            t = gh.UrllibTransport(retries=1, sleep=lambda s: None)
            with pytest.raises(gh.GitHubError) as e:
                t.get_json("http://example/thing")
        finally:
            urllib.request.urlopen = real
        assert "example/thing" in str(e.value) and "2 attempts" in str(e.value)

    def test_a_dead_archive_falls_back_to_individual_files(self, small_tree,
                                                           src, tmp_path):
        """The whole point: a first sync that cannot get the tarball must still
        finish, not abort the run."""
        class NoTarball(FakeTransport):
            def get_bytes(self, url, on_chunk=None):
                if "codeload" in url:
                    raise gh.GitHubError("504 Gateway Timeout")
                return super().get_bytes(url, on_chunk)

        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        _plan, rep = S.sync(src, mpath, panel, NoTarball(small_tree), F3D,
                            dry_run=False, threshold=1)   # force the tarball path

        assert len(rep.added) == len(small_tree)
        assert panel.total_files == len(small_tree)
        assert panel.duplicates() == {}

    def test_cancelling_during_the_archive_is_not_treated_as_a_failure(
            self, small_tree, src, tmp_path):
        """Cancel must stay cancel; falling back would restart the download
        the user just stopped."""
        class Cancelling(FakeTransport):
            def get_bytes(self, url, on_chunk=None):
                if "codeload" in url:
                    raise gh.Cancelled("cancelled")
                return super().get_bytes(url, on_chunk)

        panel = FakeDataPanel()
        with pytest.raises(gh.Cancelled):
            S.sync(src, str(tmp_path / "m.json"), panel, Cancelling(small_tree),
                   F3D, dry_run=False, threshold=1)
        assert panel.total_files == 0
