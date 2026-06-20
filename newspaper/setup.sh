#!/usr/bin/env bash
# Install the system libraries the newspaper engine needs, then the Python deps.
# Pango/Cairo are required for proper justified, hyphenated column text; Pillow
# alone cannot do this well enough.
#
# Tested on Debian/Ubuntu. On macOS use: brew install pango cairo pkg-config
# gobject-introspection, then `pip install -r requirements.txt`.
set -e

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y \
    libcairo2 libcairo2-dev \
    libpango-1.0-0 libpangocairo-1.0-0 libpango1.0-dev \
    libgirepository1.0-dev gir1.2-pango-1.0 \
    libffi-dev pkg-config python3-dev
fi

pip install -r requirements.txt

echo "Done. The Google Fonts in ../fonts are registered at runtime, no system"
echo "font install needed. Test with:"
echo "  python make_test_template.py --output template.png"
echo "  python newspaper.py --template template.png --data mmc.json --output mmc_final.png"
