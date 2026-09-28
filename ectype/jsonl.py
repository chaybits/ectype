"""One JSONL reader for every store that is a line-per-record file.

Five adapters and the converter each had their own loop, and each chose its own encoding and error
policy; one read strict `utf-8`, so a BOM made it drop the first record without a word. This is the
one place that decides: `utf-8-sig` (a Windows-written file may carry a BOM), blank lines skipped, an
unparseable line, or one that is valid JSON but not an object, COUNTED rather than passed over, so a
caller can say "n lines could not be read"
instead of presenting a shorter session as if it were whole.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def iter_jsonl(path: Path, skipped: list[int] | None = None) -> Iterator[tuple[int, Any]]:
    """Yield `(line_number, record)` for every non-blank line that parses; line numbers are 1-based.

    Args:
        path: the JSONL file.
        skipped: when given, the 1-based numbers of the lines that failed to parse are appended.
    Raises:
        OSError: the file cannot be opened.
        ValueError: the file is not valid UTF-8 (the message names the file and the line).
    """
    with open(path, encoding="utf-8-sig") as fh:
        i = 0
        try:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    if skipped is not None:
                        skipped.append(i)
                    continue
                if not isinstance(rec, dict):
                    # valid JSON that is not a record (a bare number, string or list): every caller
                    # calls .get on what comes out of here, so it counts as a line that could not be read
                    if skipped is not None:
                        skipped.append(i)
                    continue
                yield i, rec
        except UnicodeDecodeError as e:
            raise ValueError(f"{path}: not valid UTF-8 near line {i + 1}: {e}") from e


def read_jsonl(path: Path) -> tuple[list[Any], list[int]]:
    """Every record of the file, plus the 1-based numbers of the lines that could not be parsed."""
    bad: list[int] = []
    return [r for _, r in iter_jsonl(path, bad)], bad
