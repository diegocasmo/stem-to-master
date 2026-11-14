import hashlib
import http.client
import io
import subprocess

import pytest

import plugins
from prepared import bundle_hash

ENTRY = {"name": "Fake", "version": "1", "url": "https://example.invalid/Fake.dmg", "sha256": "0" * 64,
         "bundle_sha256": "1" * 64}


def test_a_download_with_another_hash_stops_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path)
    monkeypatch.setattr(plugins.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"not the locked file"))
    with pytest.raises(SystemExit, match="plugin Fake: download sha256 mismatch, expected 0+, actual [0-9a-f]{64}"):
        plugins.install(ENTRY)
    assert not (tmp_path / "Fake.vst3").exists()


def test_an_installed_bundle_with_another_hash_stops_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path)
    (tmp_path / "Fake.vst3" / "Contents").mkdir(parents=True)
    (tmp_path / "Fake.vst3" / "Contents" / "binary").write_bytes(b"other build")
    with pytest.raises(SystemExit, match="plugin Fake: installed bundle sha256 .* mismatch, expected 1+, actual"):
        plugins.install(ENTRY)


def test_a_cut_off_download_stops_the_install_with_both_hashes(tmp_path, monkeypatch):
    def cut_off(*a, **k):
        raise http.client.IncompleteRead(b"partial")
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path)
    monkeypatch.setattr(plugins.urllib.request, "urlopen", cut_off)
    with pytest.raises(SystemExit, match="plugin Fake: download failed .* expected sha256 0+, actual none"):
        plugins.install(ENTRY)


def test_a_bundle_in_a_pkg_in_a_zip_named_like_a_script_installs(tmp_path, monkeypatch):
    # TDR ships a zip of a pkg whose payload is the bundle itself; Klanghelm's link saves a zip as "...dl.php".
    root = tmp_path / "root"
    (root / "Contents").mkdir(parents=True)
    (root / "Contents" / "binary").write_bytes(b"fake plugin")
    pkg = tmp_path / "Fake.pkg"
    subprocess.run(["pkgbuild", "--root", root, "--identifier", "test.fake", "--install-location",
                    "/Library/Audio/Plug-Ins/VST3/Fake.vst3", pkg], check=True, capture_output=True)
    archive = tmp_path / "Fakedl.php"
    subprocess.run(["ditto", "-c", "-k", pkg, archive], check=True)
    entry = dict(ENTRY, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), bundle_sha256=bundle_hash(root))
    monkeypatch.setattr(plugins, "PLUGINS", tmp_path / "plugins")
    monkeypatch.setattr(plugins.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(archive.read_bytes()))
    assert plugins.install(entry) == "installed"
    assert (tmp_path / "plugins" / "Fake.vst3" / "Contents" / "binary").read_bytes() == b"fake plugin"


def test_every_entry_checks_the_hash_of_a_shared_download(tmp_path, monkeypatch):
    calls = []

    def fetch(*a, **k):
        calls.append(1)
        return io.BytesIO(b"vendor archive")

    monkeypatch.setattr(plugins.urllib.request, "urlopen", fetch)
    good = dict(ENTRY, sha256=hashlib.sha256(b"vendor archive").hexdigest())
    assert plugins.download(good, tmp_path).read_bytes() == b"vendor archive"
    with pytest.raises(SystemExit, match="plugin Second: download sha256 mismatch"):
        plugins.download(dict(good, name="Second", sha256="0" * 64), tmp_path)
    assert len(calls) == 1
