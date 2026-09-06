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


class TestAdopt:
    """Claiming a library the user already imported. Uploads nothing."""

    def _panel_with(self, tree, src):
        from core import paths as P
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
            def poll_upload(self, handle):
                return None                        # forever processing

        panel, tr = Stuck(), FakeTransport({"A/Part.f3d": "sha"})
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=2.0)

        assert rep.added == []
        assert any("still processing" in msg for _, msg in rep.failures)

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

        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry.lineage != decoy.lineage, "recorded the decoy"
        assert entry.state == PLACED

    def test_upload_failure_during_settle_is_reported(self, src, tmp_path):
        from core.datapanel import UploadFailed as UF

        class Failing(FakeDataPanel):
            def poll_upload(self, handle):
                raise UF("upload state 2")

        panel, tr = Failing(), FakeTransport({"A/Part.f3d": "sha"})
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=5.0)

        assert any("state 2" in msg for _, msg in rep.failures)
        assert "A/Part.f3d" not in Manifest.load(mpath), "must stay retryable"

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

    def test_no_folder_enumeration_during_settle(self):
        """Regression: settle used to list the folder once per pending file
        per pass, and DataFolder has no refresh() to make that cheap or even
        valid. It now polls futures instead."""
        import inspect
        body = inspect.getsource(S.settle)
        assert "poll_upload" in body
        assert "list_folder" not in body
        assert "find_by_name" not in body


class TestFusionPollLogic:
    """FusionDataPanel.poll_upload touches no adsk API, so its state machine
    IS testable — with a fake future. Previously it was not covered at all,
    and a mutation flipping the Processing check passed every test."""

    def _panel(self):
        from core.datapanel import FusionDataPanel
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
        from core.datapanel import UploadFailed
        h = {"future": self.Future(2), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)

    def test_finished_without_a_datafile_raises(self):
        from core.datapanel import UploadFailed
        h = {"future": self.Future(1, None), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)

    def test_a_raising_future_becomes_UploadFailed_not_a_crash(self):
        from core.datapanel import UploadFailed
        h = {"future": self.Future(0, raises=True), "name": "Part"}
        with pytest.raises(UploadFailed):
            self._panel().poll_upload(h)
