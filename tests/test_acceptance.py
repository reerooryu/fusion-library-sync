"""Acceptance tests against the real VEX-CAD corpus.

Fixtures are genuine git trees (blob SHA + path) at tags v2.0.3 and v2.0.5 —
the same shape the GitHub trees API returns. No network, no Fusion.
"""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths as P
from core import plan as PL
from core.manifest import Manifest, adopt, PLACED, ADOPTED, INFLIGHT

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
F3D_ONLY = ("**/*.f3d",)


def load_tree(tag):
    tree = {}
    with open(os.path.join(FIX, f"tree_{tag}.tsv"), encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            sha, _, path = line.partition("\t")
            tree[path] = sha
    return tree


@pytest.fixture(scope="module")
def t203():
    return load_tree("v2.0.3")


@pytest.fixture(scope="module")
def t205():
    return load_tree("v2.0.5")


# ---------------------------------------------------------------- A3 --------
class TestA3Delta:
    """Adopt at v2.0.3, sync to v2.0.5: exactly the right files, nothing else."""

    def test_fixtures_are_the_real_corpus(self, t203, t205):
        assert len(t203) == 1168
        assert len(t205) == 1201

    def test_adopt_uploads_nothing_and_claims_everything(self, t203):
        sel = PL.select(t203, F3D_ONLY)
        local = {p: f"urn:adsk.wipprod:dm.lineage:fake-{i}" for i, p in enumerate(sel)}
        m = adopt("vex", "VEX-CAD/x", "main", sel, local)

        assert len(m.files) == len(sel)
        assert all(e.state == ADOPTED for e in m.files.values())
        assert all(e.blob is not None for e in m.files.values())
        # Adoption is a bookkeeping operation. Nothing is queued for upload.
        assert PL.diff(sel, m).is_empty

    def test_delta_adds_only_new_files(self, t203, t205):
        a, b = PL.select(t203, F3D_ONLY), PL.select(t205, F3D_ONLY)
        local = {p: f"urn:fake:{i}" for i, p in enumerate(a)}
        m = adopt("vex", "VEX-CAD/x", "main", a, local)

        p = PL.diff(b, m)
        expected_add = sorted(set(b) - set(a))

        assert p.add == expected_add
        assert len(p.add) == 33, "v2.0.3 -> v2.0.5 added 33 CAD files"
        assert p.orphan == [], "no .f3d was deleted between these tags"

    def test_no_cad_file_was_ever_modified(self, t203, t205):
        """The finding the whole design rests on.

        Between these releases only README.md and changelog.md change, so a
        .f3d-only sync has an empty `change` set and Phase 1 alone is enough.
        """
        a, b = PL.select(t203, F3D_ONLY), PL.select(t205, F3D_ONLY)
        local = {p: f"urn:fake:{i}" for i, p in enumerate(a)}
        m = adopt("vex", "VEX-CAD/x", "main", a, local)

        assert PL.diff(b, m).change == []

        # Sanity: without the filter, the modified files show up and are docs.
        m_all = adopt("vex", "VEX-CAD/x", "main", t203,
                      {p: f"urn:fake:{i}" for i, p in enumerate(t203)})
        changed = PL.diff(t205, m_all).change
        assert changed == ["README.md", "changelog.md"]

    def test_adopting_at_head_would_hide_the_delta(self, t203, t205):
        """Why adopt() takes a release rather than HEAD.

        Recording today's SHAs against yesterday's files makes them look
        current, and they never update again.
        """
        a, b = PL.select(t203, F3D_ONLY), PL.select(t205, F3D_ONLY)
        local = {p: f"urn:fake:{i}" for i, p in enumerate(a)}

        wrong = adopt("vex", "r", "main", b, local)   # adopted against HEAD
        right = adopt("vex", "r", "main", a, local)   # adopted against v2.0.3

        assert PL.diff(b, right).add == sorted(set(b) - set(a))
        # The wrong version silently claims the stale files are current.
        assert all(wrong.files[p].blob == b[p] for p in local if p in b)

    def test_unknown_release_defers_rather_than_guesses(self, t203, t205):
        a, b = PL.select(t203, F3D_ONLY), PL.select(t205, F3D_ONLY)
        local = {p: f"urn:fake:{i}" for i, p in enumerate(a)}
        m = adopt("vex", "r", "main", a, local, release_known=False)

        p = PL.diff(b, m)
        assert p.change == []
        assert len(p.unverified) == len(local)   # flagged, not assumed
        assert p.add == sorted(set(b) - set(a))  # genuinely new files still added

    def test_idempotence_A1(self, t205):
        sel = PL.select(t205, F3D_ONLY)
        m = Manifest("vex", "VEX-CAD/x")
        for i, (path, blob) in enumerate(sel.items()):
            m.record(path, blob, f"urn:fake:{i}")
        assert PL.diff(sel, m).is_empty
        assert PL.diff(sel, m).is_empty   # and again


# ---------------------------------------------------------------- A5 --------
class TestA5HostilePaths:
    """The corpus fights back: smart quotes, leading '!', deep nesting."""

    def test_smart_quote_files_exist_in_the_corpus(self, t205):
        smart = [p for p in t205 if "”" in p]
        assert len(smart) == 5, "0.5” OD HS Spacer files use U+201D"

    def test_smart_quotes_survive_verbatim(self, t205):
        for p in [p for p in t205 if "”" in p]:
            pp = P.map_path(p)
            assert "”" in pp.panel_path, "must not fold U+201D to a quote"
            assert pp.alterations == ()
            assert pp.repo_path == p, "manifest key stays the repo path"

    def test_normalising_unicode_would_break_the_manifest(self, t205):
        """Guards rule 2. Folding the quote makes every later sync see a
        phantom change, because the manifest key stops matching the tree."""
        import unicodedata
        p = next(p for p in t205 if "”" in p)
        folded = unicodedata.normalize("NFKD", p).replace("”", '"')
        assert folded != p
        m = Manifest("vex", "r")
        m.record(folded, "sha", "urn:fake:1")       # the bug we must not ship
        assert PL.diff({p: "sha"}, m).add == [p]    # phantom add
        assert PL.diff({p: "sha"}, m).orphan == [folded]

    def test_leading_bang_is_preserved(self, t205):
        bang = [p for p in t205 if p.rsplit("/", 1)[-1].startswith("!")]
        assert bang, "corpus contains a file starting with '!'"
        for p in bang:
            assert P.map_path(p).name.startswith("!")

    def test_extension_stripped_but_key_retained(self, t205):
        p = next(p for p in t205 if p.endswith(".f3d"))
        pp = P.map_path(p)
        assert not pp.name.endswith(".f3d")
        assert pp.repo_path.endswith(".f3d")

    def test_whole_corpus_maps_without_error_or_collision(self, t205):
        sel = PL.select(t205, F3D_ONLY)
        mapped, errors, collisions = P.map_all(sorted(sel))
        assert errors == [], f"unmappable paths: {errors[:3]}"
        assert collisions == {}, f"two files would land in one place: {list(collisions)[:3]}"
        assert len(mapped) == len(sel)

    def test_depth_and_length_of_the_real_corpus(self, t205):
        sel = PL.select(t205, F3D_ONLY)
        assert max(p.count("/") for p in sel) + 1 == 7
        assert max(len(p) for p in sel) == 135

    def test_alterations_are_never_silent(self):
        pp = P.map_path("Folder/Trailing space .f3d")
        assert pp.altered and pp.alterations
        assert pp.name == "Trailing space"

    def test_traversal_and_junk_rejected(self):
        for bad in ("../escape.f3d", "a/../../b.f3d", "trailing/", ""):
            with pytest.raises(P.PathError):
                P.map_path(bad)


# ------------------------------------------------------- manifest ----------
class TestManifestDurability:
    def test_atomic_save_and_roundtrip(self, tmp_path, t205):
        sel = PL.select(t205, F3D_ONLY)
        m = Manifest("vex", "VEX-CAD/x", synced_commit="f13995d")
        for i, (path, blob) in enumerate(list(sel.items())[:50]):
            m.record(path, blob, f"urn:fake:{i}")
        f = tmp_path / "vex.json"
        m.save(str(f))
        back = Manifest.load(str(f))
        assert back.to_dict() == m.to_dict()
        assert not list(tmp_path.glob("*.tmp")), "temp file left behind"

    def test_smart_quotes_survive_json(self, tmp_path, t205):
        p = next(p for p in t205 if "”" in p)
        m = Manifest("vex", "r")
        m.record(p, "sha", "urn:fake:1")
        f = tmp_path / "m.json"
        m.save(str(f))
        assert "”" in Manifest.load(str(f)).to_dict()["files"].popitem()[0]

    def test_inflight_is_reported_not_re_added(self):
        """A4. An interrupted upload must not look like a fresh add, or the
        retry duplicates the file."""
        m = Manifest("vex", "r")
        m.mark_inflight("Motion/LS Shaft.f3d")
        p = PL.diff({"Motion/LS Shaft.f3d": "sha"}, m)
        assert p.add == []
        assert p.inflight == ["Motion/LS Shaft.f3d"]

    def test_dropping_inflight_makes_it_addable_again(self):
        m = Manifest("vex", "r")
        m.mark_inflight("x.f3d")
        m.drop("x.f3d")
        assert PL.diff({"x.f3d": "sha"}, m).add == ["x.f3d"]

    def test_schema_mismatch_refuses_to_load(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"schema": 99, "source_id": "x", "repo": "r"}))
        with pytest.raises(ValueError):
            Manifest.load(str(f))
