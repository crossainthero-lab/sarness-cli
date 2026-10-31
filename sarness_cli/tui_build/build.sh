#!/usr/bin/env bash
# Build the Sarness terminal from the pinned OpenCode commit + patches/ and install it next to earlier builds.
# Inputs (set by `sarness tui install`): SARNESS_VERSION, SARNESS_TUI_INSTALL_ROOT, SARNESS_TUI_BUILD_ROOT,
# SARNESS_TUI_KEEP_BUILD (1 keeps the ~3.5 GB build tree). Nothing outside those two roots is written.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source <(grep -E '^[A-Z_a-z0-9]+=' "$here/UPSTREAM")
install_root="${SARNESS_TUI_INSTALL_ROOT:?}"
build_root="${SARNESS_TUI_BUILD_ROOT:?}"
version="${SARNESS_VERSION:?}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) bun_arch=x64; bun_sha="$BUN_SHA256_linux_x64" ;;
  Linux-aarch64 | Linux-arm64) bun_arch=aarch64; bun_sha="$BUN_SHA256_linux_aarch64" ;;
  *) echo "The Sarness terminal is only built and tested on Linux x64/arm64 (this is $(uname -sm))." >&2; exit 2 ;;
esac
for tool in git curl unzip sha256sum; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done

mkdir -p "$build_root"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Bun $BUN_VERSION (pinned, checksum-verified)"
bun_dir="$build_root/bun-$BUN_VERSION-$bun_arch"
if [ ! -x "$bun_dir/bun" ]; then
  zip="$build_root/bun-$BUN_VERSION-$bun_arch.zip"
  curl -fL --retry 3 --progress-bar -o "$zip" "https://github.com/oven-sh/bun/releases/download/bun-v$BUN_VERSION/bun-linux-$bun_arch.zip"
  echo "$bun_sha  $zip" | sha256sum -c -
  rm -rf "$bun_dir" "$build_root/bun-linux-$bun_arch"
  unzip -q -o "$zip" -d "$build_root"
  mv "$build_root/bun-linux-$bun_arch" "$bun_dir"
  rm -f "$zip"
fi
export PATH="$bun_dir:$PATH"

step "OpenCode $OPENCODE_TAG ($OPENCODE_COMMIT)"
src="$build_root/opencode"
if [ ! -d "$src/.git" ]; then
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$OPENCODE_TAG" "$OPENCODE_REPO" "$src"
fi
git -C "$src" fetch --quiet --depth 1 origin "refs/tags/$OPENCODE_TAG:refs/tags/$OPENCODE_TAG" 2>/dev/null || true
git -C "$src" -c advice.detachedHead=false checkout --quiet --detach "$OPENCODE_COMMIT"
git -C "$src" reset --quiet --hard "$OPENCODE_COMMIT"
git -C "$src" clean --quiet -fdx -e node_modules -e '**/node_modules'
[ "$(git -C "$src" rev-parse HEAD)" = "$OPENCODE_COMMIT" ] || { echo "upstream commit mismatch" >&2; exit 1; }

step "Applying the Sarness patch series"
for patch in "$here"/patches/*.patch; do
  echo "  $(basename "$patch")"
  git -C "$src" apply --whitespace=nowarn "$patch"
done

step "Installing dependencies (bun install --frozen-lockfile)"
(cd "$src" && bun install --frozen-lockfile >/dev/null)

step "Compiling sarness-tui"
(cd "$src/packages/cli" && OPENCODE_CHANNEL=sarness OPENCODE_VERSION="${OPENCODE_TAG#v}" \
  bun run script/build.ts --single --skip-web-ui --skip-install >/dev/null)
built="$(ls -d "$src"/packages/cli/dist/cli-linux-*/bin/opencode | head -1)"
"$built" --version | grep -q '^sarness-tui ' || { echo "the built binary does not identify as sarness-tui" >&2; exit 1; }

step "Installing"
patch_id="$(cat "$here"/patches/*.patch | sha256sum | cut -c1-12)"
dest="$install_root/$version-${OPENCODE_TAG#v}-$patch_id"
mkdir -p "$dest"
install -m 0755 "$built" "$dest/sarness-tui.new" && mv -f "$dest/sarness-tui.new" "$dest/sarness-tui"
cp "$src/LICENSE" "$dest/LICENSE-OpenCode"
cp "$here/UPSTREAM" "$dest/UPSTREAM"
# Rebuilding the same build must not overwrite the rollback target with itself.
if [ -L "$install_root/current" ] && [ "$(readlink "$install_root/current")" != "$(basename "$dest")" ]; then
  ln -sfn "$(readlink "$install_root/current")" "$install_root/previous"
fi
ln -sfn "$(basename "$dest")" "$install_root/current"
echo "  $dest/sarness-tui"
echo "  $("$dest/sarness-tui" --version)"
[ -L "$install_root/previous" ] && echo "  rollback: ln -sfn $(readlink "$install_root/previous") $install_root/current"

if [ "${SARNESS_TUI_KEEP_BUILD:-0}" != "1" ]; then
  step "Removing the build tree (use --keep-build to keep it for faster rebuilds)"
  rm -rf -- "$src"
fi
echo
echo "Done. Run 'sarness' in a project."
