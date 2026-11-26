#!/usr/bin/env python3
"""auto_bump_version.py - 每次 commit 前自动 bump patch version."""
import re, sys, os, subprocess, argparse

REPO = r"."
PATH = os.path.join(REPO, "server", "src", "api_pg.py")


def bump(part="patch"):
    text = open(PATH, encoding="utf-8").read()
    m = re.search(r'version="(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        print("ERROR: version not found in api_pg.py")
        sys.exit(1)
    major = int(m.group(1))
    minor = int(m.group(2))
    patch_n = int(m.group(3))
    if part == "patch":
        patch_n += 1
    elif part == "minor":
        minor += 1
        patch_n = 0
    elif part == "major":
        major += 1
        minor = 0
        patch_n = 0
    new = "%d.%d.%d" % (major, minor, patch_n)
    new_text = re.sub(r'version="\d+\.\d+\.\d+"', 'version="' + new + '"', text)
    new_text = re.sub(r"v\d+\.\d+\.\d+", "v" + new, new_text, count=1)
    if new_text == text:
        print("ERROR: no change after bump")
        sys.exit(1)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new_text)
    return new


def commit(new_version):
    msg = "bump version: " + new_version + " (auto via auto_bump_version.py)"
    subprocess.run(["git", "add", "server/src/api_pg.py"], cwd=REPO)
    r = subprocess.run(["git", "commit", "-m", msg], cwd=REPO,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("git commit FAIL: " + r.stderr)
        sys.exit(1)
    print("committed: " + r.stdout.strip())


if __name__ == "__main__":
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--major", action="store_true", help="major version bump")
    p.add_argument("--minor", action="store_true", help="minor version bump")
    p.add_argument("--patch", action="store_true", default=True, help="patch version bump (default)")
    p.add_argument("--no-commit", action="store_true",
                   help="bump version without creating git commit")
    args = p.parse_args()

    part = "patch"
    if args.major:
        part = "major"
    elif args.minor:
        part = "minor"

    new = bump(part)
    print("bumped to " + new + " (" + part + ")")

    if not args.no_commit:
        commit(new)

    print("")
    print("Next steps:")
    print("  1. git push origin main")
    print("  2. ssh user at 127.0.0.1 pull + docker restart (admin)")
    print("  3. curl 8.2:1880/health   # verify version=" + new)
