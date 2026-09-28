"""The Windows portable bundle: the invariants that break silently.

    python3 tests/test_packaging.py

Nothing here builds a bundle (that needs a download, and CI does it on every release). These are
the checks whose failure would otherwise surface only as a stranger's bug report: the launcher
that stopped being portable, the sys.path line that makes `-m ectype` resolve at all, and the
version the zip name is built from.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def read(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8")


def test_files_present() -> None:
    for name in ("build-portable.sh", "ectype.bat", "ectype-gui.bat", "README.txt"):
        check((PKG / name).is_file(), f"packaging/windows/{name} exists")


def test_launchers_are_portable() -> None:
    """Both .bat files must point ECTYPE_CONFIG inside the bundle.

    Drop that line and everything still runs, while settings quietly move to %APPDATA% and the
    folder stops being self-contained. Nothing user-visible says so.
    """
    for name in ("ectype.bat", "ectype-gui.bat"):
        t = read(name)
        check('set "ECTYPE_CONFIG=%~dp0user\\settings.json"' in t,
              f"{name} keeps settings inside the bundle")
        check(r'"%~dp0python\python.exe" -m ectype' in t,
              f"{name} runs the bundled interpreter, not one from PATH")


def test_launchers_survive_a_path_with_spaces() -> None:
    """Every %~dp0 expansion is quoted. An unquoted one breaks under C:\\Users\\Ada Lovelace\\."""
    for name in ("ectype.bat", "ectype-gui.bat"):
        bad = [ln.strip() for ln in read(name).splitlines()
               if "%~dp0" in ln and not ln.lstrip().lower().startswith("rem")
               and not re.search(r'"[^"]*%~dp0[^"]*"', ln)]
        check(not bad, f"{name} quotes every %~dp0 path ({bad or 'all quoted'})")


def test_build_appends_the_path_entry() -> None:
    """The one line without which the bundle builds fine and dies at run time.

    The embeddable distribution takes sys.path entirely from python<XY>._pth and ignores both
    PYTHONPATH and the working directory, so the package one level up is invisible until '..'
    is appended. Verified under wine: without it, 'No module named ectype'.
    """
    t = read("build-portable.sh")
    check(r"printf '..\r\n'" in t and '>> "$pth"' in t,
          "build-portable.sh appends '..' to python*._pth")
    check("python*._pth" in t, "it globs the _pth name rather than hardcoding a Python version")


def test_pinned_interpreter_is_coherent() -> None:
    t = read("build-portable.sh")
    ver = re.search(r'PY_VERSION_DEFAULT="([^"]+)"', t)
    sha = re.search(r'PY_SHA256_DEFAULT="([^"]+)"', t)
    check(bool(ver) and re.fullmatch(r"\d+\.\d+\.\d+", ver.group(1)) is not None,
          f"a pinned CPython version ({ver.group(1) if ver else 'missing'})")
    check(bool(sha) and re.fullmatch(r"[0-9a-f]{64}", sha.group(1)) is not None,
          "a pinned sha256 for the embeddable zip")
    check("sha256 mismatch" in t, "the build fails on a hash mismatch instead of warning")
    check("rm -f \"$EMBED_ZIP\"" in t, "a bad cached download is deleted, not left to be reused")


def test_zip_name_follows_the_package_version() -> None:
    """The archive name is derived from ectype/__init__.py, so it cannot disagree with its contents."""
    t = read("build-portable.sh")
    check("__init__.py" in t and "__version__" in t,
          "build-portable.sh reads the version out of the package")
    check('NAME="ectype-$VERSION"' in t, "the bundle folder carries that version")


def test_nothing_published_carries_an_em_dash() -> None:
    """A repo-wide rule, checked here too because packaging/ is easy to forget."""
    for p in sorted(PKG.iterdir()):
        if p.is_file():
            check("\u2014" not in p.read_text(encoding="utf-8"), f"no em dash in {p.name}")


def test_ci_runs_the_bundle_on_real_windows() -> None:
    """The build is allowed near a release only after a Windows runner has run it."""
    wf = (ROOT / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")
    check("windows-latest" in wf, "a windows-latest job exists")
    check("needs: portable" in wf, "it waits for the bundle to be built")
    i, j = wf.find("portable-windows:"), wf.find("gh release upload", wf.find("portable-windows:"))
    check(i != -1 and j > i, "the release upload sits inside the job that tested it")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        fn()
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else f"all packaging checks passed ({len(tests)} tests)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
