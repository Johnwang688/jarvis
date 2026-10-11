# Decisions: the HUD editor, tree-sitter colour and VEX (2026-10-09)

These are the owner's answers to the open decisions in `2026-10-09-hud-editor-plan.md` §4. **Where this file and the
plan disagree, this file wins.**

## Answered by the owner

| # | Decision | Answer |
|---|---|---|
| E-1 | Intellisense | **None.** In the owner's words: "claude code is my intellisense". There are no language servers, LSP client, server-driven Problems or `jarvis lsp`. Monaco's own editing features stay (plan §2.2). |
| E-6 | AI ghost text | **No.** |
| — | Hand-rolled analysis | **No.** There is no symbol index and no type engine. |
| E-4 | Editor theme | **Graphite only.** One editor colour scheme matching the HUD, with no picker and no theme import. No syntax colour may use amber or red. |
| E-5 | Highlighting | **Tree-sitter, the Neovim way** (plan §2.6), with Monarch as the fallback. |
| — | VS Code extensions | **No general extension support.** Instead the owner wants a native VEX feature: (a) build and download to the brain, (b) the brain terminal and device info, (c) new projects from VEX's templates. |
| E-11 | Vim keybindings | **Yes**, as an editor setting, if the spike shows `monaco-vim` works on Monaco 0.57. `:w` goes through the guarded save. |
| C-1 | HUD served from a protected copy | **Yes.** `jarvis hud install` copies a build to `~/.local/share/jarvis/hud/`, which agent file tools cannot write, and the daemon serves only that copy. The lead runs the install as part of each post-merge restart (see the `restart-daemon-after-merges` procedure). |
| V-1 / V-5 | How the HUD reaches the brain, and the C++ toolchain | **Windows tools through WSL interop.** That means the `vexcom.exe` and Windows toolchain the VS Code extension already downloaded, run in place from fixed configured paths. There is no usbipd, and nothing is copied, bundled or committed. |
| V-2 | Agents and VEX | **Build only.** Agents may compile through the `vexbuild` command, which asks like any command unless allowlisted. Downloading or running a program on the robot is never an agent action: there is no tool, and any agent command naming `vexcom` is refused. |

## Taken as recommended (the lead's default; the owner raised no objection)

- **E-2:** moot now that there are no language servers.
- **E-7:** every editor file route is HUD-only (`owner_only`), reads included.
- **E-8:** credential folders and Claude's and Codex's login files are withheld from the editor. Jarvis's gate-state files
  stay readable and read-only.
- **E-9:** auto-save off, format-on-save off.
- **E-10:** Ctrl+B folds the sidebar, except as the second key of a Ctrl+K chord.
- **V-3:** **no "run after download".** Starting a program stays something done at the brain or controller by someone
  who can see the robot.
- **V-4:** new VEX projects go in a Windows folder, default `C:\Users\johnw\Documents\Robotics\<name>`, because VEX's
  Windows toolchain builds only on a Windows drive. The owner's existing projects live under `OneDrive\Desktop`, so the
  folder is a setting.
- **Plan §4.3, defaulted items 1–12:** these stand as written. One of them adds a CSP with `'wasm-unsafe-eval'` and no
  `'unsafe-eval'`.
- **Plan reading of E-1:** no TypeScript, JSON, CSS or HTML language workers, so there are no JSON schema checks or TS
  squiggles. Ask the owner before adding any back.

## Notes for the builders

- **Merging.** The owner's standing OK to merge after review covers the HUD workspace packages (WP-A to WP-F) and PR #24.
  **Ask the owner before merging editor or VEX packages** unless that OK is extended.
  **Extended 2026-10-10:** the owner said to merge editor and VEX packages on review too, the same rule as the HUD
  work. Build started with ED-1 and VX-1 in parallel.
- **The C-1 restart procedure.** Once C-1 lands, the HUD is live only after `jarvis hud install`. Every post-merge
  restart must run it, and the CLAUDE.md HUD paragraph must say so.
- **Licences.** VEX's tools may not be decompiled or redistributed. Only use `vexcom` flags from DishPy's MIT source or
  from `vexcom --help` as the owner ran it. Tests use a fake `vexcom` and never a device.
- **Terminal spans are advisory** (PR #25 review). Anything that relies on OSC 133 spans, such as the VEX brain terminal
  being readable through WP-F, must still apply text-pattern refusals to every read.
