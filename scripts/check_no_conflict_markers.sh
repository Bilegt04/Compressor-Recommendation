#!/usr/bin/env bash
# Conflict-marker scanner. Exits non-zero if any tracked file contains an
# unresolved git merge marker. Run locally before commit, in CI, or in the
# Docker build. Adding this to the build pipeline would have caught the
# exact failure that broke the Render deploy.
#
# Usage:
#     scripts/check_no_conflict_markers.sh
#
# Exit codes:
#     0  clean
#     1  markers found (output lists files + line numbers)

set -u

# Strict pattern: marker chars at start of line, followed by space or EOL.
# This avoids false positives on CSS/JS comment dividers like '// ====...'.
PATTERN='^(<{7}|={7}|>{7})( |$)'

# Files to ignore: this script itself + binary data.
EXCLUDE_DIRS='--exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=node_modules --exclude-dir=data'

# shellcheck disable=SC2086
matches=$(grep -RnE "$PATTERN" $EXCLUDE_DIRS --binary-files=without-match . 2>/dev/null | grep -v 'check_no_conflict_markers.sh' || true)

if [ -n "$matches" ]; then
    echo "ERROR: unresolved git merge-conflict markers found:" >&2
    echo "$matches" >&2
    echo "" >&2
    echo "Resolve the conflicts and re-run before committing." >&2
    exit 1
fi

echo "No conflict markers found."
exit 0
