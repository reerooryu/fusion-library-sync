"""Sync tests against a fake Data Panel that, like Fusion, allows two files
with the same name. Every test checks that sync never creates one."""

import os
import sys
import urllib.parse
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
    """Serves a fixture tree and made-up file contents."""

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


class ArchiveTransport(FakeTransport):
    """Fails raw downloads for `fail_raw` paths (all, if True) and serves a
    real .tar.gz of the tree, reporting bytes as it goes. Records every URL."""

    def __init__(self, tree, fail_raw=True, archive_error=None, **kw):
        super().__init__(tree, **kw)
        self.fail_raw = fail_raw
        self.archive_error = archive_error
        self.urls = []

    def get_bytes(self, url, on_chunk=None):
        self.urls.append(url)
        if "codeload" not in url:
            path = urllib.parse.unquote("/".join(url.split("/")[6:]))
            if self.fail_raw is True or path in (self.fail_raw or ()):
                raise OSError("429 Too Many Requests")
            return super().get_bytes(url)
        if self.archive_error:
            raise self.archive_error
        import io, tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in self.tree:
                body = f"content-of:{path}".encode()
                info = tarfile.TarInfo(f"repo-c0ffee/{path}")
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        data = buf.getvalue()
        if on_chunk is not None:
            for got in (len(data) // 4, len(data) // 2, len(data) * 3 // 4, len(data)):
                if on_chunk(got, len(data)) is False:
                    raise gh.Cancelled("download cancelled")
        return data

    @property
    def archive_requests(self):
        return sum("codeload" in u for u in self.urls)


@pytest.fixture
def small_tree():
    """A few files, including awkward names. Small enough to skip the archive."""
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
        """The fake must allow duplicates, or these tests prove nothing."""
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
    """A crash mid-sync must never cause duplicates."""

    def test_interrupted_sync_leaves_an_inflight_record(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(crash_after=3)
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        with pytest.raises(KeyboardInterrupt):
            S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        m = Manifest.load(mpath)
        assert m is not None, "manifest must survive a crash"
        # A crash while starting uploads leaves them all inflight.
        # reconcile_inflight checks the folder on the next run.
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
        """The upload succeeded but the manifest wasn't updated."""
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
    """One bad file doesn't stop the run."""

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
        """An unreported rename becomes a duplicate on the next sync."""
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
    """Adopt records an existing library and uploads nothing."""

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
        """With no release given, Adopt still matches files. It used to record
        nothing."""
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
    """The confirm dialog shows size and time, not just a count."""

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


class TestCancel:
    """Cancel stops the download and leaves nothing half-done."""

    @staticmethod
    def stop_while_downloading(i, n, label):
        return not label.startswith("Downloading")

    def test_cancel_during_download_writes_nothing(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")

        with pytest.raises(gh.Cancelled):
            S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                   dry_run=False, on_progress=self.stop_while_downloading)

        assert panel.total_files == 0
        assert panel.duplicates() == {}

    def test_cancel_does_not_stamp_the_manifest(self, small_tree, src, tmp_path):
        """A cancelled run must not be recorded as a completed sync."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        head, rest = dict(list(small_tree.items())[:3]), dict(small_tree)

        settled = FakeTransport(head)
        settled.commit = "commit_AAA"
        S.sync(src, mpath, panel, settled, F3D, dry_run=False)
        was = Manifest.load(mpath).synced_at
        assert was

        # More files upstream now, and the download is interrupted part way.
        aborted = FakeTransport(rest)
        aborted.commit = "commit_BBB"
        with pytest.raises(gh.Cancelled):
            S.sync(src, mpath, panel, aborted, F3D, dry_run=False,
                   on_progress=self.stop_while_downloading)

        m = Manifest.load(mpath)
        assert m.synced_at == was
        assert m.synced_commit == "commit_AAA"


class TestManifestHeader:
    """The manifest header records when, and against what, the last sync ran."""

    def test_a_sync_records_when_it_ran(self, small_tree, src, tmp_path):
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, FakeDataPanel(), FakeTransport(small_tree), F3D,
               dry_run=False)
        assert Manifest.load(mpath).synced_at

    def test_a_run_with_nothing_to_add_still_records_the_check(
            self, small_tree, src, tmp_path):
        """A sync with nothing to upload still records that it ran."""
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
        """Changing ref in config.json updates the manifest."""
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
    """Uploads are started and confirmed later. Waiting on each one took
    minutes."""

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
        assert any("not confirmed" in msg for _, msg in rep.failures)

    def test_lineage_comes_from_our_own_upload_not_a_name_match(
            self, src, tmp_path):
        """With two files of the same name, the upload handle picks ours. The
        decoy arrives after the scan, the only way one gets this far."""
        tree = {"A/Part.f3d": "sha"}

        class RaceIn(FakeDataPanel):
            decoy = None

            def begin_upload(self, folder_id, local_path, name):
                if self.decoy is None:
                    self.decoy = FakeDataPanel.upload(
                        self, folder_id, local_path, name)
                return super().begin_upload(folder_id, local_path, name)

        panel, tr = RaceIn(), FakeTransport(tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        # Two files named Part: the upload handle decides.
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None, "gave up instead of asking the future"
        assert panel.decoy is not None
        assert entry.lineage != panel.decoy.lineage, "recorded the decoy"
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
        # The file may already be in the cloud, so the entry stays inflight.
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None and entry.state == INFLIGHT

    def test_no_blocking_wait_on_the_future(self):
        """sync must never wait on an upload."""
        import inspect
        src_text = inspect.getsource(S.apply_plan)
        assert "begin_upload" in src_text
        assert "on_wait" not in src_text
        assert ".upload(" not in src_text


class TestSettleResilience:
    """settle must not overload the API or fail on a temporary error."""

    def test_one_listing_per_folder_per_pass(self, src, tmp_path):
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
        """Files appear in the folder at once; upload status takes 18-50 s."""
        import inspect
        body = inspect.getsource(S.settle)
        assert "list_folder" in body, "the fast path is the folder scan"
        assert "poll_upload" in body, "the future is the fallback"
        assert body.index("list_folder") < body.index("poll_upload")

    def test_nothing_calls_refresh_on_a_folder(self):
        """DataFolder has no refresh(). Checks the code, not the comments."""
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
    """FusionDataPanel.poll_upload, tested with a fake upload handle."""

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
    """Uploads must be confirmed from the folder listing, not by waiting."""

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
    """When the manifest and the Data Panel disagree, the Data Panel wins."""

    def test_the_reported_case(self, src, tmp_path):
        """Sync, delete the folder by hand, preview. It used to say '0 to add'."""
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
        """Our file was deleted and another took its name. Report it, don't
        upload."""
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
        """A failed scan must not look like everything is gone."""
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
        """With verify_placed off, deleted files aren't noticed."""
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
    """A started upload may already be in the cloud, so settle never drops its
    manifest entry."""

    class Ambiguous(FakeDataPanel):
        def poll_upload(self, handle):
            return None                      # future never resolves

    def test_unresolvable_upload_does_not_duplicate_next_run(self, src, tmp_path):
        """A same-named file arrives after the scan, and the upload never
        confirms."""
        tree = {"A/Part.f3d": "sha1"}

        class AmbiguousRace(FakeDataPanel):
            stranger = None

            def begin_upload(self, folder_id, local_path, name):
                if self.stranger is None:
                    self.stranger = FakeDataPanel.upload(
                        self, folder_id, local_path, name)
                return super().begin_upload(folder_id, local_path, name)

            def poll_upload(self, handle):
                return None                  # future never resolves

        panel = AmbiguousRace()
        mpath = str(tmp_path / "m.json")

        for _ in range(3):
            S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                   dry_run=False, settle_wait=1.2)

        assert panel.total_files == 2, (
            "one upload (plus the stranger) across three runs; "
            "the entry must survive to stop the next one")
        entry = Manifest.load(mpath).get("A/Part.f3d")
        assert entry is not None, "a fired upload must stay recorded"

    def test_upload_error_leaves_the_entry_inflight(self, src, tmp_path):
        class Erroring(FakeDataPanel):
            """Nothing appears in the folder and the upload status errors."""
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
        """Two new files with the same name, and no way to tell which is ours."""
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
    def test_a_stranger_already_there_means_we_never_upload(self, src, tmp_path):
        """A file already using the name is never claimed, and nothing is
        uploaded."""
        tree = {"A/Part.f3d": "sha1"}
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        stranger = panel.upload(folder, "/tmp/x", "Part")
        mpath = str(tmp_path / "m.json")

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                            dry_run=False, settle_wait=3.0)

        assert rep.occupied == [("A/Part.f3d", "Part")]
        assert rep.added == [] and not rep.ok
        assert panel.list_folder(folder) == [stranger], "uploaded nothing"
        assert panel.duplicates() == {}
        assert Manifest.load(mpath).get("A/Part.f3d") is None

    def test_settle_still_refuses_a_pre_existing_lineage(self, tmp_path):
        """Backup check: settle never claims a file that was there before the
        run."""
        from detent_core import paths as P

        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        stranger = panel.upload(folder, "/tmp/x", "Part")
        panel.poll_upload = lambda handle: None      # future never resolves

        manifest = Manifest(source_id="s", repo="o/r")
        manifest.mark_inflight("A/Part.f3d", placed_name="Part")
        report = S.Report()
        mpath = str(tmp_path / "m.json")

        S.settle(manifest, panel, {"A/Part.f3d": "sha1"}, report, mpath,
                 {"A/Part.f3d": {"placed": None, "polls": 0}}, None, 1.2,
                 mapped_by_path={"A/Part.f3d": P.map_path("A/Part.f3d")},
                 pre_existing={stranger.lineage})

        entry = manifest.get("A/Part.f3d")
        assert entry is not None and entry.state == INFLIGHT
        assert entry.lineage is None, "claimed the stranger's lineage"
        assert panel.list_folder(folder) == [stranger]


class TestReconciledBlobIsReal:
    def test_a_recovered_file_is_not_changed_forever(self, src, tmp_path):
        """blob="" made recovered files show as changed forever."""
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
    """GitHub timeouts, e.g. a 504 on a big archive."""

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

    def test_many_failed_downloads_fall_back_to_the_archive(self, small_tree,
                                                           src, tmp_path):
        """What a GitHub rate limit looks like: every raw download refused."""
        tr = ArchiveTransport(small_tree, fail_raw=True)
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        _plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                            fallback_at=3)

        assert tr.archive_requests == 1
        assert rep.failures == []
        assert len(rep.added) == len(small_tree)
        assert panel.duplicates() == {}

    def test_a_few_failures_do_not_fetch_the_archive(self, small_tree, src,
                                                     tmp_path):
        bad = sorted(small_tree)[0]
        tr = ArchiveTransport(small_tree, fail_raw=[bad])
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(), tr,
                            F3D, dry_run=False, fallback_at=3)

        assert tr.archive_requests == 0, "2.2 GB is not worth one file"
        assert [p for p, _m in rep.failures] == [bad]
        assert len(rep.added) == len(small_tree) - 1

    def test_a_dead_archive_keeps_the_original_failures(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(5)}
        tr = ArchiveTransport(tree, fail_raw=True,
                              archive_error=gh.GitHubError("504 Gateway Timeout"))
        fetched, failures = gh.fetch_files(src, sorted(tree), str(tmp_path), tr,
                                           "c0ffee", fallback_at=3)
        assert fetched == {}
        assert sorted(p for p, _m in failures) == sorted(tree)
        assert all("429" in m for _p, m in failures), "the real reason is kept"

    def test_the_archive_only_fills_the_gaps(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(6)}
        failing = sorted(tree)[:4]
        tr = ArchiveTransport(tree, fail_raw=failing)
        fetched, failures = gh.fetch_files(src, sorted(tree), str(tmp_path), tr,
                                           "c0ffee", fallback_at=3)
        assert failures == [] and sorted(fetched) == sorted(tree)
        for path, local in fetched.items():
            assert open(local).read() == f"content-of:{path}"

    def test_fallback_off(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(5)}
        tr = ArchiveTransport(tree, fail_raw=True)
        _f, failures = gh.fetch_files(src, sorted(tree), str(tmp_path), tr,
                                      "c0ffee", fallback_at=0)
        assert tr.archive_requests == 0 and len(failures) == 5

    def test_cancel_during_downloads_never_starts_the_archive(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(20)}
        tr = ArchiveTransport(tree, fail_raw=True)
        with pytest.raises(gh.Cancelled):
            gh.fetch_files(src, sorted(tree), str(tmp_path), tr, "c0ffee",
                           fallback_at=3, workers=1,
                           on_progress=lambda i, n, l: i < 2)
        assert tr.archive_requests == 0

    def test_cancel_during_the_archive_is_still_a_cancel(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(5)}
        tr = ArchiveTransport(tree, fail_raw=True)
        with pytest.raises(gh.Cancelled):
            gh.fetch_files(src, sorted(tree), str(tmp_path), tr, "c0ffee",
                           fallback_at=3,
                           on_progress=lambda i, n, l: "archive" not in l)


class TestDownloadProgress:
    """The progress bar must move during a long download."""

    def test_archive_progress_reports_bytes_and_advances(self, src, tmp_path):
        seen = []
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(8)}
        gh.fetch_files(src, sorted(tree), str(tmp_path),
                       ArchiveTransport(tree, fail_raw=True), "c0ffee",
                       fallback_at=1,
                       on_progress=lambda i, n, lbl: seen.append((i, n, lbl)))

        during = [s for s in seen if "archive" in s[2] and " of " in s[2]]
        assert during, f"no byte-level progress reported: {seen}"
        assert "B" in during[0][2] and "%" not in during[0][2], during[0][2]
        indexes = [i for i, _n, _l in during]
        assert indexes[0] < indexes[-1], f"progress never advanced: {indexes}"
        assert indexes[-1] == 8

    def test_the_label_survives_the_trip_through_sync(self, src, tmp_path):
        """Checks the label the add-in actually receives, not just fetch_files."""
        seen = []
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(6)}

        def progress(i, n, label):
            seen.append(label)
            return True

        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(), FakeTransport(tree),
               F3D, dry_run=False, on_progress=progress)
        assert "Downloading 6/6" in seen, seen

        seen.clear()
        S.sync(src, str(tmp_path / "m2.json"), FakeDataPanel(),
               ArchiveTransport(tree, fail_raw=True), F3D, dry_run=False,
               fallback_at=1, on_progress=progress)
        assert any("archive" in l and " of " in l for l in seen), seen

    def test_per_file_progress_counts_files(self, src, tmp_path):
        seen = []
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(4)}
        gh.fetch_files(gh.Source("o/r", "main"), sorted(tree), str(tmp_path),
                       FakeTransport(tree), "c0ffee",
                       on_progress=lambda i, n, lbl: seen.append((i, n, lbl)))          # force the per-file path
        assert [i for i, _n, _l in seen] == [1, 2, 3, 4]
        assert seen[-1][2] == "Downloading 4/4"


class TestSettleDeadlineScales:
    """The wait for uploads grows with the number of files."""

    def test_deadline_grows_with_the_batch(self, src, tmp_path, monkeypatch):
        seen = {}
        real_settle = S.settle

        def spy(manifest, panel, selected, report, mpath, handles,
                on_progress, max_wait, **kw):
            seen["wait"] = max_wait
            seen["n"] = len(handles)
            return real_settle(manifest, panel, selected, report, mpath,
                               handles, on_progress, max_wait, **kw)

        monkeypatch.setattr(S, "settle", spy)
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(400)}
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
               FakeTransport(tree), F3D, dry_run=False)
        assert seen["n"] == 400
        assert seen["wait"] >= 400, f"deadline did not scale: {seen}"

    def test_small_batches_keep_the_floor(self, small_tree, src, tmp_path,
                                          monkeypatch):
        seen = {}
        real_settle = S.settle

        def spy(*a, **kw):
            seen["wait"] = a[7]
            return real_settle(*a, **kw)

        monkeypatch.setattr(S, "settle", spy)
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
               FakeTransport(small_tree), F3D, dry_run=False)
        assert seen["wait"] == 300.0

    def test_an_explicit_wait_still_wins(self, small_tree, src, tmp_path,
                                        monkeypatch):
        seen = {}
        real_settle = S.settle
        monkeypatch.setattr(S, "settle",
                            lambda *a, **kw: (seen.setdefault("wait", a[7]),
                                              real_settle(*a, **kw))[1])
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
               FakeTransport(small_tree), F3D, dry_run=False, settle_wait=7.0)
        assert seen["wait"] == 7.0


class TestDownloadStrategy:
    """Files download individually, several at once. The archive downloads as
    one slow stream (10 Mbit/s against 117 measured), so it's only a fallback."""

    def test_the_whole_library_downloads_file_by_file(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(1198)}
        tr = ArchiveTransport(tree, fail_raw=False)
        fetched, failures = gh.fetch_files(src, sorted(tree), str(tmp_path), tr,
                                           "c0ffee")
        assert not failures and len(fetched) == 1198
        assert tr.archive_requests == 0

    def test_sizes_are_captured_from_the_tree(self):
        tree = gh.parse_tree({"truncated": False, "tree": [
            {"path": "a.f3d", "type": "blob", "sha": "s1", "size": 1000},
            {"path": "b.f3d", "type": "blob", "sha": "s2", "size": 2500},
            {"path": "dir", "type": "tree", "sha": "s3"},
        ]})
        assert tree == {"a.f3d": "s1", "b.f3d": "s2"}, "still a plain mapping"
        assert tree.total_bytes == 3500
        assert tree.bytes_for(["a.f3d"]) == 1000

    def test_a_tree_without_sizes_still_works(self):
        tree = gh.parse_tree({"truncated": False, "tree": [
            {"path": "a.f3d", "type": "blob", "sha": "s1"}]})
        assert tree.total_bytes == 0 and tree["a.f3d"] == "s1"


class TestParallelDownloads:
    def test_every_file_arrives_exactly_once(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(40)}
        fetched, failures = gh.fetch_files(
            src, sorted(tree), str(tmp_path), FakeTransport(tree), "c0ffee")
        assert not failures
        assert sorted(fetched) == sorted(tree)
        for path, local in fetched.items():
            assert open(local).read() == f"content-of:{path}"

    def test_one_bad_file_does_not_sink_the_rest(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(20)}
        bad = "A/P7.f3d"
        fetched, failures = gh.fetch_files(
            src, sorted(tree), str(tmp_path),
            FakeTransport(tree, fail_paths=[bad]), "c0ffee")
        assert [p for p, _m in failures] == [bad]
        assert len(fetched) == len(tree) - 1

    def test_progress_counts_every_file(self, src, tmp_path):
        seen = []
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(30)}
        gh.fetch_files(src, sorted(tree), str(tmp_path), FakeTransport(tree),
                       "c0ffee",
                       on_progress=lambda i, n, l: seen.append(i) or True)
        assert sorted(seen) == list(range(1, 31))

    def test_cancel_stops_the_pool(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(50)}

        def stop_after_five(i, n, label):
            return i < 5

        with pytest.raises(gh.Cancelled):
            gh.fetch_files(src, sorted(tree), str(tmp_path),
                           FakeTransport(tree), "c0ffee",
                           on_progress=stop_after_five, workers=2)

    def test_a_single_worker_is_still_correct(self, src, tmp_path):
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(5)}
        fetched, failures = gh.fetch_files(
            src, sorted(tree), str(tmp_path), FakeTransport(tree), "c0ffee", workers=1)
        assert not failures and len(fetched) == 5


class TestAddNeverLandsOnATakenName:
    """New files must not be uploaded onto a name that's already in the folder."""

    def test_a_hand_placed_file_is_not_doubled(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha1"}
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        theirs = panel.upload(folder, "/tmp/x", "Part")
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                           dry_run=False)

        assert plan.add == ["A/Part.f3d"], "it really is new to the manifest"
        assert rep.occupied == [("A/Part.f3d", "Part")]
        assert rep.added == []
        assert not rep.ok, "a refused file must not read as a clean run"
        assert panel.list_folder(folder) == [theirs]
        assert panel.duplicates() == {}, "this is the bug: two files named Part"

    def test_the_rest_of_the_batch_still_lands(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha1", "A/Other.f3d": "sha2", "B/Third.f3d": "sha3"}
        panel = FakeDataPanel()
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")
        mpath = str(tmp_path / "m.json")

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                            dry_run=False)

        assert sorted(rep.added) == ["A/Other.f3d", "B/Third.f3d"]
        assert rep.occupied == [("A/Part.f3d", "Part")]
        assert panel.duplicates() == {}

    def test_a_refused_file_is_not_recorded_so_it_can_land_later(self, src, tmp_path):
        """A refused file isn't recorded, so it uploads once the name is free."""
        tree = {"A/Part.f3d": "sha1"}
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        theirs = panel.upload(folder, "/tmp/x", "Part")
        mpath = str(tmp_path / "m.json")

        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False)
        assert Manifest.load(mpath).get("A/Part.f3d") is None

        panel.contents[folder] = []          # the obstruction is removed
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                            dry_run=False)

        assert rep.added == ["A/Part.f3d"]
        assert rep.occupied == []
        assert panel.total_files == 1
        assert theirs is not None

    def test_preview_says_so_before_the_user_commits(self, src, tmp_path):
        tree = {"A/Part.f3d": "sha1"}
        panel = FakeDataPanel()
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")

        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport(tree), F3D, dry_run=True)

        assert rep.occupied == [("A/Part.f3d", "Part")]
        assert panel.total_files == 1, "preview wrote nothing"

    def test_a_re_placement_is_not_refused_by_its_own_name(self, small_tree, src,
                                                           tmp_path):
        """Putting back a deleted file still works."""
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        for fid in panel.contents:
            panel.contents[fid] = []

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False)

        assert rep.occupied == []
        assert len(rep.replaced) == len(small_tree)
        assert panel.duplicates() == {}

    def test_an_unlistable_folder_does_not_block_a_first_sync(self, src, tmp_path):
        """If the folder can't be listed, the check is skipped, not failed."""
        tree = {"A/Part.f3d": "sha1"}

        class Blind(FakeDataPanel):
            def scan(self):
                raise RuntimeError("Data Panel unreachable")
            def list_folder(self, folder_id):
                raise RuntimeError("Data Panel unreachable")

        panel = Blind()
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport(tree), F3D, dry_run=False,
                            settle_wait=1.2)

        assert rep.occupied == [], "refused a file it could not prove was taken"
        assert panel.uploads == ["Part"], "the upload was still fired"


class TestFolderChangeIsVisible:
    """Changing the target folder uploads everything again, so it must be
    caught."""

    def test_no_change_is_no_impact(self):
        m = Manifest(source_id="s", repo="o/r")
        m.record("A/x.f3d", "sha", "urn:1")
        assert S.folder_change_impact(m, "VEX CAD Library", "VEX CAD Library") == 0

    def test_only_the_separators_differ(self):
        m = Manifest(source_id="s", repo="o/r")
        m.record("A/x.f3d", "sha", "urn:1")
        assert S.folder_change_impact(m, "/VEX/Lib/", "VEX/Lib") == 0

    def test_a_real_change_reports_every_recorded_file(self):
        m = Manifest(source_id="s", repo="o/r")
        for i in range(45):
            m.record(f"A/x{i}.f3d", "sha", f"urn:{i}")
        assert S.folder_change_impact(m, "VEX CAD Library", "VEX CAD") == 45

    def test_nothing_recorded_yet_is_harmless(self):
        assert S.folder_change_impact(None, "A", "B") == 0
        assert S.folder_change_impact(Manifest(source_id="s", repo="o/r"), "A", "B") == 0

    def test_the_damage_it_describes_is_real(self, src, tmp_path):
        """Same manifest, new folder: every file is uploaded again."""
        tree = {f"A/p{i}.f3d": f"s{i}" for i in range(9)}
        mpath = str(tmp_path / "m.json")
        old_folder, new_folder = FakeDataPanel(), FakeDataPanel()

        S.sync(src, mpath, old_folder, FakeTransport(tree), F3D, dry_run=False)
        _plan, rep = S.sync(src, mpath, new_folder, FakeTransport(tree), F3D,
                            dry_run=False)

        assert len(rep.replaced) == 9
        assert old_folder.total_files == 9 and new_folder.total_files == 9
        assert S.folder_change_impact(Manifest.load(mpath), "old", "new") == 9


class TestPreviewAndWriteAgreeOnTheCommit:
    """The upload must use the same commit as the preview."""

    class Moving(FakeTransport):
        def get_json(self, url):
            if "/commits/" in url:
                self.commit = self.commit + "x"      # the branch advanced
            return super().get_json(url)

    def test_the_plan_carries_the_commit_it_was_built_from(self, src, tmp_path):
        tr = FakeTransport({"A/x.f3d": "s"}, commit="deadbeef")
        plan, _ = S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(), tr,
                         F3D, dry_run=True)
        assert plan.commit == "deadbeef"

    def test_a_pinned_write_does_not_re_resolve(self, src, tmp_path):
        tr = self.Moving({"A/x.f3d": "s"}, commit="c0")
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")

        plan, _ = S.sync(src, mpath, panel, tr, F3D, dry_run=True)
        pinned = plan.commit
        S.sync(src, mpath, panel, tr, F3D, dry_run=False, commit=pinned)

        assert Manifest.load(mpath).synced_commit == pinned, (
            "uploaded against a commit the user never saw")

    def test_without_the_pin_the_two_runs_disagree(self, src, tmp_path):
        """Without pinning, the upload uses a newer commit than the preview."""
        tr = self.Moving({"A/x.f3d": "s"}, commit="c0")
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")

        plan, _ = S.sync(src, mpath, panel, tr, F3D, dry_run=True)
        S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert Manifest.load(mpath).synced_commit != plan.commit

    def test_a_pinned_run_makes_one_fewer_api_call(self, src, tmp_path):
        tr = FakeTransport({"A/x.f3d": "s"})
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(), tr, F3D,
               dry_run=True, commit="pinned")
        assert tr.json_calls == 1, "resolve_commit was called despite the pin"


class TestSubpathKeepsBlobSizes:
    """Filtering by subpath must keep the file sizes."""

    payload = {"truncated": False, "tree": [
        {"path": "cad/a.f3d", "type": "blob", "sha": "s1", "size": 1000},
        {"path": "cad/b.f3d", "type": "blob", "sha": "s2", "size": 2000},
        {"path": "doc/c.md", "type": "blob", "sha": "s3", "size": 10},
    ]}

    class T:
        def get_json(self, url):
            return TestSubpathKeepsBlobSizes.payload, {}
        def get_bytes(self, url, on_chunk=None):
            return b""

    def test_sizes_survive_the_subpath_filter(self):
        tree = gh.fetch_tree(gh.Source("o/r", "main", "cad"), self.T())
        assert isinstance(tree, gh.Tree)
        assert tree.total_bytes == 3000, "the filter dropped .sizes"
        assert tree.bytes_for(["cad/a.f3d"]) == 1000

    def test_the_filter_still_filters(self):
        tree = gh.fetch_tree(gh.Source("o/r", "main", "cad"), self.T())
        assert sorted(tree) == ["cad/a.f3d", "cad/b.f3d"]
        assert "doc/c.md" not in tree.sizes

    def test_no_subpath_is_unaffected(self):
        tree = gh.fetch_tree(gh.Source("o/r", "main"), self.T())
        assert tree.total_bytes == 3010


class TestProgressNeverOvershoots:
    """The progress bar maximum must match what's actually sent."""

    def test_the_addin_sizes_the_bar_by_everything_it_will_send(self):
        """The bar counts new and missing files."""
        import ast
        import re
        text = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Detent.py"), encoding="utf-8").read()
        ast.parse(text)                      # still valid Python
        shows = re.findall(r"progress\.show\([^)]*\)", text)
        upload_bar = [s for s in shows if "Starting" in s]
        assert len(upload_bar) == 1, shows
        assert "plan.to_place" in upload_bar[0], upload_bar[0]
        assert "plan.add" not in upload_bar[0], upload_bar[0]

    def test_the_counter_counts_sends_not_positions(self, src, tmp_path):
        """Skipped files don't advance the counter."""
        tree = {"A/Part.f3d": "s1", "A/Other.f3d": "s2", "A/Third.f3d": "s3"}
        panel = FakeDataPanel()
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")

        seen = []

        def on_progress(i, total, label):
            if label.startswith("Sending"):
                seen.append((i, total, label))
            return True

        S.sync(src, str(tmp_path / "m.json"), panel, FakeTransport(tree), F3D,
               dry_run=False, on_progress=on_progress)

        assert seen, "no upload progress was reported"
        for i, total, label in seen:
            assert 0 <= i < total, f"{label} reported {i} of {total}"
        assert [t for _i, t, _l in seen] == [2, 2], (
            "the total must exclude the file that was refused")
        assert [lbl.split()[1] for _i, _t, lbl in seen] == ["1/2", "2/2"]


class TestTheConfirmationCountsWhatWillBeSent:
    """The upload prompt counts new and missing files."""

    def _plan(self, add=0, missing=0):
        p = PL.Plan()
        p.add = [f"A/new{i}.f3d" for i in range(add)]
        p.missing = [f"A/gone{i}.f3d" for i in range(missing)]
        return p

    def test_a_pure_drift_recovery_still_asks(self):
        assert S.upload_count(self._plan(add=0, missing=45), S.Report()) == 45

    def test_a_mixed_run_counts_both(self):
        assert S.upload_count(self._plan(add=5, missing=45), S.Report()) == 50

    def test_plain_adds_are_unchanged(self):
        assert S.upload_count(self._plan(add=33), S.Report()) == 33

    def test_nothing_to_do_is_zero(self):
        assert S.upload_count(self._plan(), S.Report()) == 0

    def test_refused_names_are_not_counted(self):
        rep = S.Report(occupied=[("A/new0.f3d", "new0"), ("A/new1.f3d", "new1")])
        assert S.upload_count(self._plan(add=5), rep) == 3

    def test_everything_refused_means_nothing_is_sent(self):
        rep = S.Report(occupied=[(f"A/new{i}.f3d", f"new{i}") for i in range(5)])
        assert S.upload_count(self._plan(add=5), rep) == 0

    def test_the_addin_asks_using_that_count(self):
        """The add-in must use upload_count, not plan.add."""
        import re
        text = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Detent.py"), encoding="utf-8").read()
        body = text[text.index("def _do_sync"):text.index("def _do_adopt")]
        assert "S.upload_count(plan, preview)" in body
        assert not re.search(r"if plan\.add:", body), \
            "the confirmation is gating on plan.add again"

    def test_changed_files_are_named_not_just_counted(self, src, tmp_path):
        """The preview lists changed files by name."""
        import re
        text = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Detent.py"), encoding="utf-8").read()
        body = text[text.index("def _plan_text"):text.index("def _short")]
        assert "plan.change[:8]" in body, "changed paths are not listed"


class TestFolderCheck:
    """Preview reports the folder's file count, duplicate names and files
    Detent doesn't know about, from the scan it already does."""

    def test_a_clean_sync_checks_clean(self, small_tree, src, tmp_path):
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=True)
        assert rep.check.files == len(small_tree)
        assert rep.check.duplicates == {}
        assert rep.check.untracked == 0

    def test_duplicates_are_named_with_their_folder(self, src, tmp_path):
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A", "B"))
        panel.upload(folder, "/tmp/x", "Part")
        panel.upload(folder, "/tmp/x", "Part")
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")  # different folder: fine
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport({}), F3D, dry_run=True)
        assert rep.check.duplicates == {"A/B/Part": 2}
        assert rep.check.files == 3 and rep.check.untracked == 3

    def test_hand_added_files_are_untracked(self, small_tree, src, tmp_path):
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        panel.upload(panel.ensure_folder(("Extra",)), "/tmp/x", "Mine")
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=True)
        assert rep.check.untracked == 1
        assert rep.check.files == len(small_tree) + 1

    def test_adopted_files_count_as_tracked(self, small_tree, src, tmp_path):
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        for path in small_tree:
            pp = S.P.map_path(path)
            panel.upload(panel.ensure_folder(pp.folders), "/tmp/x", pp.name)
        S.adopt_existing(src, mpath, panel, FakeTransport(small_tree), "v1",
                         F3D, dry_run=False)
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=True)
        assert rep.check.untracked == 0 and rep.check.files == len(small_tree)

    def test_no_scan_means_no_check(self, small_tree, src, tmp_path):
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
                            FakeTransport(small_tree), F3D, dry_run=True,
                            verify_placed=False)
        assert rep.check is None and S.check_lines(rep.check) == []

    def test_a_failed_scan_means_no_check(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()

        def boom():
            raise RuntimeError("Data Panel unreachable")
        panel.scan = boom
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport(small_tree), F3D, dry_run=True)
        assert rep.check is None

    def test_the_line_shown_to_the_user(self):
        d = S.Drift(files=48, duplicates={}, untracked=0, present=set(), names={})
        assert S.check_lines(d) == [
            "Folder check: 48 file(s), 0 duplicate name(s), 0 not tracked by Detent"]
        d = S.Drift(files=9, untracked=2, present=set(), names={},
                    duplicates={f"A/P{i}": 2 for i in range(7)})
        lines = S.check_lines(d)
        assert lines[0].startswith("Folder check: 9 file(s), 7 duplicate name(s)")
        assert lines[1] == "    A/P0  (x2)"
        assert lines[-1] == "    ... and 2 more"

    def test_the_addin_shows_it(self):
        text = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Detent.py"), encoding="utf-8").read()
        body = text[text.index("def _do_sync"):text.index("def _do_adopt")]
        assert "S.check_lines(preview.check)" in body


class TestDownloadSize:
    """The preview and the prompt show how much will be downloaded."""

    class Sized(FakeTransport):
        def get_json(self, url):
            payload, h = super().get_json(url)
            for e in payload.get("tree", []):
                e["size"] = 1000
            return payload, h

    def test_preview_knows_the_size(self, small_tree, src, tmp_path):
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
                            self.Sized(small_tree), F3D, dry_run=True)
        assert rep.download_bytes == 1000 * len(small_tree)

    def test_refused_files_are_not_counted(self, src, tmp_path):
        tree = {"A/Part.f3d": "s1", "A/Other.f3d": "s2"}
        panel = FakeDataPanel()
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            self.Sized(tree), F3D, dry_run=True)
        assert rep.download_bytes == 1000

    def test_the_addin_uses_it(self):
        text = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Detent.py"), encoding="utf-8").read()
        assert "gh.estimate(n, preview.download_bytes)" in text


class TestOnlyRealFusionIdsAreRecorded:
    """While a file uploads, Fusion reports a temporary local path as its id.
    Recording that made Detent think its own files had been replaced once the
    real ID appeared (found in testing, 23 Sep 2026)."""

    TEMP = "/Users/tester/Library/Application Support/Autodesk/Q/abc.f3d"

    def test_a_sync_never_records_a_temporary_path(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(temp_ids=2)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False,
               settle_wait=30.0)
        m = Manifest.load(mpath)
        bad = {p: e.lineage for p, e in m.files.items()
               if not str(e.lineage).startswith("urn:")}
        assert bad == {}, f"recorded temporary ids: {bad}"
        assert all(e.state == PLACED for e in m.files.values())

    def test_the_reported_case_is_clean_afterwards(self, small_tree, src, tmp_path):
        """Sync, then Preview: nothing missing, nothing taken, nothing untracked.
        The real IDs appear between the sync and the Preview, as they did."""
        panel = FakeDataPanel(temp_ids=1)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False,
               settle_wait=30.0)
        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True)
        assert plan.is_empty, plan
        assert rep.check.untracked == 0 and rep.check.duplicates == {}

    def test_still_uploading_at_the_deadline_stays_unconfirmed(self, small_tree,
                                                               src, tmp_path):
        """If the real ID hasn't appeared in time, keep the file as unconfirmed.
        Never record the temporary path, and never drop the entry."""
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, settle_wait=1.2)
        m = Manifest.load(mpath)
        assert len(m.files) == len(small_tree), "an entry was dropped"
        assert all(e.state == INFLIGHT for e in m.files.values())
        assert all(e.lineage is None for e in m.files.values())
        assert rep.failures, "the unconfirmed files must be reported"

    def test_the_next_sync_confirms_them_without_uploading(self, small_tree, src,
                                                           tmp_path):
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False,
               settle_wait=1.2)
        uploads = len(panel.uploads)
        panel.temp_ids = 0                         # the uploads have finished
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, settle_wait=1.2)
        assert len(panel.uploads) == uploads, "uploaded again"
        assert panel.duplicates() == {}
        m = Manifest.load(mpath)
        assert all(e.state == PLACED and e.lineage.startswith("urn:")
                   for e in m.files.values())

    def test_a_manifest_with_temporary_paths_heals(self, small_tree, src, tmp_path):
        """The state v0.4.1 and earlier leave behind: placed entries whose
        'lineage' is a local path. Preview must not call them taken, and Sync
        must fix them without uploading."""
        panel, mpath = FakeDataPanel(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        m = Manifest.load(mpath)
        broken = sorted(m.files)[:3]
        for p in broken:
            m.files[p].lineage = self.TEMP.replace("abc", p.rsplit("/", 1)[1][:4])
        m.save(mpath)
        uploads = len(panel.uploads)

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True)
        assert plan.conflict == [] and plan.missing == []
        assert sorted(plan.inflight) == broken

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False)
        assert len(panel.uploads) == uploads, "uploaded again"
        assert sorted(p for p, _why in rep.reconciled) == broken
        m = Manifest.load(mpath)
        assert all(e.state == PLACED and e.lineage.startswith("urn:")
                   for e in m.files.values())
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=True)
        assert rep.check.untracked == 0

    def test_confirming_waits_for_the_real_id(self, src, tmp_path):
        """reconcile_inflight must not record a file that's still uploading,
        and must not drop it either."""
        tree = {"A/Part.f3d": "s1"}
        panel = FakeDataPanel(temp_ids=10_000)
        panel.upload(panel.ensure_folder(("A",)), "/tmp/x", "Part")
        m = Manifest(source_id="s", repo="o/r")
        m.mark_inflight("A/Part.f3d", placed_name="Part")
        rep = S.Report()
        S.reconcile_inflight(m, panel, rep, tree)
        e = m.get("A/Part.f3d")
        assert e is not None and e.state == INFLIGHT and e.lineage is None

    def test_adopt_skips_files_still_uploading(self, src, tmp_path):
        tree = {"A/Part.f3d": "s1", "A/Other.f3d": "s2"}
        panel = FakeDataPanel()
        folder = panel.ensure_folder(("A",))
        panel.upload(folder, "/tmp/x", "Part")
        panel.upload(folder, "/tmp/x", "Other")
        panel.temp_ids = 10_000
        for pf in panel.contents[folder][1:]:
            panel._seen[pf.lineage] = 0             # "Other" is still uploading
        panel._seen[panel.contents[folder][0].lineage] = 10_000
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"), panel,
                                    FakeTransport(tree), "v1", F3D)
        assert sorted(m.files) == ["A/Part.f3d"]
        assert all(e.lineage.startswith("urn:") for e in m.files.values())
        assert stats["matched"] == 1


class TestUploadHandleTemporaryIds:
    """If Fusion's upload handle ever reports a temporary path, don't record it."""

    TEMP = "/Users/tester/Library/Application Support/Autodesk/Q/part.f3d"

    def test_the_handle_alone(self, src, tmp_path):
        tmp = self.TEMP

        class HandleOnly(FakeDataPanel):
            def begin_upload(self, folder_id, local_path, name):
                return {"name": name}            # nothing shows in the folder
            def poll_upload(self, handle):
                from detent_core.datapanel import PlacedFile
                return PlacedFile(name=handle["name"], lineage=tmp)

        panel, mpath = HandleOnly(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport({"A/Part.f3d": "s1"}), F3D,
               dry_run=False, settle_wait=1.2)
        e = Manifest.load(mpath).get("A/Part.f3d")
        assert e.state == INFLIGHT and e.lineage is None, e

    def test_the_handle_when_two_files_share_a_name(self, src, tmp_path):
        tmp = self.TEMP

        class TwoLand(FakeDataPanel):
            def begin_upload(self, folder_id, local_path, name):
                FakeDataPanel.upload(self, folder_id, local_path, name)
                FakeDataPanel.upload(self, folder_id, local_path, name)
                return {"name": name}
            def poll_upload(self, handle):
                from detent_core.datapanel import PlacedFile
                return PlacedFile(name=handle["name"], lineage=tmp)

        panel, mpath = TwoLand(), str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport({"A/Part.f3d": "s1"}), F3D,
               dry_run=False, settle_wait=1.2)
        e = Manifest.load(mpath).get("A/Part.f3d")
        assert e.state == INFLIGHT and e.lineage is None, e
