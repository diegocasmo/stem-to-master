"""Install plugins.lock into plugins/: uv run tools/plugins.py

A bundle already in plugins/ with the locked hash is kept. Anything else (failed download, wrong download or bundle
hash) stops with the plugin's name, so a run never continues with a plugin other than the locked one.
"""
import hashlib
import http.client
import re
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
    path = folder / hashlib.sha256(entry["url"].encode()).hexdigest()[:16]
    # Several bundles can come from one vendor archive: it is fetched once per run, but every entry checks its hash.
    if not path.exists():
        part = path.with_suffix(".part")
        try:
            # Some vendors refuse Python's default user agent.
            request = urllib.request.Request(entry["url"], headers={"User-Agent": "curl/8.7.1"})
            with urllib.request.urlopen(request, timeout=300) as response, part.open("wb") as out:
                shutil.copyfileobj(response, out)
        except (OSError, http.client.HTTPException) as error:
            fail(entry["name"], f"download failed from {entry['url']} ({error!r}), expected sha256 {entry['sha256']}, actual none")
        part.rename(path)
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


def kind(path):
    # By content, not name: a vendor link can save a zip as "...dl.php".
    with path.open("rb") as source:
        head = source.read(4)
    return {b"PK\x03\x04": "zip", b"xar!": "pkg"}.get(head, "dmg")


def unpack(entry, path, out):
    """One zip, dmg or pkg into out, without running an installer or its scripts."""
    found = kind(path)
    if found == "pkg":
        run(entry, "pkgutil", "--expand-full", path, out)
        return
    out.mkdir(parents=True)
    if found == "zip":
        run(entry, "ditto", "-x", "-k", path, out)
        return
    mount = out.with_name(out.name + ".mount")
    run(entry, "hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-quiet", "-mountpoint", mount, path)
    try:
        run(entry, "ditto", mount, out)
    finally:
        run(entry, "hdiutil", "detach", "-quiet", mount)


def loose(path, root):
    bundles = (".vst3", ".vst", ".app", ".component", ".aaxplugin")
    return not any(part.endswith(bundles) for part in path.relative_to(root).parts[:-1])


def extract(entry, archive, folder):
    """Unpack archives inside one another until the named bundle turns up; every copy of it must be the same."""
    root = folder / "unpacked"
    unpack(entry, archive, root)
    done = set()
    while todo := [f for f in root.rglob("*") if f.suffix in (".zip", ".dmg", ".pkg") and f.is_file() and not f.is_symlink()
                   and f not in done and loose(f, root)]:
        for f in todo:
            done.add(f)
            unpack(entry, f, f.with_name(f.name + ".x"))
    found = [b for b in root.rglob(f"{entry['name']}.vst3") if b.is_dir() and not b.is_symlink() and loose(b, root)]
    # Some packages install their payload as the bundle itself; only PackageInfo's install-location names it.
    for info in root.rglob("PackageInfo"):
        if re.search(rf'install-location="[^"]*/{re.escape(entry["name"])}\.vst3/?"', info.read_text(errors="ignore")):
            found.append(info.with_name("Payload"))
    if not found:
        fail(entry["name"], f"no {entry['name']}.vst3 in {archive.name}")
    if len({bundle_hash(b) for b in found}) != 1:
        fail(entry["name"], f"{len(found)} different {entry['name']}.vst3 bundles in {archive.name}")
    return found[0]


def install(entry, downloads=None):
    target = PLUGINS / f"{entry['name']}.vst3"
    if target.exists():
        check(entry["name"], f"installed bundle sha256 (remove {target} to reinstall)", entry["bundle_sha256"],
              bundle_hash(target))
        return "kept"
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        bundle = extract(entry, download(entry, downloads or folder), folder)
        check(entry["name"], "bundle sha256", entry["bundle_sha256"], bundle_hash(bundle))
        PLUGINS.mkdir(exist_ok=True)
        staged = PLUGINS / f".{entry['name']}.vst3.partial"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(bundle, staged, symlinks=True)
        check(entry["name"], "copied bundle sha256", entry["bundle_sha256"], bundle_hash(staged))
        staged.rename(target)
    return "installed"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as downloads:
        for entry in tomllib.loads(LOCK.read_text())["plugin"]:
            print(f"{entry['name']} {entry['version']}: {install(entry, Path(downloads))}", flush=True)
