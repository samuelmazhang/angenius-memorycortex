"""Apply all known arcadia-side patches from connector/arcadia/patches/ to a
fresh `A:/Application/Arcadia-Agent/Program/arcadia-agent/` checkout.

The patches directory contains one `.md` file per patch, each with a
"### Before" and "### After" code block (in fenced ```python blocks).
This script parses each patch, extracts the diff, and applies it to the
target file using targeted string replacement.

Usage:
    /a/Application/Arcadia-Agent/Program/arcadia-agent/venv/Scripts/python.exe \\
        ".connector\\arcadia\\scripts\\apply_patches.py"

    # Dry-run (prints planned diffs, makes no changes):
    ... apply_patches.py --dry-run

    # Apply only one patch (filename match):
    ... apply_patches.py --only 04-agent_init-mem-config-fix

    # Idempotent re-runs are safe; already-applied patches are detected and
    # skipped with a "no-op" message.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ARCADIA_REPO = Path(r"A:/Application/Arcadia-Agent/Program/arcadia-agent")
PATCHES_DIR = Path(r".connector/arcadia/patches")
SOURCE_FILENAME_RE = re.compile(r"^(\d+)-(.+)\.md$")
PATCH_HEADER_RE = re.compile(
    r"^## What this fixes", re.M
)
BEFORE_AFTER_RE = re.compile(
    r"#{2,4}\s+\*?\*?Before\*?\*?\s*\(?[^`\n]*\)?\s*```python\n(?P<before>.*?)```\s*"
    r"#{2,4}\s+\*?\*?After\*?\*?\s*\(?[^`\n]*\)?\s*```python\n(?P<after>.*?)```",
    re.DOTALL,
)


def list_patches(only: str | None = None) -> list[Path]:
    if not PATCHES_DIR.is_dir():
        return []
    files = sorted(
        p for p in PATCHES_DIR.glob("*.md")
        if SOURCE_FILENAME_RE.match(p.name)
    )
    if only:
        files = [p for p in files if only in p.name]
    return files


def parse_patch(md_path: Path) -> list[tuple[str, str, str]] | None:
    """Parse a patch markdown file. Return a list of (target_file, before, after)
    tuples — one per `**Before**` / `**After**` block pair. Multiple pairs in
    one file are supported (e.g. patch 05 has two edits in the same file).

    Returns None if no target file or before/after blocks are found.
    """
    text = md_path.read_text(encoding="utf-8")
    # First markdown heading after "# Patch NN —" gives the patch title.
    title_m = re.search(r"^# Patch \d+\s*[—–-]\s*(.+?)$", text, re.M)
    title = title_m.group(1).strip() if title_m else md_path.stem

    # Target file — prefer a top-level "## Target file" or "## Source:",
    # then fall back to the first .py in backticks.
    tgt_m = re.search(r"## Target file\s*\n+\s*`?([\w/\\.\-]+\.py)`?", text)
    if not tgt_m:
        tgt_m = re.search(r"## Source:\s*`?([\w/\\.\-]+\.py)`?", text)
    if not tgt_m:
        first_py = re.search(r"`([\w/\\.\-]+\.py)`", text)
        if not first_py:
            return None
        default_target = first_py.group(1)
    else:
        default_target = tgt_m.group(1).strip()

    # Each block opens with either:
    #   - a markdown heading `### Before` / `### After`, or
    #   - an inline-bold opener like `**Before** (around line 1316):` /
    #     `**After**:` (no heading — bold text acts as the marker).
    pair_re = re.compile(
        r"(?:^#{2,4}[^\n]*\*?\*?Before\*?\*?[^\n]*"
        r"|^\*\*Before\*\*[^\n]*)\n+\s*```python\n(?P<before>.*?)```\s*"
        r"(?:^#{2,4}[^\n]*\*?\*?After\*?\*?[^\n]*"
        r"|^\*\*After\*\*[^\n]*)\n+\s*```python\n(?P<after>.*?)```",
        re.MULTILINE | re.DOTALL,
    )

    # Per-block target: look at the most recent `### ... <file>.py` heading
    # that precedes the block; fall back to default_target.
    blocks: list[tuple[str, str, str]] = []
    for m in pair_re.finditer(text):
        before_text = m.group("before")
        after_text = m.group("after")
        block_start = m.start()
        # find most recent heading that names a .py file before block_start
        block_target = default_target
        for hm in re.finditer(
            r"^#{2,4}\s+([^\n]*?\.py)[^\n]*$",
            text[:block_start],
            re.M,
        ):
            block_target = hm.group(1).strip()
        blocks.append((block_target, before_text, after_text))

    return blocks if blocks else None


def already_applied(target_path: Path, before: str, after: str) -> str:
    """Return one of: 'after', 'before', 'neither', 'partial'."""
    try:
        text = target_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "neither"
    # Exact-substring match first (cheapest, most precise).
    if _norm(after) in _norm(text):
        return "after"
    if _norm(before) in _norm(text):
        return "before"
    # Fall back to per-line overlap so trailing-whitespace-only diffs still match.
    text_lines = {l.rstrip() for l in text.splitlines() if l.strip()}
    if _lines_present(text_lines, after):
        return "after"
    if _lines_present(text_lines, before):
        return "before"
    if _partial_overlap(text, before) or _partial_overlap(text, after):
        return "partial"
    return "neither"


def _norm(s: str) -> str:
    """Collapse whitespace runs to single spaces, drop empty lines.

    Two patches differ only in indentation or trailing whitespace — normalizing
    makes the substring match robust to those cases.
    """
    return "\n".join(" ".join(l.split()) for l in s.splitlines() if l.strip())


def _lines_present(haystack: set[str], needle: str) -> bool:
    """True if all non-empty stripped lines of needle are in haystack."""
    needle_lines = [l.rstrip() for l in needle.splitlines() if l.strip()]
    if len(needle_lines) < 2:
        return False
    return all(l in haystack for l in needle_lines)


def _partial_overlap(haystack: str, needle: str) -> bool:
    """True if ≥30% of needle's lines appear in haystack (rough heuristic)."""
    n_lines = [l for l in needle.splitlines() if l.strip()]
    if len(n_lines) < 4:
        return False
    matches = sum(1 for l in n_lines if l.strip() in haystack)
    return matches / len(n_lines) >= 0.3


def apply_one(md_path: Path, dry_run: bool) -> list[str]:
    parsed = parse_patch(md_path)
    if not parsed:
        return [f"SKIP  {md_path.name}: cannot parse patch (no target file or before/after blocks)"]
    results = []
    for idx, (target_rel, before, after) in enumerate(parsed, start=1):
        suffix = "" if len(parsed) == 1 else f" (block {idx}/{len(parsed)})"
        target_path = ARCADIA_REPO / target_rel
        state = already_applied(target_path, before, after)
        if state == "after":
            results.append(f"OK    {md_path.name}{suffix}: already applied ({target_rel})")
        elif state == "before":
            if dry_run:
                preview = after[:200].replace("\n", " ")
                results.append(f"APPLY {md_path.name}{suffix}: would patch {target_rel} (after-preview: {preview}…)")
            else:
                if not _normalize_aware_replace(target_path, before, after):
                    results.append(
                        f"FAIL  {md_path.name}{suffix}: normalize-aware replace failed for {target_rel} — "
                        f"the file may have diverged. Apply by hand."
                    )
                else:
                    results.append(f"APPLIED {md_path.name}{suffix}: {target_rel}")
        elif state == "partial":
            results.append(
                f"WARN  {md_path.name}{suffix}: {target_rel} contains a partial overlap with "
                f"the patch — refusing to apply. Inspect the file manually."
            )
        else:
            results.append(
                f"FAIL  {md_path.name}{suffix}: {target_rel} does not contain the 'Before' block — "
                f"the file has likely diverged. Apply the patch by hand."
            )
    return results


def _normalize_aware_replace(target_path: Path, before: str, after: str) -> bool:
    """Replace `before` with `after` in target_path even when their raw
    whitespace/indentation differs from the markdown.

    Strategy: walk the target file line-by-line, treating runs of horizontal
    whitespace as a single token. Find the smallest window whose normalized
    line sequence equals the normalized `before` sequence, then replace
    *only that window's raw lines* with `after`.

    Returns True on success, False if no unambiguous window matches.
    """
    target_text = target_path.read_text(encoding="utf-8", errors="replace")
    target_lines = target_text.splitlines(keepends=True)

    # Normalize each line for comparison (collapse internal whitespace).
    norm = lambda s: " ".join(s.split())
    before_norm = [norm(l) for l in before.splitlines() if l.strip()]
    n = len(before_norm)
    if n == 0:
        return False

    matches = []
    for i in range(len(target_lines) - n + 1):
        window = [norm(target_lines[i + j].rstrip("\r\n")) for j in range(n)]
        if window == before_norm:
            matches.append(i)

    if len(matches) != 1:
        # 0 matches: caller already checked 'before'; an actual race shouldn't
        # happen. >1 matches: ambiguous — caller should reject.
        return False

    start = matches[0]
    end = start + n
    # Drop the old window; insert the new `after` block (with a single trailing
    # newline if the original window had one, otherwise no newline).
    old_window = target_lines[start:end]
    had_trailing_newline = old_window[-1].endswith("\n") if old_window else False
    after_block = after.splitlines()
    if had_trailing_newline:
        after_block = [l + "\n" for l in after_block] + [""]

    new_text = "".join(target_lines[:start]) + "".join(after_block) + "".join(target_lines[end:])
    target_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print planned actions, make no changes")
    ap.add_argument("--only", metavar="NAME", help="only run patches whose filename matches NAME substring")
    args = ap.parse_args(argv)

    patches = list_patches(only=args.only)
    if not patches:
        print(f"No patches found in {PATCHES_DIR}")
        return 1

    print(f"Found {len(patches)} patch(es):")
    for p in patches:
        print(f"  - {p.name}")
    print()

    results = []
    for p in patches:
        for r in apply_one(p, dry_run=args.dry_run):
            print(r)
            results.append(r)

    print()
    applied = sum(1 for r in results if r.startswith(("APPLIED", "APPLY", "OK")))
    failed = sum(1 for r in results if r.startswith("FAIL") or r.startswith("WARN"))
    skipped = sum(1 for r in results if r.startswith("SKIP"))
    print(f"Summary: {applied} ok, {failed} need attention, {skipped} skipped.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))