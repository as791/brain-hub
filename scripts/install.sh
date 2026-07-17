#!/bin/sh
set -eu

REPOSITORY="as791/brain-hub"
REF="${BRAINHUB_REF:-main}"

case "$REF" in
  ""|/*|*..*|*[!A-Za-z0-9._/-]*)
    echo "Invalid BRAINHUB_REF: $REF" >&2
    exit 2
    ;;
esac

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
        >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  echo "Brain Hub requires Python 3.11 or newer." >&2
  echo "macOS: brew install python@3.12" >&2
  echo "Ubuntu/Debian: sudo apt install python3 python3-venv" >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)"
LOCAL_INSTALLER="$SCRIPT_DIR/install.py"
LOCAL_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." 2>/dev/null && pwd || true)"

if [ -f "$LOCAL_INSTALLER" ] && [ -f "$LOCAL_ROOT/pyproject.toml" ]; then
  exec "$PYTHON" "$LOCAL_INSTALLER" --source "$LOCAL_ROOT" "$@"
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/brainhub-install.XXXXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
INSTALLER="$TEMP_DIR/install.py"
INSTALLER_URL="https://raw.githubusercontent.com/$REPOSITORY/$REF/scripts/install.py"

"$PYTHON" - "$INSTALLER_URL" "$INSTALLER" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

url, destination = sys.argv[1:]
request = Request(url, headers={"User-Agent": "brain-hub-installer"})
with urlopen(request, timeout=30) as response:
    final = urlparse(response.geturl())
    if final.scheme != "https" or final.hostname != "raw.githubusercontent.com":
        raise SystemExit(f"refusing unexpected installer redirect: {response.geturl()}")
    payload = response.read(1_000_001)
if len(payload) > 1_000_000:
    raise SystemExit("installer download exceeded 1 MB")
Path(destination).write_bytes(payload)
PY

"$PYTHON" "$INSTALLER" \
  --source "https://github.com/$REPOSITORY" \
  --ref "$REF" \
  "$@"
