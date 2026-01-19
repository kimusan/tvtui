#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <version> [--test]"
  exit 1
fi

VERSION="$1"
REPO="pypi"
if [[ "${2:-}" == "--test" ]]; then
  REPO="testpypi"
fi

python3 - <<PY
import re
from datetime import date
from pathlib import Path

version = "${VERSION}"
today = date.today().isoformat()

pyproject = Path("pyproject.toml")
data = pyproject.read_text(encoding="utf-8")
data, count = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', data)
if count != 1:
    raise SystemExit("Failed to update version in pyproject.toml")
pyproject.write_text(data, encoding="utf-8")

app = Path("tvtui.py")
data = app.read_text(encoding="utf-8")
data, count = re.subn(r'(?m)^VERSION = "[^"]+"$', f'VERSION = "{version}"', data)
if count != 1:
    raise SystemExit("Failed to update VERSION in tvtui.py")
app.write_text(data, encoding="utf-8")

changelog = Path("CHANGELOG.md")
if changelog.exists():
    text = changelog.read_text(encoding="utf-8")
    header = "# Changelog\n\n"
    if not text.startswith(header):
        raise SystemExit("CHANGELOG.md must start with '# Changelog'")
    new_entry = f"## {version} - {today}\n\n- TBD\n\n"
    text = header + new_entry + text[len(header):]
    changelog.write_text(text, encoding="utf-8")
PY

python3 -m pip install --upgrade build twine --break-system-packages
rm -rf dist build tvtui.egg-info
python3 -m build
python3 -m twine upload --repository "${REPO}" dist/*

git tag "v${VERSION}"
echo "Release tagged as v${VERSION}. Remember to push: git push && git push --tags"
