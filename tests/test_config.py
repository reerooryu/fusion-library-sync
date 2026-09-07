import os, sys, json, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detent_core import config as C


class TestRepoNormalisation:
    @pytest.mark.parametrize("raw", [
        "VEX-CAD/VEX-CAD-Fusion-360-Library",
        "https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library",
        "https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library.git",
        "http://www.github.com/VEX-CAD/VEX-CAD-Fusion-360-Library/",
        "  github.com/VEX-CAD/VEX-CAD-Fusion-360-Library  ",
    ])
    def test_accepts_what_people_paste(self, raw):
        assert C.normalise_repo(raw) == "VEX-CAD/VEX-CAD-Fusion-360-Library"

    @pytest.mark.parametrize("bad", ["", "   ", "notarepo", "https://gitlab.com/a/b",
                                     "owner/name/extra"])
    def test_rejects_junk(self, bad):
        with pytest.raises(C.ConfigError):
            C.normalise_repo(bad)


class TestConfig:
    def test_default_ships_vex(self):
        cfg = C.default_config()
        assert len(cfg.sources) == 1
        s = cfg.sources[0]
        assert s.repo == "VEX-CAD/VEX-CAD-Fusion-360-Library"
        assert s.include == ["**/*.f3d"]

    def test_roundtrip(self, tmp_path):
        cfg = C.default_config()
        p = str(tmp_path / "config.json")
        cfg.save(p)
        assert C.Config.load(p).to_dict() == cfg.to_dict()
        assert not list(tmp_path.glob("*.tmp"))

    def test_load_or_create_writes_defaults_once(self, tmp_path):
        p = str(tmp_path / "config.json")
        assert not os.path.exists(p)
        C.Config.load_or_create(p)
        assert os.path.exists(p)
        with open(p) as fh:
            fh_data = json.load(fh)
        fh_data["sources"][0]["ref"] = "edited"
        with open(p, "w") as fh:
            json.dump(fh_data, fh)
        assert C.Config.load_or_create(p).sources[0].ref == "edited", "clobbered user edits"

    def test_missing_file_returns_defaults_without_writing(self, tmp_path):
        p = str(tmp_path / "nope.json")
        assert C.Config.load(p).sources[0].id == "vex-cad"
        assert not os.path.exists(p)

    def test_schema_mismatch_refuses(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"schema": 99, "sources": []}))
        with pytest.raises(C.ConfigError):
            C.Config.load(str(p))

    def test_duplicate_ids_refused(self):
        d = {"schema": 1, "sources": [
            {"id": "a", "label": "A", "repo": "x/y"},
            {"id": "a", "label": "B", "repo": "x/z"}]}
        with pytest.raises(C.ConfigError):
            C.Config.from_dict(d)

    def test_unknown_keys_ignored_not_fatal(self):
        d = {"schema": 1, "sources": [
            {"id": "a", "label": "A", "repo": "x/y", "future_option": True}]}
        assert C.Config.from_dict(d).sources[0].id == "a"

    def test_folder_path_splits(self):
        s = C.SourceConfig(id="a", label="A", repo="x/y", folder_path="/Libraries/VEX/")
        assert s.folders == ["Libraries", "VEX"]
        assert C.SourceConfig(id="a", label="A", repo="x/y").folders == []

    def test_manifest_name_is_per_source(self):
        assert C.SourceConfig(id="vex-cad", label="V", repo="x/y").manifest_name \
            == "vex-cad.manifest.json"
