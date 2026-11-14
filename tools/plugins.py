"""Install plugins.lock into plugins/: uv run tools/plugins.py

A bundle already in plugins/ with the locked hash is kept. Anything else (failed download, wrong download or bundle
hash) stops with the plugin's name, so a run never continues with a plugin other than the locked one.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from prepared import bundle_hash

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "plugins.lock"
PLUGINS = ROOT / "plugins"


def fail(name, message):
    sys.exit(f"plugin {name}: {message}")


def check(name, what, expected, actual):
    if actual != expected:
        fail(name, f"{what} mismatch, expected {expected}, actual {actual}")


def download(entry, folder):
    path = folder / entry["url"].rsplit("/", 1)[1]
    try:
        with urllib.request.urlopen(entry["url"], timeout=120) as response, path.open("wb") as out:
            shutil.copyfileobj(response, out)
    except OSError as error:
        fail(entry["name"], f"download failed from {entry['url']}: {error}")
    with path.open("rb") as source:
        check(entry["name"], "download sha256", entry["sha256"], hashlib.file_digest(source, "sha256").hexdigest())
    return path


def extract(entry, dmg, folder):
    """Copy the bundle out of the dmg's pkg without running the installer or its scripts."""
    mount, expanded = folder / "mount", folder / "expanded"
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-quiet", "-mountpoint", mount, dmg],
                   check=True)
    try:
        packages = list(mount.glob("*.pkg"))
        if len(packages) != 1:
            fail(entry["name"], f"expected one .pkg in {dmg.name}, found {len(packages)}")
        subprocess.run(["pkgutil", "--expand-full", packages[0], expanded], check=True, stdout=subprocess.DEVNULL)
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", mount], check=True)
    found = list(expanded.glob(f"**/Payload/{entry['name']}.vst3"))
    if len(found) != 1:
        fail(entry["name"], f"expected one {entry['name']}.vst3 in {dmg.name}, found {len(found)}")
    return found[0]


def install(entry):
    target = PLUGINS / f"{entry['name']}.vst3"
    if target.exists():
        check(entry["name"], f"installed bundle sha256 (remove {target} to reinstall)", entry["bundle_sha256"],
              bundle_hash(target))
        return "kept"
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        bundle = extract(entry, download(entry, folder), folder)
        check(entry["name"], "bundle sha256", entry["bundle_sha256"], bundle_hash(bundle))
        PLUGINS.mkdir(exist_ok=True)
        staged = PLUGINS / f".{entry['name']}.vst3.partial"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(bundle, staged, symlinks=True)
        check(entry["name"], "copied bundle sha256", entry["bundle_sha256"], bundle_hash(staged))
        staged.rename(target)
    return "installed"


if __name__ == "__main__":
    for entry in tomllib.loads(LOCK.read_text())["plugin"]:
        print(f"{entry['name']} {entry['version']}: {install(entry)}", flush=True)
