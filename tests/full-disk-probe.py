#!/usr/bin/python3
"""Exercise the real Harvester on a small, disposable Linux tmpfs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--expect-damaged", action="store_true")
parser.add_argument("--root", type=Path)
args = parser.parse_args()
repo = args.repo.resolve()
uuid = "sound@cinnamon.org"


def snapshot(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


if args.root is None:
    with tempfile.TemporaryDirectory(prefix="harvester-full-disk-") as directory:
        root = Path(directory)
        data = root / "data"
        data.mkdir()
        schemas = root / "schemas"
        schemas.mkdir()
        shutil.copy(repo / "data/org.cinnamon.gschema.xml", schemas)
        subprocess.run(["glib-compile-schemas", "--strict", str(schemas)], check=True)
        env = dict(os.environ, GSETTINGS_BACKEND="memory", GSETTINGS_SCHEMA_DIR=str(schemas))
        for name in ("DATA", "CONFIG", "CACHE", "STATE"):
            env[f"XDG_{name}_HOME"] = str(root / name.lower())
        subprocess.run(["sudo", "mount", "-t", "tmpfs", "-o",
                        f"size=1M,uid={os.getuid()},gid={os.getgid()}",
                        "tmpfs", str(data)], check=True)
        try:
            command = [sys.executable, __file__, "--repo", str(repo), "--root", str(root)]
            if args.expect_damaged:
                command.append("--expect-damaged")
            result = subprocess.run(command, env=env)
        finally:
            subprocess.run(["sudo", "umount", str(data)], check=True)
        sys.exit(result.returncode)

from PIL import Image
sys.path.insert(0, str(repo / "python3"))
from cinnamon.harvester import Harvester

root = args.root
source = root / "source" / uuid
shutil.copytree(repo / "files/usr/share/cinnamon/applets" / uuid, source)
harvester = Harvester("applet")
assert Path(harvester.install_folder).is_relative_to(root / "data")
harvester.install_from_folder(str(source), uuid)
destination = Path(harvester.install_folder) / uuid
before = snapshot(destination)
print(f"Installed stock {uuid}: {len(before)} files", flush=True)

# Model an update which adds an image, so deleting the old version cannot make it fit.
pixels = random.Random(0).randbytes(512 * 512 * 3)
Image.frombytes("RGB", (512, 512), pixels).save(source / "new-icon.png")
metadata = json.loads((source / "metadata.json").read_text())
metadata["description"] += " (updated)"
(source / "metadata.json").write_text(json.dumps(metadata))
space = os.statvfs(root / "data")
free = space.f_bavail * space.f_frsize
(root / "data" / "filler").write_bytes(b"x" * (free - 8192))
print(f"Free space before update: {shutil.disk_usage(root / 'data').free} bytes", flush=True)

try:
    harvester.install_from_folder(str(source), uuid)
except OSError as error:
    assert "No space left on device" in str(error), str(error)
    print(f"Update rejected by the filesystem: {error}", flush=True)
else:
    raise AssertionError("Update unexpectedly succeeded on the full filesystem")

after = snapshot(destination)
changed = sorted(name for name, digest in before.items() if after.get(name) != digest)
print(f"Changed or lost original files: {changed}", flush=True)
if args.expect_damaged:
    assert changed, "The baseline did not reproduce damage to the installed applet"
    print("Confirmed: upstream damages the installed applet after ENOSPC", flush=True)
else:
    assert after == before, "The failed update changed the installed applet"
    assert sorted(p.name for p in destination.parent.iterdir()) == [uuid]
    print("Confirmed: the fix preserves every installed file after ENOSPC", flush=True)
