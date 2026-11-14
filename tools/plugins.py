"""Install plugins.lock into plugins/: uv run tools/plugins.py

A bundle already in plugins/ with the locked hash is kept. Anything else (failed download, wrong download or bundle
hash) stops with the plugin's name, so a run never continues with a plugin other than the locked one.
"""
import hashlib
import http.client
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "plugins.lock"
PLUGINS = ROOT / "plugins"


def bundle_hash(bundle):
    """The installed .vst3: every file's relative path, size and bytes, in path order."""
    digest = hashlib.sha256()
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


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
    except (OSError, http.client.HTTPException) as error:
        fail(entry["name"], f"download failed from {entry['url']} ({error!r}), expected sha256 {entry['sha256']}, actual none")
    with path.open("rb") as source:
        check(entry["name"], "download sha256", entry["sha256"], hashlib.file_digest(source, "sha256").hexdigest())
    return path


def run(entry, *command):
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as error:
        fail(entry["name"], f"{command[0]} {command[1]} failed with exit {error.returncode} {error.stderr.strip()}".strip())
    except OSError as error:
        fail(entry["name"], f"{command[0]} {command[1]} failed ({error})")


def extract(entry, dmg, folder):
    """Copy the bundle out of the dmg's pkg without running the installer or its scripts."""
    mount, expanded = folder / "mount", folder / "expanded"
    run(entry, "hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-quiet", "-mountpoint", mount, dmg)
    try:
        packages = list(mount.glob("*.pkg"))
        if len(packages) != 1:
            fail(entry["name"], f"expected one .pkg in {dmg.name}, found {len(packages)}")
        run(entry, "pkgutil", "--expand-full", packages[0], expanded)
    finally:
        run(entry, "hdiutil", "detach", "-quiet", mount)
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
