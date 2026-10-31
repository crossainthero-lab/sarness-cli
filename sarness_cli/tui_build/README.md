# Sarness terminal build recipe

The Sarness terminal (`sarness-tui`) is [OpenCode](https://github.com/anomalyco/opencode) (MIT licence,
Copyright (c) 2025 opencode) at the commit pinned in `UPSTREAM`, plus the patch series in `patches/`.
The upstream licence is installed next to the binary as `LICENSE-OpenCode`.

`sarness tui install` runs `build.sh`, which:

1. downloads Bun at the pinned version and verifies its SHA-256;
2. clones OpenCode at the pinned tag and checks that the commit matches;
3. applies `patches/*.patch` in order;
4. runs `bun install --frozen-lockfile` (upstream's lockfile) and compiles a single executable;
5. installs it at `~/.local/share/sarness-cli/tui/<sarness>-<upstream>-<patch hash>/sarness-tui` and points
   `current` at it, keeping the previous build as `previous` (rollback = repoint `current`).

The build tree is ~3.5 GB, under `~/.cache/sarness-cli/tui-build`, and is deleted afterwards unless
`--keep-build` is given. Linux x64 is built and tested. Linux arm64 uses the same recipe but is untested.
macOS and Windows are not supported by this recipe.

## Regenerating the patches

The patches are `git format-patch v2.0.32..sarness` from a checkout of the fork branch. The fork delta is kept
small: every Sarness change is marked with a `SARNESS:` comment. To move to a new upstream release, rebase the
branch onto the new tag, re-export the patches, update `UPSTREAM`, and re-run the upstream TUI tests.

**Which branch.** The series is `git format-patch v2.0.32..sarness-0.4.2` from `~/Documents/sarness-tui-src/agentguard`
(branch `sarness-0.4.2`). That branch is the shared `sarness` branch plus patches 0007 (Ctrl+C, written on branch
`sarness-ctrlc`) and 0009 to 0012. The shared `sarness` branch in `~/Documents/sarness-tui-src/opencode` does not have
them: regenerating from it would silently drop the Ctrl+C and compaction fixes (`tests/test_ctrl_c_tui.py` and
`tests/test_scenario_tui.py` fail if that happens). Bring `sarness-0.4.2` onto `sarness` (fast-forward is not possible:
0007 and 0008 were committed on different branches) before using `sarness` as the source again. 0001 to 0008 are kept
byte-identical to the 0.4.1 release; to add a patch, `git format-patch --stdout -1 <commit>` into a new numbered file.
