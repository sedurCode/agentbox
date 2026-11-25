#!/usr/bin/env bash
# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Attach SHA256SUMS to a published release: the checksums of GitHub's tarball and zip for the tag, which the
# one-line installers (install.sh / install.ps1 run without a checkout) verify before installing.
#   tools/release-checksums.sh v0.1.1
set -euo pipefail
tag="${1:?usage: tools/release-checksums.sh vX.Y.Z}"
repo="${AGENTBOX_REPO:-sedurCode/agentbox}"
name="agentbox-${tag#v}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for ext in tar.gz zip; do
  curl -fsSL "https://github.com/$repo/archive/refs/tags/$tag.$ext" -o "$tmp/$name.$ext"
done
(cd "$tmp" && shasum -a 256 "$name.tar.gz" "$name.zip" > SHA256SUMS && cat SHA256SUMS)
gh release upload "$tag" "$tmp/SHA256SUMS" --repo "$repo" --clobber
