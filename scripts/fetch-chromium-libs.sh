#!/bin/sh
# Optional, user-space only (no sudo, nothing system-wide): Playwright's Chromium needs libnss3/libnspr4.
# Downloads the distro .deb files and unpacks them into ~/.cache/riva-chromium-libs; the browser test
# fixtures add that directory to LD_LIBRARY_PATH automatically. Skip this if the system already has them.
set -eu
dest="${RIVA_CHROMIUM_LIBS_ROOT:-$HOME/.cache/riva-chromium-libs}"
mkdir -p "$dest/debs"
cd "$dest/debs"
apt-get download libnss3 libnspr4
for deb in ./*.deb; do dpkg -x "$deb" "$dest/extracted"; done
echo "Unpacked into $dest/extracted/usr/lib/x86_64-linux-gnu"
