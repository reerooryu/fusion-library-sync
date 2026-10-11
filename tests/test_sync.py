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
        if "/releases?" in url or "/tags?" in url:
            return [], {}
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

    def test_the_estimate_is_the_time_until_detent_is_done(self):
        """It said 66 seconds for 33 files (2 s each) when the work took 19.
        Measured 6-7 Oct 2026: a 33-file, 251 MB update into 6 folders took
        19 s; a 1,165-file, 2.0 GB install into 115 folders took 5 min 20 s."""
        update = gh.estimate_seconds(33, 251_000_000, 6)
        install = gh.estimate_seconds(1165, 2_044_000_000, 115)
        assert 19 <= update <= 45, update
        assert 320 <= install <= 600, install
        assert "about 36 sec" in gh.estimate(33, 251_000_000, 6)

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

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False,
                           settle_wait=5.0)

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
        assert rep.pending == ["A/Part.f3d"], "it must be reported as still uploading"
        assert rep.failures == [], "still uploading is not a failure"

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
        assert "poll_upload" in body, "the future is the fallback"
        waiting = body[body.index("while pending"):]
        assert "list_folder" in waiting, "the fast path is the folder scan"
        assert waiting.index("list_folder") < waiting.index("ask_handles()")

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


class TestDetentDoesNotWaitForFusion:
    """Sending 33 parts took 19 seconds; waiting for Fusion to confirm them
    took two more minutes and confirmed none (7 Oct 2026). Fusion uploads in
    the background, so Detent hands over and finishes."""

    def test_by_default_it_neither_waits_nor_reads_folders(self, small_tree,
                                                           src, tmp_path):
        waits = []

        class Watching(FakeDataPanel):
            def wait(self, seconds):
                waits.append(seconds)

        panel = Watching(deferred=True)               # nothing confirmed yet
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=True,
               verify_placed=False)
        panel.listings = []

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, verify_placed=False)

        assert S.CONFIRM_WAIT == 0
        assert waits == [], "it waited"
        assert panel.listings == [], "it read folders to confirm uploads"
        assert sorted(rep.pending) == sorted(small_tree)
        assert rep.failures == [] and rep.ok
        assert rep.timings["confirm"] < 1.0

    def test_uploads_already_finished_are_confirmed_on_the_spot(
            self, small_tree, src, tmp_path):
        """Asking the upload handle once is free, so do that much."""
        panel = FakeDataPanel()                       # finishes at once
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport(small_tree), F3D, dry_run=False,
                            verify_placed=False)
        assert len(rep.added) == len(small_tree) and rep.pending == []

    def test_the_next_sync_confirms_them_and_uploads_nothing(self, small_tree,
                                                             src, tmp_path):
        panel = FakeDataPanel(deferred=True)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False,
               verify_placed=False)
        sent = list(panel.uploads)

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False, verify_placed=False)

        assert len(rep.reconciled) == len(small_tree)
        assert panel.uploads == sent, "something was uploaded twice"
        assert Manifest.load(mpath).in_state(INFLIGHT) == {}
        assert panel.duplicates() == {}

    def test_the_done_box_counts_everything_handed_over_as_added(
            self, small_tree, src, tmp_path):
        """'added 0' after sending 33 parts reads as if nothing happened."""
        panel = FakeDataPanel(deferred=True)
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), panel,
                            FakeTransport(small_tree), F3D, dry_run=False,
                            verify_placed=False)
        n = len(small_tree)
        lines = rep.lines()
        assert lines[0].split() == ["added", str(n)]
        assert any(l.startswith("still uploading") and str(n) in l for l in lines)

    def test_re_placed_counts_parts_still_uploading(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)
        for fid in panel.contents:
            panel.contents[fid] = []                  # all deleted by hand
        panel.deferred = True

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, verify_placed=True)

        assert len(rep.replaced) == len(small_tree)


class TestVerifyConfirmsEarlierUploadsFirst:
    """Verify Integrity reads every folder anyway. Without this, parts Fusion
    finished since the last sync show up as files Detent doesn't know."""

    def waiting(self, small_tree, src, tmp_path):
        panel = FakeDataPanel(deferred=True)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False,
               verify_placed=False)
        assert len(Manifest.load(mpath).in_state(INFLIGHT)) == len(small_tree)
        return panel, mpath

    def test_with_recheck_the_library_verifies_clean(self, small_tree, src, tmp_path):
        panel, mpath = self.waiting(small_tree, src, tmp_path)

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True, verify_placed=True, recheck=True)

        assert plan.is_empty
        assert rep.check.untracked == 0
        assert Manifest.load(mpath).in_state(INFLIGHT) == {}

    def test_a_plain_preview_still_changes_nothing(self, small_tree, src, tmp_path):
        panel, mpath = self.waiting(small_tree, src, tmp_path)
        before = open(mpath, encoding="utf-8").read()

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True, verify_placed=False)

        assert len(plan.inflight) == len(small_tree)
        assert open(mpath, encoding="utf-8").read() == before

    def test_the_addin_asks_for_it_only_when_verifying(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Detent.py"), encoding="utf-8").read()
        assert "verify_placed=full, recheck=full," in source


class TestConfirmWaitIsBounded:
    """Detent hands uploads to Fusion and waits a short, fixed time for them
    to be confirmed. Waiting one second per file made a full library take 20
    more minutes (5 Oct 2026); Fusion uploads in the background anyway."""

    def spy_on_settle(self, monkeypatch):
        seen = {}
        real_settle = S.settle

        def spy(manifest, panel, selected, report, mpath, handles,
                on_progress, max_wait, **kw):
            seen["wait"], seen["n"] = max_wait, len(handles)
            return real_settle(manifest, panel, selected, report, mpath,
                               handles, on_progress, max_wait, **kw)

        monkeypatch.setattr(S, "settle", spy)
        return seen

    def test_the_wait_does_not_grow_with_the_batch(self, src, tmp_path, monkeypatch):
        seen = self.spy_on_settle(monkeypatch)
        tree = {f"A/P{i}.f3d": f"s{i}" for i in range(400)}
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
               FakeTransport(tree), F3D, dry_run=False)
        assert seen["n"] == 400
        assert seen["wait"] == S.CONFIRM_WAIT <= 180

    def test_an_explicit_wait_still_wins(self, small_tree, src, tmp_path,
                                        monkeypatch):
        seen = self.spy_on_settle(monkeypatch)
        S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
               FakeTransport(small_tree), F3D, dry_run=False, settle_wait=7.0)
        assert seen["wait"] == 7.0

    def test_what_fusion_hasnt_confirmed_is_pending_not_failed(
            self, small_tree, src, tmp_path):
        panel = FakeDataPanel(temp_ids=10_000)        # never finishes uploading
        mpath = str(tmp_path / "m.json")
        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, settle_wait=1.2)

        assert sorted(rep.pending) == sorted(small_tree)
        assert rep.failures == [] and rep.ok
        assert any(line.startswith("still uploading") for line in rep.lines())
        m = Manifest.load(mpath)
        assert all(e.state == INFLIGHT for e in m.files.values())
        assert m.synced_at, "the sync ran; the uploads are just not confirmed yet"

    def test_cancel_while_confirming_only_stops_the_wait(
            self, small_tree, src, tmp_path):
        """The files are already with Fusion. Cancel must not look like a
        cancelled sync, and must not lose track of them."""
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")

        def stop_when_confirming(i, n, label):
            return not label.startswith("Confirming")

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                            dry_run=False, settle_wait=60.0,
                            on_progress=stop_when_confirming)

        assert sorted(rep.pending) == sorted(small_tree)
        assert len(Manifest.load(mpath).in_state(INFLIGHT)) == len(small_tree)

    def test_cancel_between_passes_also_only_stops_the_wait(self, src, tmp_path):
        tree = {"Bin/One.f3d": "s1", "Bin/Two.f3d": "s2"}
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")
        asked = []

        def stop_on_the_second_ask(i, n, label):
            if label.startswith("Confirming"):
                asked.append(label)
                return len(asked) < 2
            return True

        _plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                            dry_run=False, settle_wait=60.0,
                            on_progress=stop_on_the_second_ask)

        assert sorted(rep.pending) == sorted(tree)
        assert len(asked) == 2, "one folder: asked before it, then after the pass"

    def test_the_wait_keeps_fusion_responsive(self, small_tree, src, tmp_path):
        """A plain sleep freezes Fusion's window and its uploads."""
        waits = []

        class Watching(FakeDataPanel):
            def wait(self, seconds):
                waits.append(seconds)

        panel = Watching(deferred=True)               # needs a few passes
        S.sync(src, str(tmp_path / "m.json"), panel, FakeTransport(small_tree),
               F3D, dry_run=False, settle_wait=5.0)
        import inspect
        assert "time.sleep" not in inspect.getsource(S.settle)


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
        assert ("gh.estimate(n, preview.download_bytes, "
                "preview.target_folders)") in text


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
        assert sorted(rep.pending) == sorted(small_tree), \
            "the unconfirmed files must be reported"

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


class TestFusionFolderErrors:
    """Fusion sometimes refuses a Data Panel call. Seen 5 Oct 2026: a full
    sync downloaded 1,165 files, then dataFolders.count raised
    InternalValidationError on the first upload and the whole run died."""

    def folders_of(self, tree):
        from detent_core import paths as P
        return {P.map_path(p).folders for p in tree}

    def test_a_folder_fusion_wont_open_stops_before_the_download(
            self, small_tree, src, tmp_path):
        bad = sorted(self.folders_of(small_tree))[0]
        panel = FakeDataPanel(refuse_folders=[bad])
        tr = FakeTransport(small_tree)
        mpath = str(tmp_path / "m.json")

        plan, rep = S.sync(src, mpath, panel, tr, F3D, dry_run=False)

        assert tr.byte_calls == 0, "downloaded before checking the folders"
        assert panel.uploads == []
        assert rep.stopped_early and not rep.ok
        assert len(rep.failures) == 1
        assert "Nothing was downloaded" in rep.failures[0][1]
        m = Manifest.load(mpath)
        assert m.files == {}, "nothing was sent, so nothing may be recorded"
        assert m.synced_at is None, "a run that stopped early isn't a sync"

    def test_every_folder_is_opened_before_the_first_download(
            self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        seen = {}

        class Watching(FakeTransport):
            def get_bytes(self, url, on_chunk=None):
                seen.setdefault("folders", set(panel.folder_calls))
                return super().get_bytes(url, on_chunk)

        S.sync(src, str(tmp_path / "m.json"), panel, Watching(small_tree), F3D,
               dry_run=False)

        assert seen["folders"] == self.folders_of(small_tree)

    def test_preview_opens_no_folders(self, small_tree, src, tmp_path):
        """Opening a folder creates it. Preview must change nothing."""
        panel = FakeDataPanel()
        S.sync(src, str(tmp_path / "m.json"), panel, FakeTransport(small_tree),
               F3D, dry_run=True)
        assert panel.folder_calls == []

    def test_the_next_sync_places_everything_once(self, small_tree, src, tmp_path):
        bad = sorted(self.folders_of(small_tree))[0]
        panel = FakeDataPanel(refuse_folders=[bad])
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=False)

        panel.refuse_folders.clear()                  # Fusion recovers
        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False)

        assert len(rep.added) == len(small_tree) and rep.ok
        assert panel.duplicates() == {}

    def test_a_folder_error_while_sending_skips_only_those_files(
            self, small_tree, src, tmp_path):
        from detent_core import paths as P
        bad = sorted(self.folders_of(small_tree))[0]
        in_bad = [p for p in small_tree if P.map_path(p).folders == bad]

        class FailsLater(FakeDataPanel):
            """Opens every folder once, then refuses one."""
            def ensure_folder(self, folders):
                if tuple(folders) == bad and tuple(folders) in self.folder_calls:
                    self.folder_calls.append(tuple(folders))
                    raise RuntimeError("InternalValidationError")
                return super().ensure_folder(folders)

        panel = FailsLater()
        mpath = str(tmp_path / "m.json")
        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False)

        assert sorted(p for p, _m in rep.failures) == sorted(in_bad)
        assert len(rep.added) == len(small_tree) - len(in_bad)
        m = Manifest.load(mpath)
        assert all(m.get(p) is None for p in in_bad), \
            "a file that was never sent must not be recorded"

        # Fusion recovers: only the skipped files go up, and nothing twice.
        fixed = FakeDataPanel()
        fixed.folders, fixed.contents = panel.folders, panel.contents
        fixed._ids = panel._ids
        plan, rep = S.sync(src, mpath, fixed, FakeTransport(small_tree), F3D,
                           dry_run=False)
        assert sorted(rep.added) == sorted(in_bad)
        assert fixed.duplicates() == {}

    def test_an_unconfirmed_file_fusion_cant_check_is_not_uploaded_again(
            self, small_tree, src, tmp_path):
        mpath = str(tmp_path / "m.json")
        victim = sorted(small_tree)[0]
        m = Manifest(source_id="x", repo=src.repo, ref=src.ref)
        m.mark_inflight(victim)
        m.save(mpath)

        class CantLook(FakeDataPanel):
            """Fusion refuses the first request, which is the recheck."""
            asked = 0

            def peek_folder(self, folders):
                self.asked += 1
                if self.asked == 1:
                    raise RuntimeError("InternalValidationError")
                return super().peek_folder(folders)

        panel = CantLook()
        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False)

        from detent_core import paths as P
        assert P.map_path(victim).name not in panel.uploads
        assert victim in Manifest.load(mpath).in_state(INFLIGHT)
        assert any(p == victim and "next sync" in msg for p, msg in rep.failures)
        assert len(rep.added) == len(small_tree) - 1, "the rest still sync"


class TestFusionPanel:
    """FusionDataPanel against stand-ins for Fusion's folder objects. Every
    request to Fusion is counted, because requests are what make it slow."""

    ERR = "2 : InternalValidationError : status.isOk() && folders"

    class Folder:
        def __init__(self, name, fail=0):
            self.name, self.children, self.files = name, [], []
            self.fail = fail                  # how many requests fail first
            self.folder_requests = 0          # times .dataFolders was fetched
            self.file_requests = 0            # times .dataFiles was fetched
            self.adds = 0

        def _check(self):
            if self.fail > 0:
                self.fail -= 1
                raise RuntimeError(TestFusionPanel.ERR)

        @property
        def dataFolders(self):
            self.folder_requests += 1
            outer = self

            class Folders:
                @property
                def count(self):
                    outer._check()
                    return len(outer.children)

                def item(self, i):
                    return outer.children[i]

                def add(self, name):
                    outer._check()
                    outer.adds += 1
                    child = TestFusionPanel.Folder(name)
                    outer.children.append(child)
                    return child

            return Folders()

        @property
        def dataFiles(self):
            self.file_requests += 1
            outer = self

            class Files:
                @property
                def count(self):
                    outer._check()
                    return len(outer.files)

                def item(self, i):
                    return outer.files[i]

            return Files()

        def child(self, name):
            return next(c for c in self.children if c.name == name)

    class File:
        def __init__(self, name, n):
            self.name, self.id, self.versionNumber = name, f"urn:x:{n}", 1

    def panel(self, top, root_path=("Lib",)):
        from detent_core.datapanel import FusionDataPanel
        p = object.__new__(FusionDataPanel)           # skip the adsk import
        p.waits = []
        p.wait = p.waits.append                       # don't really wait
        p._project = type("Project", (), {"rootFolder": top})()
        p._root_path = tuple(root_path)
        p._forget()
        return p

    # -- speed -------------------------------------------------------------
    def test_listing_a_folder_fetches_its_files_once(self):
        top = self.Folder("root")
        p = self.panel(top)
        folder = p.ensure_folder(("Hardware",))
        folder.files = [self.File(f"Part {i}", i) for i in range(40)]

        assert len(p.list_folder(folder)) == 40
        assert folder.file_requests == 1, "fetched dataFiles once per file"

    def test_a_folders_subfolders_are_read_once_however_many_are_opened(self):
        top = self.Folder("root")
        lib = self.Folder("Lib")
        top.children.append(lib)
        aluminum = self.Folder("Aluminum")
        lib.children.append(aluminum)
        aluminum.children = [self.Folder(f"C-Channel {i}") for i in range(60)]
        p = self.panel(top)

        for i in range(60):
            p.ensure_folder(("Aluminum", f"C-Channel {i}"))

        assert top.folder_requests == 1
        assert lib.folder_requests == 1
        assert aluminum.folder_requests == 1, "re-read 60 siblings 60 times"

    def test_a_folder_just_made_is_never_listed(self):
        """It's empty, and listing a brand-new folder is what Fusion refused."""
        top = self.Folder("root")
        p = self.panel(top)

        p.ensure_folder(("Electronics", "Sensors", "3-Wire"))
        p.ensure_folder(("Electronics", "Sensors", "Smart"))

        lib = top.child("Lib")
        electronics = lib.child("Electronics")
        sensors = electronics.child("Sensors")
        # Each was only asked to add a subfolder, never to list its own.
        assert [c.name for c in sensors.children] == ["3-Wire", "Smart"]
        assert p._children[("Lib", "Electronics")] == {"Sensors": sensors}
        for made in (lib, electronics, sensors):
            assert made.folder_requests == made.adds, \
                f"{made.name} was listed after being created"

    def test_peek_creates_nothing(self):
        top = self.Folder("root")
        p = self.panel(top)

        assert p.peek_folder(("Hardware", "Screws")) is None
        assert p.peek_folder(()) is None
        assert top.children == [], "peek created the library folder"

    def test_peek_finds_what_exists(self):
        top = self.Folder("root")
        lib = self.Folder("Lib")
        top.children.append(lib)
        lib.children.append(self.Folder("Hardware"))
        p = self.panel(top)

        assert p.peek_folder(("Hardware",)) is lib.child("Hardware")
        assert p.peek_folder(("Motion",)) is None

    def test_opening_the_panel_creates_nothing(self):
        """Preview opens the panel. It used to create the library folder."""
        from detent_core.datapanel import FusionDataPanel
        import inspect
        assert "ensure" not in inspect.getsource(FusionDataPanel.__init__)
        assert ".add(" not in inspect.getsource(FusionDataPanel.__init__)

    def test_scan_reads_each_folder_once(self):
        top = self.Folder("root")
        p = self.panel(top)
        a = p.ensure_folder(("A",))
        b = p.ensure_folder(("A", "B"))
        a.files = [self.File("One", 1)]
        b.files = [self.File("Two", 2), self.File("Three", 3)]
        p._forget()                                   # a fresh run
        seen = []

        found = p.scan(seen.append)

        assert {k: [f.name for f in v] for k, v in found.items()} == \
            {("A",): ["One"], ("A", "B"): ["Two", "Three"]}
        assert a.file_requests == 1 and b.file_requests == 1
        assert a.folder_requests == 2, "one add at setup, one listing in scan"
        assert seen == [1, 2, 3], "library root, A, A/B"

    def test_scan_of_a_library_that_isnt_there_is_empty(self):
        top = self.Folder("root")
        p = self.panel(top)
        assert p.scan() == {}
        assert top.children == []

    # -- errors ------------------------------------------------------------
    def test_a_request_that_fails_twice_then_works(self):
        top = self.Folder("root", fail=2)
        p = self.panel(top)

        folder = p.ensure_folder(("Hardware",))

        assert folder.name == "Hardware"
        assert len(p.waits) == 2, "should have waited between tries"

    def test_a_failed_create_is_not_repeated_if_it_worked(self):
        """Fusion can report an error and make the folder anyway."""
        top = self.Folder("root")
        lib = self.Folder("Lib")
        top.children.append(lib)
        p = self.panel(top)
        p.peek_folder(())                             # lib is known now

        real = self.Folder.dataFolders
        calls = {"n": 0}
        made = self.Folder("Hardware")

        class MakesItThenErrors:
            @property
            def count(self):
                return len(lib.children)

            def item(self, i):
                return lib.children[i]

            def add(self, name):
                calls["n"] += 1
                lib.children.append(made)
                raise RuntimeError(TestFusionPanel.ERR)

        lib_cls = type("Lib", (self.Folder,), {
            "dataFolders": property(lambda self_: MakesItThenErrors())})
        lib.__class__ = lib_cls

        assert p.ensure_folder(("Hardware",)) is made
        assert calls["n"] == 1, "created the folder a second time"
        assert [c.name for c in lib.children] == ["Hardware"]

    def test_stale_folder_objects_are_replaced_from_the_project(self):
        top = self.Folder("root")
        fresh = self.Folder("Lib")
        top.children.append(fresh)
        p = self.panel(top)
        stale = self.Folder("Lib", fail=10**6)        # never answers again
        p._folders[()] = top
        p._folders[("Lib",)] = stale

        folder = p.ensure_folder(("Hardware",))

        assert folder in fresh.children, "should have used the live folder"

    def test_when_fusion_never_answers_the_error_names_the_folder(self):
        from detent_core.datapanel import PanelError
        top = self.Folder("root", fail=10**6)
        p = self.panel(top)

        with pytest.raises(PanelError) as err:
            p.ensure_folder(("Hardware", "Screws"))

        assert "Lib/Hardware/Screws" in str(err.value)
        assert "InternalValidationError" in str(err.value)

    def test_peek_reports_a_refusal_the_same_way(self):
        from detent_core.datapanel import PanelError
        top = self.Folder("root", fail=10**6)
        with pytest.raises(PanelError):
            self.panel(top).peek_folder(("Hardware",))

    def test_listing_a_folder_retries(self):
        top = self.Folder("root")
        p = self.panel(top)
        folder = p.ensure_folder(("Hardware",))
        folder.fail = 1
        assert p.list_folder(folder) == []
        assert len(p.waits) == 1

    def test_an_upload_fusion_refuses_is_a_failed_upload_not_a_crash(self):
        p = self.panel(self.Folder("root"))

        class Refuses:
            def uploadFile(self, path):
                raise RuntimeError(TestFusionPanel.ERR)

        with pytest.raises(UploadFailed):
            p.begin_upload(Refuses(), "/tmp/x.f3d", "Part")


class TestRealPanelWithTheSyncEngine:
    """The real FusionDataPanel, driven by the sync code, against stand-in
    Fusion objects. Counts requests for a whole run."""

    class Folder(TestFusionPanel.Folder):
        uploads = 0

        def uploadFile(self, path):
            cls = TestRealPanelWithTheSyncEngine.Folder
            cls.uploads += 1
            name = os.path.basename(path)[:-len(".f3d")]
            df = TestFusionPanel.File(name, f"up{cls.uploads}")
            self.files.append(df)
            return type("Future", (), {"uploadState": 1, "dataFile": df})()

    def fresh_project(self):
        from detent_core.datapanel import FusionDataPanel
        self.Folder.uploads = 0
        # Folders the stub makes must be able to upload too.
        TestFusionPanel.Folder.uploadFile = self.Folder.uploadFile
        self.top = self.Folder("root")

        def panel():
            p = object.__new__(FusionDataPanel)
            p.wait = lambda seconds: None
            p._project = type("Project", (), {"rootFolder": self.top})()
            p._root_path = ("Lib",)
            p._forget()
            return p
        return panel

    def teardown_method(self):
        if hasattr(TestFusionPanel.Folder, "uploadFile"):
            del TestFusionPanel.Folder.uploadFile

    def requests(self, folder=None):
        folder = folder or self.top
        return (folder.folder_requests + folder.file_requests
                + sum(self.requests(c) for c in folder.children))

    def test_install_then_update_then_check_then_adopt(self, small_tree, src, tmp_path):
        panel = self.fresh_project()
        mpath = str(tmp_path / "m.json")
        tr = FakeTransport(small_tree)
        n = len(small_tree)

        # Preview of a fresh install: one request, nothing created.
        plan, rep = S.sync(src, mpath, panel(), tr, F3D, dry_run=True,
                           verify_placed=False)
        assert len(plan.add) == n and self.top.children == []
        assert self.requests() == 1

        # Install.
        plan, rep = S.sync(src, mpath, panel(), tr, F3D, dry_run=False,
                           verify_placed=False)
        assert len(rep.added) == n and rep.ok and rep.pending == []
        assert self.Folder.uploads == n

        # Preview when up to date: one request (is the library folder there).
        before = self.requests()
        plan, rep = S.sync(src, mpath, panel(), tr, F3D, dry_run=True,
                           verify_placed=False)
        assert plan.is_empty
        assert self.requests() - before == 1

        # A second sync uploads nothing.
        S.sync(src, mpath, panel(), tr, F3D, dry_run=False, verify_placed=False)
        assert self.Folder.uploads == n

        # The full check sees every file once and nothing odd.
        plan, rep = S.sync(src, mpath, panel(), tr, F3D, dry_run=True,
                           verify_placed=True)
        assert plan.is_empty
        assert rep.check.files == n and rep.check.duplicates == {}
        assert rep.check.untracked == 0

        # Someone else adopts the same library from scratch.
        other = str(tmp_path / "other.json")
        m, stats = S.adopt_existing(src, other, panel(), tr, "v2.0.5", F3D,
                                    dry_run=False)
        assert stats["matched"] == n and stats["missing"] == 0
        S.sync(src, other, panel(), tr, F3D, dry_run=False, verify_placed=False)
        assert self.Folder.uploads == n, "adopted files were uploaded again"

    def test_a_deleted_part_comes_back_through_the_check(self, small_tree, src, tmp_path):
        panel = self.fresh_project()
        mpath = str(tmp_path / "m.json")
        tr = FakeTransport(small_tree)
        S.sync(src, mpath, panel(), tr, F3D, dry_run=False, verify_placed=False)

        def folders(f):
            yield f
            for c in f.children:
                yield from folders(c)
        victim = next(f for f in folders(self.top) if f.files)
        gone = victim.files.pop()

        plan, _ = S.sync(src, mpath, panel(), tr, F3D, dry_run=True,
                         verify_placed=False)
        assert plan.is_empty, "an ordinary preview doesn't look for deleted parts"

        plan, rep = S.sync(src, mpath, panel(), tr, F3D, dry_run=False,
                           verify_placed=True)
        assert len(plan.missing) == 1 and len(rep.added) == 1
        assert [f.name for f in victim.files].count(gone.name) == 1


class TestFastPath:
    """An ordinary Preview or Sync looks only at the folders it will put files
    into. Reading every folder of a big library is the full check's job."""

    def placed(self, tree, src, tmp_path, panel=None):
        panel = panel or FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               verify_placed=False)
        panel.scans, panel.listings = 0, []
        return panel, mpath

    def test_no_full_scan(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        mpath = str(tmp_path / "m.json")
        for dry in (True, False, True):
            S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                   dry_run=dry, verify_placed=False)
        assert panel.scans == 0

    def test_up_to_date_preview_asks_fusion_nothing(self, small_tree, src, tmp_path):
        panel, mpath = self.placed(small_tree, src, tmp_path)

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True, verify_placed=False)

        assert plan.is_empty
        assert panel.listings == [] and panel.scans == 0

    def test_an_update_looks_only_where_the_new_files_go(self, src, tmp_path):
        old = {f"Old/Dir{i}/Part.f3d": f"s{i}" for i in range(30)}
        panel, mpath = self.placed(old, src, tmp_path)
        new = dict(old, **{"New/A/One.f3d": "n1", "New/A/Two.f3d": "n2",
                           "Old/Dir3/Extra.f3d": "n3"})

        plan, rep = S.sync(src, mpath, panel, FakeTransport(new), F3D,
                           dry_run=True, verify_placed=False)

        assert len(plan.add) == 3
        # New/A doesn't exist yet, so only Old/Dir3 can be listed.
        assert panel.listings == [panel.folders[("Old", "Dir3")]]

    def test_preview_creates_no_folders(self, small_tree, src, tmp_path):
        panel = FakeDataPanel()
        S.sync(src, str(tmp_path / "m.json"), panel, FakeTransport(small_tree),
               F3D, dry_run=True, verify_placed=False)
        assert list(panel.folders) == [()], "preview created folders"

    def test_a_name_already_in_the_folder_is_still_refused(self, small_tree,
                                                           src, tmp_path):
        """The duplicate guard must not depend on the full check."""
        from detent_core import paths as P
        victim = sorted(small_tree)[0]
        pp = P.map_path(victim)
        panel = FakeDataPanel()
        panel.upload(panel.ensure_folder(pp.folders), "/tmp/x", pp.name)
        panel.uploads.clear()
        mpath = str(tmp_path / "m.json")

        plan, preview = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                               dry_run=True, verify_placed=False)
        assert [p for p, _n in preview.occupied] == [victim], \
            "Preview must show the refusal before the user clicks Yes"

        plan, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=False, verify_placed=False)
        assert [p for p, _n in rep.occupied] == [victim]
        assert pp.name not in panel.uploads
        assert panel.duplicates() == {}

    def test_the_full_check_still_finds_deleted_files(self, small_tree, src, tmp_path):
        panel, mpath = self.placed(small_tree, src, tmp_path)
        gone = panel.folders[next(k for k in panel.folders if k)]
        for fid in panel.contents:
            panel.contents[fid] = []

        fast, _ = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                         dry_run=True, verify_placed=False)
        full, rep = S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                           dry_run=True, verify_placed=True)

        assert fast.missing == [], "the fast path doesn't look for deleted files"
        assert len(full.missing) == len(small_tree)
        assert panel.scans == 1

    def test_a_library_folder_that_is_gone_is_noticed_without_asking(
            self, small_tree, src, tmp_path):
        """Deleting the whole library folder is the one case an ordinary sync
        must catch: saying "up to date" over nothing would be absurd."""
        panel, mpath = self.placed(small_tree, src, tmp_path)
        empty = FakeDataPanel(root_exists=False)      # same project, folder gone

        plan, rep = S.sync(src, mpath, empty, FakeTransport(small_tree), F3D,
                           dry_run=True, verify_placed=False)

        assert len(plan.missing) == len(small_tree)

    def test_the_full_check_reports_progress_and_can_be_cancelled(
            self, small_tree, src, tmp_path):
        panel, mpath = self.placed(small_tree, src, tmp_path)
        labels = []

        def watch(i, n, label):
            labels.append(label)
            return True

        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D, dry_run=True,
               verify_placed=True, on_progress=watch)
        assert any(label.startswith("Checking folder") for label in labels)

        with pytest.raises(gh.Cancelled):
            S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
                   dry_run=True, verify_placed=True,
                   on_progress=lambda i, n, label: False)


class TestRecheckIsOneListingPerFolder:
    """After a big sync, hundreds of files wait to be confirmed. Listing the
    folder once per file would read each folder dozens of times."""

    def test_many_waiting_files_in_one_folder(self, src, tmp_path):
        tree = {f"Bin/Part {i}.f3d": f"s{i}" for i in range(40)}
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(tree), F3D, dry_run=False,
               settle_wait=0.0, verify_placed=False)
        assert len(Manifest.load(mpath).in_state(INFLIGHT)) == 40

        panel.temp_ids = 0                            # Fusion has finished
        panel.listings = []
        plan, rep = S.sync(src, mpath, panel, FakeTransport(tree), F3D,
                           dry_run=False, verify_placed=False)

        assert len(rep.reconciled) == 40
        assert panel.listings == [panel.folders[("Bin",)]], panel.listings
        assert panel.uploads.count("Part 0") == 1
        assert panel.duplicates() == {}

    def test_rechecking_creates_no_folders(self, src, tmp_path):
        mpath = str(tmp_path / "m.json")
        m = Manifest(source_id="x", repo=src.repo, ref=src.ref)
        m.mark_inflight("Gone/Part.f3d")
        m.save(mpath)
        panel = FakeDataPanel()

        rep = S.Report()
        S.reconcile_inflight(Manifest.load(mpath), panel, rep)

        assert list(panel.folders) == [()]
        assert rep.reconciled == [("Gone/Part.f3d", "not present, will retry")]


class TestAdoptReadsOnlyWhatItNeeds:
    def library(self, tree):
        from detent_core import paths as P
        panel = FakeDataPanel()
        for repo_path in tree:
            pp = P.map_path(repo_path)
            panel.upload(panel.ensure_folder(pp.folders), "/tmp/x", pp.name)
        panel.uploads.clear()
        panel.listings = []
        return panel

    def test_one_listing_per_release_folder_and_no_scan(self, small_tree, src, tmp_path):
        from detent_core import paths as P
        panel = self.library(small_tree)
        other = panel.ensure_folder(("My own parts",))
        panel.upload(other, "/tmp/x", "Mine")

        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"), panel,
                                    FakeTransport(small_tree), "v2.0.5", F3D)

        folders = {P.map_path(p).folders for p in small_tree}
        assert sorted(panel.listings) == sorted(panel.folders[f] for f in folders)
        assert panel.scans == 0
        assert stats["matched"] == len(small_tree)

    def test_progress_and_cancel(self, small_tree, src, tmp_path):
        panel = self.library(small_tree)
        labels = []
        S.adopt_existing(src, str(tmp_path / "m.json"), panel,
                         FakeTransport(small_tree), "v2.0.5", F3D,
                         on_progress=lambda i, n, label: labels.append(label) or True)
        assert labels and all(l.startswith("Reading folder") for l in labels)

        with pytest.raises(gh.Cancelled):
            S.adopt_existing(src, str(tmp_path / "m.json"), panel,
                             FakeTransport(small_tree), "v2.0.5", F3D,
                             on_progress=lambda i, n, label: False)

    def test_a_file_still_uploading_is_not_adopted(self, small_tree, src, tmp_path):
        panel = self.library(small_tree)
        panel.temp_ids = 10_000
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"), panel,
                                    FakeTransport(small_tree), "v2.0.5", F3D)
        assert stats["matched"] == 0 and len(m.files) == 0


class TestTimings:
    def test_every_run_reports_where_the_time_went(self, small_tree, src, tmp_path):
        _plan, rep = S.sync(src, str(tmp_path / "m.json"), FakeDataPanel(),
                            FakeTransport(small_tree), F3D, dry_run=False)
        assert list(rep.timings) == ["github", "recheck", "check", "look",
                                     "folders", "download", "send", "confirm",
                                     "record"]
        assert all(t >= 0 for t in rep.timings.values())

    def test_text(self):
        text = S.timing_text({"github": 0.4, "download": 151.2, "send": 20.0})
        assert text == "2 min 52 s (download 2 min 31 s, send 20 s)"
        assert S.timing_text({"github": 0.2}) == "0 s"


class TestAddin:
    """Detent.py needs Fusion to run, so these read its source."""

    def source(self):
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Detent.py"), encoding="utf-8").read()

    def test_a_folder_fusion_wont_open_gets_a_message_not_a_traceback(self):
        source = self.source()
        handler = source[source.index("except PanelError as exc:"):][:300]
        assert "restart Fusion" in handler

    def test_only_the_check_action_reads_every_folder(self):
        source = self.source()
        assert "full=action == A.VERIFY or bool(stranded)" in source
        assert source.count("verify_placed=full") == 2, \
            "the preview pass and the upload pass must both follow the action"
        assert "source.verify_placed" not in source

    def test_a_clean_check_reads_the_folders_once_not_twice(self):
        source = self.source()
        stop = source.index("if full and not n:")
        upload_pass = source.index("dry_run=False, on_progress=_watch(progress)")
        assert stop < upload_pass
        assert "return" in source[stop:upload_pass]

    def test_the_dialog_offers_every_action_and_describes_the_one_picked(self):
        source = self.source()
        assert "for name in A.ORDER:" in source
        assert 'inputs.itemById("about").text = A.ABOUT.get(picked, "")' in source
        assert 'A.ABOUT[A.ORDER[0]]' in source, "the default action needs its text too"

    def test_adopt_says_when_it_worked_out_the_release(self):
        source = self.source()
        body = source[source.index("def _do_adopt"):]
        assert 'stats["detected"]' in body
        assert "Worked out from the parts in your folder" in body
        assert "type your release in Installed release" in body
        assert "Leave blank and Detent works it out" in source

    def test_changed_upstream_says_how_to_update_a_part(self):
        source = self.source()
        assert "Phase 2" not in source
        preview = source[source.index("def _plan_text"):]
        preview = preview[:preview.index("\ndef ")]
        done = source[source.index("def _report_text"):]
        done = done[:done.index("\ndef ")]
        steps = source[source.index("def _update_steps"):]
        steps = steps[:steps.index("\ndef ")]
        assert "Import New Change" in steps and "Get Latest" in steps
        for body in (preview, done):
            assert "_update_steps()" in body
        assert "report.skipped_changed" in done
        sync_src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "detent_core", "sync.py"),
            encoding="utf-8").read()
        assert "Phase 2" not in sync_src

    def test_adopt_box_explains_parts_assumed_older(self):
        body = self.source()[self.source().index("def _do_adopt"):]
        assert 'stats["assumed_older"]' in body
        assert 'stats["newer"]' in body

    def test_installed_release_shows_only_for_adopt(self):
        source = self.source()
        assert "adopt_ref.isVisible = False" in source
        assert 'inputs.itemById("adopt_ref").isVisible = picked == A.ADOPT' in source

    def test_no_action_is_matched_by_a_typed_out_name(self):
        """A renamed action must not quietly stop being recognised."""
        source = self.source()
        assert "action.startswith(" not in source

    def test_still_uploading_is_explained_in_the_done_box(self):
        source = self.source()
        assert "Fusion is still uploading" in source
        assert "Nothing is uploaded twice" in source

    def test_every_run_is_timed(self):
        source = self.source()
        assert source.count("_log_timing(") >= 4      # def + preview, sync, adopt
        assert "timing.log" in source


class TestConfigIgnoresTheOldSwitch:
    def test_an_old_config_with_verify_placed_still_loads(self):
        from detent_core.config import Config
        cfg = Config.from_dict({"schema": 1, "sources": [
            {"id": "x", "label": "X", "repo": "a/b", "verify_placed": True}]})
        assert not hasattr(cfg.sources[0], "verify_placed")
        assert "verify_placed" not in cfg.to_dict()["sources"][0]


class TestActions:
    def root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_four_actions_each_with_a_description(self):
        from detent_core import actions as A
        assert A.ORDER == ("Sync now", "Preview changes", "Verify Integrity",
                           "Adopt existing library")
        assert set(A.ABOUT) == set(A.ORDER)
        assert all(len(text) > 20 for text in A.ABOUT.values())

    def test_a_description_fits_the_dialog(self):
        """The box under Action is three lines tall."""
        from detent_core import actions as A
        assert all(len(text) <= 200 for text in A.ABOUT.values()), \
            {k: len(v) for k, v in A.ABOUT.items()}

    def test_the_readme_explains_every_action_by_its_name(self):
        from detent_core import actions as A
        readme = open(os.path.join(self.root(), "README.md"), encoding="utf-8").read()
        table = readme[readme.index("## The four actions"):]
        for name in A.ORDER:
            assert f"| **{name}** |" in table, f"{name} is missing from the table"
        assert "Check library" not in readme


class ReleasesTransport(FakeTransport):
    """Serves several releases, each with its own tree."""

    def __init__(self, trees, releases, fail_releases=False):
        super().__init__(trees[releases[0]])
        self.trees = trees
        self.releases = releases
        self.fail_releases = fail_releases
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        if "/releases?" in url:
            if self.fail_releases:
                raise gh.GitHubError("simulated: GitHub unavailable")
            return [{"tag_name": t, "draft": False} for t in self.releases], {}
        if "/git/trees/" in url:
            ref = urllib.parse.unquote(url.split("/git/trees/")[1].split("?")[0])
            # Sync reads the tree by commit; that's the current ref.
            tree = self.trees.get(ref) or self.trees["main"]
            return ({"truncated": False,
                     "tree": [{"path": p, "type": "blob", "sha": s}
                              for p, s in tree.items()]}, {})
        return super().get_json(url)


class TestAdoptWorksOutTheRelease:
    """With Installed release blank, Adopt finds the release whose parts match
    the folder. People don't know which release they downloaded."""

    T3 = PL.select(load_tree("v2.0.3"), F3D)
    T5 = PL.select(load_tree("v2.0.5"), F3D)

    def library(self, tree):
        from detent_core import paths as P
        panel = FakeDataPanel()
        for repo_path in tree:
            pp = P.map_path(repo_path)
            panel.upload(panel.ensure_folder(pp.folders), "/tmp/x", pp.name)
        panel.uploads.clear()
        return panel

    def transport(self, **kw):
        return ReleasesTransport({"v2.0.5": self.T5, "v2.0.3": self.T3,
                                  "main": self.T5},
                                 ["v2.0.5", "v2.0.3"], **kw)

    def test_finds_an_older_release_and_sync_adds_only_the_newer_parts(
            self, src, tmp_path):
        panel = self.library(self.T3)
        tr = self.transport()
        mpath = str(tmp_path / "m.json")

        m, stats = S.adopt_existing(src, mpath, panel, tr, None, F3D,
                                    dry_run=False)

        assert stats["release"] == "v2.0.3" and stats["detected"]
        assert stats["matched"] == len(self.T3)
        assert stats["unverified"] == 0
        assert all(e.blob == self.T3[p] for p, e in m.files.items())

        tr5 = FakeTransport(self.T5)
        plan, _rep = S.sync(src, mpath, panel, tr5, F3D, dry_run=True)
        assert plan.add == sorted(set(self.T5) - set(self.T3))
        assert plan.unverified == [] and plan.change == []

    def test_finds_the_latest_release(self, src, tmp_path):
        panel = self.library(self.T5)
        mpath = str(tmp_path / "m.json")
        m, stats = S.adopt_existing(src, mpath, panel, self.transport(), None,
                                    F3D, dry_run=False)
        assert stats["release"] == "v2.0.5"
        plan, _rep = S.sync(src, mpath, panel, FakeTransport(self.T5), F3D)
        assert plan.is_empty

    def test_a_few_missing_parts_dont_change_the_answer(self, src, tmp_path):
        some = dict(list(self.T3.items())[30:])
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"),
                                    self.library(some), self.transport(), None,
                                    F3D)
        assert stats["release"] == "v2.0.3"
        assert stats["matched"] == len(some)
        assert stats["missing"] == 30

    def test_releases_with_the_same_names_assume_the_older_version(
            self, src, tmp_path):
        """Two releases with the same part names can't be told apart. A part
        whose content differs between them is recorded as the older version,
        so it shows as changed upstream instead of looking current."""
        edited = sorted(self.T5)[:2]
        t6 = dict(self.T5)
        for p in edited:
            t6[p] = "edited-" + p
        tr = ReleasesTransport({"v2.0.6": t6, "v2.0.5": self.T5, "main": t6},
                               ["v2.0.6", "v2.0.5"])
        mpath = str(tmp_path / "m.json")

        m, stats = S.adopt_existing(src, mpath, self.library(self.T5), tr,
                                    None, F3D, dry_run=False)

        assert stats["release"] == "v2.0.6" and stats["tied"] == ["v2.0.5"]
        assert stats["assumed_older"] == 2
        assert all(e.blob == self.T5[p] for p, e in m.files.items())

    def test_a_typed_release_is_used_as_is(self, src, tmp_path):
        tr = self.transport()
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"),
                                    self.library(self.T3), tr, "v2.0.5", F3D)
        assert stats["release"] == "v2.0.5" and not stats["detected"]
        assert all(e.blob == self.T5[p] for p, e in m.files.items())

    def test_each_release_is_read_once_and_each_folder_once(self, src, tmp_path):
        panel = self.library(self.T3)
        panel.listings = []
        tr = self.transport()
        S.adopt_existing(src, str(tmp_path / "m.json"), panel, tr, None, F3D)
        trees = [u for u in tr.urls if "/git/trees/" in u]
        assert len(trees) == 3, "each release and the current ref, once each"
        assert len(panel.listings) == len(set(panel.listings))

    def test_an_empty_folder_works_out_nothing(self, src, tmp_path):
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"),
                                    FakeDataPanel(), self.transport(), None, F3D)
        assert not stats["detected"] and stats["release"] is None
        assert stats["matched"] == 0

    def test_if_github_wont_list_releases_parts_are_still_adopted(
            self, src, tmp_path):
        tr = ReleasesTransport({"v2.0.5": self.T5, "main": self.T5},
                               ["v2.0.5"], fail_releases=True)
        m, stats = S.adopt_existing(src, str(tmp_path / "m.json"),
                                    self.library(self.T5), tr, None, F3D)
        assert not stats["detected"]
        assert stats["matched"] == len(self.T5)
        assert all(e.blob is None for e in m.files.values())


class TestAdoptEditedParts:
    """A release can edit parts that already exist. Fusion can't swap a part
    in place, so Detent never replaces it. What Adopt records decides whether
    the user is told. Built on the real v2.0.5 tree: v2.0.6 here adds three
    parts and edits two, like the real v2.0.6 (72 added, 2 edited)."""

    T5 = PL.select(load_tree("v2.0.5"), F3D)
    EDITED = sorted(T5)[:2]
    NEW = ["Motion/New Shaft/A.f3d", "Motion/New Shaft/B.f3d",
           "Motion/New Shaft/C.f3d"]

    def t6(self):
        t = dict(self.T5)
        for p in self.EDITED:
            t[p] = "v206-" + p
        for p in self.NEW:
            t[p] = "new-" + p
        return t

    def transport(self):
        t6 = self.t6()
        return ReleasesTransport({"v2.0.6": t6, "v2.0.5": self.T5, "main": t6},
                                 ["v2.0.6", "v2.0.5"])

    def library(self, paths):
        from detent_core import paths as P
        panel = FakeDataPanel()
        for repo_path in sorted(paths):
            pp = P.map_path(repo_path)
            panel.upload(panel.ensure_folder(pp.folders), "/tmp/x", pp.name)
        panel.uploads.clear()
        return panel

    def adopt_then_preview(self, paths, at_ref, tmp_path, src):
        panel = self.library(paths)
        mpath = str(tmp_path / "m.json")
        m, stats = S.adopt_existing(src, mpath, panel, self.transport(), at_ref,
                                    F3D, dry_run=False)
        plan, _rep = S.sync(src, mpath, panel, self.transport(), F3D)
        return m, stats, plan

    def test_upgrade_from_the_older_release_adds_new_and_lists_edited(
            self, src, tmp_path):
        m, stats, plan = self.adopt_then_preview(self.T5, None, tmp_path, src)
        assert stats["release"] == "v2.0.5"
        assert plan.add == sorted(self.NEW)
        assert plan.change == self.EDITED

    def test_new_parts_added_by_hand_are_assumed_with_the_old_edited_parts(
            self, src, tmp_path):
        """Someone installed only the new parts of v2.0.6 by hand. The names
        say v2.0.6, but their edited parts may be the old ones. Detent must
        say so, not record them as current."""
        m, stats, plan = self.adopt_then_preview(
            set(self.T5) | set(self.NEW), None, tmp_path, src)
        assert stats["release"] == "v2.0.6" and stats["detected"]
        assert stats["assumed_older"] == 2
        assert plan.add == []
        assert plan.change == self.EDITED

    def test_typing_the_release_trusts_it_and_clears_changed_upstream(
            self, src, tmp_path):
        """After replacing parts by hand, Adopt with the release typed in
        records what's there as that release."""
        m, stats, plan = self.adopt_then_preview(
            set(self.T5) | set(self.NEW), "v2.0.6", tmp_path, src)
        assert stats["assumed_older"] == 0
        assert plan.add == [] and plan.change == []

    def test_typing_an_older_release_still_records_newer_parts(
            self, src, tmp_path):
        """Typed v2.0.5 while the folder also has v2.0.6's new parts. They
        used to be left out of the record, so every run listed them to add
        and refused them as NAME TAKEN."""
        m, stats, plan = self.adopt_then_preview(
            set(self.T5) | set(self.NEW), "v2.0.5", tmp_path, src)
        assert stats["newer"] == len(self.NEW)
        assert all(p in m.files for p in self.NEW)
        assert plan.add == []
        assert plan.change == self.EDITED

    def test_sync_after_adopt_never_duplicates_or_replaces(self, src, tmp_path):
        panel = self.library(self.T5)
        mpath = str(tmp_path / "m.json")
        S.adopt_existing(src, mpath, panel, self.transport(), None, F3D,
                         dry_run=False)
        plan, rep = S.sync(src, mpath, panel, self.transport(), F3D,
                           dry_run=False, settle_wait=0)
        assert sorted(rep.added + rep.pending) == sorted(self.NEW)
        assert rep.skipped_changed == self.EDITED, "told about edited parts"
        assert panel.duplicates() == {}
        plan, _rep = S.sync(src, mpath, panel, self.transport(), F3D)
        assert plan.add == [] and plan.change == [], "told once, then dropped"

    def test_preview_keeps_listing_changed_parts_until_sync_now(
            self, src, tmp_path):
        panel = self.library(self.T5)
        mpath = str(tmp_path / "m.json")
        S.adopt_existing(src, mpath, panel, self.transport(), None, F3D,
                         dry_run=False)
        for _ in range(2):
            plan, _rep = S.sync(src, mpath, panel, self.transport(), F3D)
            assert plan.change == self.EDITED

    def test_a_blocked_sync_keeps_listing_changed_parts(self, src, tmp_path):
        panel = self.library(self.T5)
        mpath = str(tmp_path / "m.json")
        S.adopt_existing(src, mpath, panel, self.transport(), None, F3D,
                         dry_run=False)
        panel.refuse_folders = {("Motion", "New Shaft")}
        _plan, rep = S.sync(src, mpath, panel, self.transport(), F3D,
                            dry_run=False, settle_wait=0)
        assert rep.stopped_early
        panel.refuse_folders = set()
        plan, _rep = S.sync(src, mpath, panel, self.transport(), F3D)
        assert plan.change == self.EDITED

    def test_dropping_a_changed_part_keeps_its_fusion_file(self, src, tmp_path):
        panel = self.library(self.T5)
        mpath = str(tmp_path / "m.json")
        m, _st = S.adopt_existing(src, mpath, panel, self.transport(), None,
                                  F3D, dry_run=False)
        before = {p: m.files[p].lineage for p in self.EDITED}
        S.sync(src, mpath, panel, self.transport(), F3D, dry_run=False,
               settle_wait=0)
        after = Manifest.load(mpath)
        assert {p: after.files[p].lineage for p in self.EDITED} == before


class TestConfirmLaterKeepsTheUploadedVersion:
    """Detent doesn't wait for Fusion, so an upload is confirmed on a later
    run. If a new release edits that part in between, the record must keep
    the version that was uploaded, not the new one (found in the 11 Oct
    rehearsal: v2.0.5 shafts were recorded as v2.0.6)."""

    def test_confirmed_after_a_new_release_shows_as_changed(
            self, small_tree, src, tmp_path):
        edited = sorted(small_tree)[0]
        newer = dict(small_tree)
        newer[edited] = "edited-" + edited
        panel = FakeDataPanel(temp_ids=10_000)   # uploads stay unconfirmed
        mpath = str(tmp_path / "m.json")

        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
               dry_run=False, settle_wait=0)
        assert Manifest.load(mpath).get(edited).state == INFLIGHT

        panel.temp_ids = 0                        # Fusion has finished
        plan, _rep = S.sync(src, mpath, panel, FakeTransport(newer), F3D,
                            recheck=True)         # what Verify Integrity does
        assert Manifest.load(mpath).get(edited).blob == small_tree[edited]
        assert plan.change == [edited]

    def test_the_uploaded_version_is_noted_before_confirming(
            self, small_tree, src, tmp_path):
        panel = FakeDataPanel(temp_ids=10_000)
        mpath = str(tmp_path / "m.json")
        S.sync(src, mpath, panel, FakeTransport(small_tree), F3D,
               dry_run=False, settle_wait=0)
        m = Manifest.load(mpath)
        assert all(m.get(p).blob == small_tree[p] for p in small_tree)
