#!/usr/bin/env bash
#
# Build the Windows portable bundle: an embeddable CPython with the ectype package dropped in
# beside it, plus two .bat launchers. No installer, no pip, no venv, nothing written outside the
# folder. The same idea ComfyUI ships as its Windows release, and it is cheap here only because
# ectype has zero dependencies: there are no wheels to resolve, so the bundle is the interpreter
# plus 1.1 MB of source.
#
# Runs on Linux, macOS or a Windows runner's bash. It never needs Windows to build, because the
# embeddable distribution is just a zip of prebuilt files.
#
# Usage:
#   build-portable.sh [--python-version X.Y.Z] [--sha256 HASH] [--src DIR]
#                     [--out DIR] [--cache DIR] [--no-zip]
#
# Every default is derived from this script's own location, so a checkout anywhere works.
set -euo pipefail

# ── pinned inputs ────────────────────────────────────────────────────────────────────────────
# 3.13.7 rather than the newest 3.14: it is the current security-supported line, and a runtime we
# ship to strangers is the wrong place to be first. Bumping it is one flag, plus the new hash.
#
# The hash is the anchor. python.org also publishes python-<v>-embed-amd64.zip.sigstore and .asc
# next to the zip; verifying either needs extra tooling, so the pin below is what this script
# enforces. It was taken from two independent downloads of the same file.
PY_VERSION_DEFAULT="3.13.7"
PY_SHA256_DEFAULT="f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65"

PY_VERSION="$PY_VERSION_DEFAULT"
PY_SHA256="$PY_SHA256_DEFAULT"
MAKE_ZIP=1

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd -- "$here/../.." && pwd)"          # packaging/windows/ -> the project root
OUT=""
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ectype-build"

die() { printf 'build-portable: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --python-version) PY_VERSION="${2:?--python-version needs a value}"; shift 2 ;;
    --sha256)         PY_SHA256="${2:?--sha256 needs a value}";          shift 2 ;;
    --src)            SRC="${2:?--src needs a value}";                   shift 2 ;;
    --out)            OUT="${2:?--out needs a value}";                   shift 2 ;;
    --cache)          CACHE="${2:?--cache needs a value}";               shift 2 ;;
    --no-zip)         MAKE_ZIP=0; shift ;;
    -h|--help)        sed -n '3,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

# A pinned version with someone else's hash builds a bundle that passes its own check and is not
# the file anyone reviewed. Refuse rather than warn.
if [ "$PY_VERSION" != "$PY_VERSION_DEFAULT" ] && [ "$PY_SHA256" = "$PY_SHA256_DEFAULT" ]; then
  die "--python-version $PY_VERSION was given without a matching --sha256; the pinned hash belongs to $PY_VERSION_DEFAULT"
fi

PYTHON="${PYTHON:-}"                         # $PYTHON overrides; Git Bash on Windows has no python3
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi
command -v "$PYTHON" >/dev/null 2>&1 || die "no python interpreter found (set \$PYTHON)"

[ -d "$SRC/ectype" ] || die "no ectype package under --src ($SRC); pass --src <project root>"
[ -z "$OUT" ] && OUT="$SRC/dist"

# The version comes from the package, so the zip name can never disagree with what it contains.
VERSION="$("$PYTHON" -c "
import re, pathlib, sys
t = pathlib.Path(sys.argv[1], 'ectype', '__init__.py').read_text(encoding='utf-8')
m = re.search(r'__version__\s*=\s*[\"\x27]([^\"\x27]+)', t)
sys.exit('no __version__ in ectype/__init__.py') if not m else print(m.group(1))
" "$SRC")"

NAME="ectype-$VERSION"
BUNDLE="$OUT/$NAME"
ZIP="$OUT/$NAME-windows-x64.zip"
EMBED_ZIP="$CACHE/python-$PY_VERSION-embed-amd64.zip"
URL="https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip"

mkdir -p "$CACHE" "$OUT"

# ── 1. the interpreter, fetched once and verified every time ─────────────────────────────────
if [ ! -f "$EMBED_ZIP" ]; then
  echo "build-portable: fetching $URL" >&2
  curl -fsSL --retry 3 -o "$EMBED_ZIP.part" "$URL" || die "download failed: $URL"
  mv "$EMBED_ZIP.part" "$EMBED_ZIP"
fi

actual="$("$PYTHON" -c "
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
print(h.hexdigest())
" "$EMBED_ZIP")"
if [ "$actual" != "$PY_SHA256" ]; then
  rm -f "$EMBED_ZIP"                          # a bad cache entry must not survive to the next run
  die "sha256 mismatch for python-$PY_VERSION-embed-amd64.zip
     expected $PY_SHA256
     got      $actual
   the cached copy was deleted; re-run to fetch it again"
fi

# ── 2. lay the bundle out ────────────────────────────────────────────────────────────────────
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/python"
"$PYTHON" -m zipfile -e "$EMBED_ZIP" "$BUNDLE/python/"

# The embeddable distribution takes its whole sys.path from python<XY>._pth and ignores both
# PYTHONPATH and the working directory, so `-m ectype` cannot find a package one level up until
# ".." is on that list. Without this line the bundle builds fine and fails at run time with
# "No module named ectype", which is why it is not left to chance.
pth="$(ls "$BUNDLE"/python/python*._pth 2>/dev/null | head -1)"
[ -n "$pth" ] || die "no python*._pth in the embeddable zip; the layout changed upstream"
printf '..\r\n' >> "$pth"

cp -R "$SRC/ectype" "$BUNDLE/ectype"
find "$BUNDLE/ectype" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE/ectype" -name '*.py[co]' -delete 2>/dev/null || true

# cmd.exe is only reliable with CRLF, and a checkout on a Linux box has LF. Convert on copy so the
# bundle is correct whatever the working tree did, rather than depending on git's line-ending config.
for f in ectype.bat ectype-gui.bat README.txt; do
  [ -f "$here/$f" ] || die "missing packaging file: $here/$f"
  "$PYTHON" -c "
import pathlib, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
dst.write_bytes(src.read_text(encoding='utf-8').replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8'))
" "$here/$f" "$BUNDLE/$f"
done

# ── 3. archive ───────────────────────────────────────────────────────────────────────────────
if [ "$MAKE_ZIP" = "1" ]; then
  rm -f "$ZIP"
  "$PYTHON" -c "
import pathlib, sys, zipfile
root, out = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in sorted(root.rglob('*')):
        if p.is_file():
            z.write(p, pathlib.Path(root.name, p.relative_to(root)).as_posix())
" "$BUNDLE" "$ZIP"
fi

size() { "$PYTHON" -c "
import os, sys
n = os.path.getsize(sys.argv[1])
print(f'{n / 1048576:.1f} MB')
" "$1"; }

if [ "$MAKE_ZIP" = "1" ]; then
  echo "ectype $VERSION portable (CPython $PY_VERSION): $ZIP  ($(size "$ZIP"))"
else
  echo "ectype $VERSION portable (CPython $PY_VERSION): $BUNDLE  (folder only)"
fi
