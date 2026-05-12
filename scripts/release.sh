#!/usr/bin/env bash
# scripts/release.sh — cut a new release of whisper-hotkeyd.
#
# Usage:   ./scripts/release.sh <version>      e.g. ./scripts/release.sh 5.2.0
#
# What it does, in order:
#   1. Bump version in whisper_hotkeyd/__init__.py and pyproject.toml,
#      commit "Release vX.Y.Z", tag vX.Y.Z, push the branch and the tag.
#   2. Download the auto-generated GitHub release tarball and compute its
#      sha256.
#   3. Update PKGBUILD (pkgver and sha256sums) and commit "PKGBUILD: bump
#      to vX.Y.Z", push.
#   4. Create a GitHub Release with auto-generated notes.
#   5. Generate .SRCINFO from the new PKGBUILD and push PKGBUILD + .SRCINFO
#      to the AUR repo (master branch).
#
# Requirements: gh (logged in), git push rights to GitHub, SSH key registered
# with AUR. Assumes the AUR package is already initialized (this script does
# not handle the first-ever upload).

set -euo pipefail

GITHUB_REPO="Sinoosoida/whisper-hotkeyd"
AUR_URL="ssh://aur@aur.archlinux.org/whisper-hotkeyd.git"

usage() {
    echo "Usage: $0 <version>      # e.g. $0 5.2.0" >&2
    exit 1
}

VERSION="${1:-}"
[[ -z "$VERSION" ]] && usage
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "ERROR: version must look like 1.2.3" >&2
    usage
}

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Pre-flight checks"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree is dirty. Commit or stash first." >&2
    git status --short >&2
    exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "main" ]]; then
    echo "ERROR: not on 'main' (currently on '$BRANCH')" >&2
    exit 1
fi

if git rev-parse "v$VERSION" >/dev/null 2>&1; then
    echo "ERROR: tag v$VERSION already exists" >&2
    exit 1
fi

for cmd in gh makepkg curl sha256sum sed; do
    command -v "$cmd" >/dev/null || {
        echo "ERROR: '$cmd' not found in PATH" >&2
        exit 1
    }
done

TARBALL_URL="https://github.com/${GITHUB_REPO}/archive/refs/tags/v${VERSION}.tar.gz"

echo "==> Bumping version to $VERSION"
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" \
    whisper_hotkeyd/__init__.py
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml

git add whisper_hotkeyd/__init__.py pyproject.toml
git commit -m "Release v$VERSION"
git tag -a "v$VERSION" -m "v$VERSION"
git push origin main
git push origin "v$VERSION"

echo "==> Downloading release tarball to compute sha256"
TMP_TARBALL="$(mktemp --suffix=.tar.gz)"
trap 'rm -f "$TMP_TARBALL"' EXIT

for attempt in 1 2 3 4 5; do
    if curl -fsSL "$TARBALL_URL" -o "$TMP_TARBALL" && [[ -s "$TMP_TARBALL" ]]; then
        break
    fi
    echo "  tarball not ready yet (attempt $attempt), retrying in 3s..."
    sleep 3
done

if [[ ! -s "$TMP_TARBALL" ]]; then
    echo "ERROR: could not download $TARBALL_URL" >&2
    exit 1
fi

NEW_SHA256="$(sha256sum "$TMP_TARBALL" | cut -d' ' -f1)"
echo "  sha256 = $NEW_SHA256"

echo "==> Updating PKGBUILD"
sed -i "s/^pkgver=.*/pkgver=$VERSION/" PKGBUILD
sed -i "s/^sha256sums=.*/sha256sums=('$NEW_SHA256')/" PKGBUILD

git add PKGBUILD
git commit -m "PKGBUILD: bump to v$VERSION"
git push origin main

echo "==> Creating GitHub Release with auto-generated notes"
gh release create "v$VERSION" \
    --title "v$VERSION" \
    --generate-notes

echo "==> Generating .SRCINFO"
makepkg --printsrcinfo > .SRCINFO

echo "==> Syncing to AUR"
AUR_DIR="$(mktemp -d)"
git clone "$AUR_URL" "$AUR_DIR"
cp PKGBUILD .SRCINFO "$AUR_DIR/"
(
    cd "$AUR_DIR"
    git add PKGBUILD .SRCINFO
    git commit -m "Update to $VERSION"
    git push origin master
)
rm -rf "$AUR_DIR" .SRCINFO

cat <<EOF

============================================================
Released v$VERSION successfully:
  GitHub release: https://github.com/${GITHUB_REPO}/releases/tag/v$VERSION
  AUR package:    https://aur.archlinux.org/packages/whisper-hotkeyd

Test the AUR install path on a clean machine:
  yay -S whisper-hotkeyd
============================================================
EOF
