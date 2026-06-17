#!/usr/bin/env bash
#
# view-docs.sh - generate the HTML reference manuals for the bundled example ISAs
# (pico32 and npu-probe) and serve them on localhost so you can browse them.
#
# Usage:
#   bash examples/view-docs.sh [PORT]        # default port 8000
#   PORT=9000 bash examples/view-docs.sh
#
# Stop the server with Ctrl-C.

set -euo pipefail

# Repo root = the parent of this script's examples/ directory.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="build/manuals"
PORT="${1:-${PORT:-8000}}"

# name : path-to-isa.yaml
ISAS=(
  "pico32:examples/tutorial/pico32-part4/isa.yaml"
  "npu-probe:examples/npu-probe/isa.yaml"
)

mkdir -p "$OUT"
echo "Generating HTML reference manuals into $OUT/ ..."
for entry in "${ISAS[@]}"; do
  name="${entry%%:*}"
  isa="${entry#*:}"
  echo "  - $name  ($isa)"
  uv run isa-archive generate -i "$isa" -t docs-html -o "$OUT"
done

# A small index page linking each generated manual.
{
  echo "<!doctype html><html><head><meta charset=\"utf-8\">"
  echo "<title>ISA-Archive reference manuals</title>"
  echo "<style>body{font:16px system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}"
  echo "li{margin:.4rem 0}</style></head><body>"
  echo "<h1>ISA-Archive reference manuals</h1><ul>"
  for entry in "${ISAS[@]}"; do
    name="${entry%%:*}"
    echo "<li><a href=\"${name}_reference.html\">${name}</a></li>"
  done
  echo "</ul></body></html>"
} > "$OUT/index.html"

URL="http://localhost:${PORT}/"
echo
echo "Serving $OUT/ at $URL  (Ctrl-C to stop)"

# Best-effort: open a browser once the server is up (ignored if unavailable / headless).
( sleep 1
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi ) >/dev/null 2>&1 &

exec uv run python -m http.server "$PORT" --directory "$OUT"
