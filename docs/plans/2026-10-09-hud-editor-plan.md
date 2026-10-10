# Plan: the HUD's code editor — a whole Monaco, safe saving next to agents, tree-sitter colour in Graphite, and VEX built in (Jarvis v2 HUD)

_Planner's proposal, written read-only against `origin/main` at e8ef815, with
WP-A (`feat/hud-workspace-a`) and WP-C (`feat/hud-terminal-backend`) read from
their branches. Those branches are still moving, so this plan cites them by
function name rather than by line. The owner has answered the first round of
questions (§4.1). Where a later decisions file disagrees, the decisions file
wins._

## For the owner: what this gives you

- **First, the editor you have is missing most of itself.**
  - The HUD loads only Monaco's bare API, not its features or its languages.
    So today a file has no syntax colour, Ctrl+F opens Chrome's page search,
    and Ctrl+S opens Chrome's "Save page as".
  - Reading the code also says that typing a space in the editor starts
    push-to-talk and the space never appears.
  - ED-1 fixes all of this, and its first test confirms the space bug before
    fixing it.
- **A real editor, without the intellisense.** Claude Code is your
  intellisense, so there are no language servers and no AI completions. You
  get Monaco's own editing:
  - find and replace;
  - folding;
  - comment toggle;
  - multi-cursor;
  - bracket matching;
  - word suggestions drawn from the files you have open;
  - sticky scroll;
  - go to line.
- **Saving is safe next to Claude, Codex and Jarvis.**
  - If an agent changes a file you have open and you haven't touched it, the
    file simply reloads.
  - If you have edited it, a bar offers *Compare*, *Keep mine* or *Take
    theirs*.
  - Nothing ever silently overwrites a newer file, and there is no "force
    save" anywhere.
- **Colour the Neovim way, in Graphite.**
  - **Tree-sitter** parses the file and Neovim's own highlight queries colour
    it.
  - The scheme is one quiet colour scheme drawn from the HUD's palette:
    - most code is the normal text colour;
    - keywords are bold;
    - comments are grey italic;
    - colour is kept for functions, strings, constants and types.
  - **No syntax colour is amber or red.**
  - It covers C/C++ (your VEX code), Python, TS/JS/TSX, JSON, Markdown, Bash,
    CSS/HTML, YAML and TOML. Everything else gets Monaco's simpler colouring.
- **VEX inside the HUD.** For a VEX V5 project, a VEX view offers:
  - **Build**, using the same toolchain your VS Code extension uses;
  - **Download** to a slot (1–8) with a name;
  - the **brain terminal**, which shows your program's printouts;
  - **device info**;
  - **New VEX project** from VEX's own C++ and Python templates.
  - Download is only ever your click. Jarvis cannot download to the robot,
    and nothing runs the program for you.
- **Tabs, Ctrl+P to open a file, Ctrl+Shift+F to find in files, a command
  palette, and a git gutter.**
- **Jarvis gets no lever on any of it.** No tool opens, types into, saves or
  downloads.

```
┌ pane 2 · file · in: override ─────────────────────────────────────────────┐
│ auton.cpp ●  │ main.cpp │ robot-config.h                                   │
│ 12  void drive_for(double inches) {          ← "void" bold, name in glacier│
│ 13    // straight-line move                  ← grey italic                 │
│ 14    Drivetrain.driveFor(forward, inches, vex::distanceUnits::in);        │
│ ⚠ auton.cpp changed on disk while you were editing — Compare · Keep mine…  │
└───────────────────────────── Ln 14 · Spaces 2 · UTF-8 · LF · syntax: tree ─┘
┌ pane 3 · VEX · override ──────────┐   ┌ panel ────────────────────────────┐
│ C++ · V5 · SDK V5_20240802        │   │ [bash] [build ✓] [brain ●]        │
│ [Build] last: ✓ 56,288 B · 12:41  │   │ > Auton start                     │
│ Slot [3▾] Name [override]         │   │ > heading 90.2                    │
│ [Download to slot 3]              │   │                                   │
│ Brain: "BLUE-1" · VEXos 1.1.5     │   │                                   │
└───────────────────────────────────┘   └───────────────────────────────────┘
```

## 1. What exists now

### 1.1 How a file is opened, read and saved

**The routes** are in `jarvis/v2/hud_api.py:1039–1082`, with the contract in
`docs/hud-api.md` under Files. They are served on both the HUD listener
(8402) and the API listener (8405).

| Route | What it does |
|---|---|
| `GET /projects/{id}/tree?path=&depth=` | Lists a directory. It skips only `SKIP_DIRS` (:27, :174–206): `node_modules`, `.venv`, `.git` and the like. **Hidden directories are listed.** |
| `GET /projects/{id}/file?path=` | Returns `{path, content, mtime, size, protected}`. The bytes are decoded with `errors="replace"` (:1055). |
| `PUT /projects/{id}/file` | Takes `{path, content, expected_mtime}`. Under `daemon._lock`, it compares the file's current `st_mtime` with the expected one and returns 409 on a mismatch (:1068–1077). It refuses protected names, gate-state files and SELF_PROTECTED files (`denied_file`, :1071). It writes atomically through `_write_bytes` (`stores.py:87–101`) and keeps the file's mode. |

**Scoping, caps and refusals.**
- `scope()` (:137–156) resolves the path both as written and as the
  filesystem resolves it. It refuses `..`, backslashes, `://`, anything
  outside the root plus `extra_dirs`, and any `SKIP_DIRS` component.
- `FILE_CAP` is 2 MB (:28).
- A protected credential *name* (`secrets.PROTECTED_NAMES`, `secrets.py:41–50`)
  reads back as `{"protected": true}` with no content. **The check is by name
  only** (`secrets.py:71–73`).

**Three things in this path are wrong or thin.**
1. **Saving corrupts a file that is not UTF-8.** The read replaces bad bytes
   with U+FFFD, and the save writes `EF BF BD` back wherever a Latin-1 `é` or
   a binary byte used to be.
2. **Credential folders can be browsed through the Inbox.**
   - The Inbox is rooted at your home folder (`stores.py:253–254`), and
     `scope()` does not skip hidden folders.
   - So the File tab lists `~/.ssh`, `~/.aws` and `~/.config/gh`, and shows a
     private key in full.
   - `~/.claude/.credentials.json` and `~/.codex/auth.json` are not protected
     names either.
   - The fast path's `read_file` already reads these files, so this gives an
     agent nothing new. But it breaks the File tab's own rule that credential
     contents "are never sent to this window".
3. **An mtime is a weak precondition.**
   - Float `st_mtime` equality misses a same-size, same-mtime rewrite, which
     happens on the 9p `/mnt/c` mount.
   - It raises a false conflict on a `touch` that changed nothing.
   - The only answer the UI gives is "reload", which throws away your edit
     (`FileTab.tsx:128–131`).

### 1.2 How Monaco is loaded

**Only the bare API is loaded.**
- `hud/src/lib/monaco.ts:13–59` imports `monaco-editor/esm/vs/editor/editor.api`
  and `editor.worker?worker`, against `"monaco-editor": "^0.52.2"`.
- `editor.api` is the API with nothing registered: no contributions from
  `editor.all` and no Monarch language definitions.
- The built bundle confirms it. `dist/assets/editor.api-*.js` (2.29 MB) has
  no suggest, comment, format, go-to-line or quick-command action, and no
  language id at all.
- So `languageFor()` (:62–71) names languages that nobody registered, and
  every file renders as plain text.

| You would expect | Today |
|---|---|
| Syntax colour | None, in any language |
| Ctrl+F find/replace | Chrome's find-in-page |
| Word suggestions (Ctrl+Space) | None |
| Folding, bracket matching, Ctrl+/ comment, multi-cursor commands | None |
| Ctrl+G go to line, F1 command list | None |
| Ctrl+S | Chrome's "Save page as"; only the Save button saves |

**Worker and build setup.**
- Workers come in through Vite `?worker` imports, never from a CDN. The HUD
  works offline, and nothing agent-written may become a script source.
- `vite.config.ts:13–16` only raises the chunk-size warning limit.

**What changed in Monaco since 0.52.2.** 0.57.0 (2026-09-24) is current.
- **0.53** deprecated AMD.
- **0.55** moved the language namespaces to the top level.
- **0.56** added supported, tree-shakeable entry points
  (`monaco-editor/editor`, `features/*/register`, `languages/*/register`).
- **0.57** updated the core and DOMPurify.

### 1.3 The theme

- There is one theme, `"jarvis"`: `vs-dark` with Graphite chrome
  (`monaco.ts:23–54`).
- Its token colours are vs-dark's, which is moot today because nothing is
  tokenized.
- Diff colours are low-alpha green and red. The comment says red there means
  "removed", not "error".

### 1.4 Keys

**Layout keys** are caught in the window's capture phase
(`Layout.tsx:99–129`, `shortcutFor` at `layout.ts:284–298`).
- The zoom keys belong to the HUD everywhere.
- Ctrl+B and Ctrl+Alt+B stand aside **entirely** inside Monaco (`inMonaco`,
  `layout.ts:307–311`). This is to keep Monaco's Ctrl+K Ctrl+B chord, which
  isn't even loaded today.
- Every layout key is inert while an approval card is up (:112–115).

**Push-to-talk is a document-level Space handler** (`App.tsx:1013–1032`).
- It calls `preventDefault()` and `press()` unconditionally.
- Every other typing surface stops Space from bubbling: the input bar,
  pickers, chips, the separator, and the editor's *fallback* textarea
  (`Editor.tsx:58`).
- **The Monaco host does not.** Monaco stops propagation only for keys it
  has bound (`standaloneServices.js:258–265` in 0.52.2).
- So, by reading the code: a space typed in Monaco starts push-to-talk, and
  the space is never inserted.
- The file suite types `"\n# edited"` and only checks that Save armed
  (`hud_v2_check.py:862–879`), so it cannot see a lost space.
- WP-A's branch has the same handler.

### 1.5 When a file changes on disk under the editor

**Nothing happens.**
- A clean buffer shows stale code while an agent rewrites the file.
- A save after an agent's write returns 409. That is good: nothing is
  clobbered.
- But the only way forward is Reload, which discards your edit.

### 1.6 Other defects found on the way

- **The diff view leaks two models per file viewed.** `createModel` is called
  twice per effect run (`Editor.tsx:85–88`), and only the editor is disposed
  (:91–94).
- **Edits are lost without a word.**
  - Opening another file replaces the buffer with no dirty check
    (`FileTab.tsx:101–115`). So does Reload.
  - On main, switching tabs unmounts the File tab (`App.tsx:1325`).
  - On WP-A's branch, a pane that switches view or re-pins its project
    unmounts the editor.
  - There is no `beforeunload` guard.
- **Every keystroke copies the whole file into React state.**
  `onDidChangeModelContent` calls `getValue()` (`Editor.tsx:35`), and the
  `value` effect calls `setValue()` (:46–49), which also wipes undo.
- **Mixed line endings are normalised silently** by the first edit and save.
- **Monaco's overlays can float above the approval card.** Context menus,
  hovers and quick-input carry z-indexes in the thousands, and nothing gives
  the editor host its own stacking context. They are not loaded today; ED-1
  loads them, so ED-1 must fence them in.

### 1.7 What the HUD serves, and who can write it

- **No Content-Security-Policy.** `binary()` sends one only when a caller
  passes it (`hud_api.py:691–702`), the HUD's own routes never do (:977–988),
  and `hud/index.html` has no meta CSP.
  - So WebAssembly needs nothing today: no `'wasm-unsafe-eval'` is required.
  - WP-E plans only `frame-ancestors 'none'` and `X-Frame-Options`.
- **The HUD's code is agent-writable today.**
  - The daemon serves `/` and `/assets/*` from the repo's own `hud/dist`
    (`HUD_DIST = …/hud/dist`, `hud_api.py:32`).
  - The project `Jarvis-improvements` is rooted at `~/projects/Jarvis`.
  - Claude and Codex chat threads run in their project's folder, and nothing
    protects `hud/dist`. SELF_PROTECTED covers a handful of Python files.
  - So an agent working on Jarvis can write the JavaScript the approval window
    runs. This plan adds WebAssembly files to that folder, and §2.7 closes the
    gap as far as the file tools allow.
- **No eval today.** The current HUD bundles contain no `eval` or
  `new Function`. Monaco needs none; its CSS uses `data:` SVG for squiggles.

### 1.8 What WP-A to WP-F give this plan

- **WP-A (panes):**
  - six presets, with `PaneSpec {view, projectId, terminalId, previewUrl}`;
  - file panes pin their project;
  - panes stay mounted but hidden;
  - Alt+click opens beside;
  - all persisted under `jarvis.hud.workspace`.
- **WP-C (backend):**
  - `ws.py`, the hand-rolled RFC 6455 endpoint;
  - `terminals.py`:
    - PTY terminals with `Terminal.spawn(argv, env)`;
    - a 1 MiB ring that replays on reattach;
    - `owner_only` plus `_strict_host` routes;
    - single-use 30-second tickets;
    - `clean_environment`, which passes the WSL interop variables;
    - `session_members`;
    - lifecycle-only logs;
    - nothing on the bus.
- **WP-D:** xterm, the bottom panel with its tabs, Ctrl+\`, and the
  `route_web_socket` fake used by the headless suites.
- **WP-E:** `frame-ancestors` and `X-Frame-Options` on every HUD and API
  response.
- **WP-F:** `terminal_read`. It can read a terminal when your "Jarvis can
  read" switch allows it, and **never writes**.

### 1.9 VEX on this machine (checked read-only, 2026-10-09)

**The extension.**
- **VEX Robotics**, publisher `VEXRobotics`, id `vexcode`, version
  `0.8.2026020401`.
- It is installed for VS Code (`/mnt/c/Users/johnw/.vscode/extensions/`) and
  Cursor (`…/.cursor/extensions/…-universal`), and depends on
  `VEXRobotics.vexfeedback`.
- Its contributions:
  - commands for project new, import, build, clean, rebuild and settings;
  - system commands: info, download, erase, screen grab, brain name, team
    number, VEXos, controller, DFU and Python-VM updates, battery medic;
  - terminal clear and "new set";
  - toolchain and SDK download;
  - two sidebar views: project actions and device info;
  - settings including `Project.RunAfterDownload`, `Cpp.Toolchain.Path`,
    `General.EnableUserTerminal` and a `WebsocketServer` (off);
  - **Ctrl+B bound to build.**
- The extension folder bundles no device tools: just `dist/extension.js`
  (486 KB), webview apps, TextMate grammars, docs and **project templates**.

**Its downloads**, in `…/AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode/`:

| What | Where | Size / contents |
|---|---|---|
| `vexcom` 1.0.2.2 | `tools/vexcom/1_0_2_2/{win32/vexcom.exe, linux-x64/vexcom, linux-arm32/…, linux-arm64/…, osx/…}` | `vexcom.exe` 1,568,653 B, plus `libusb-1.0.dll` and `libcurl.dll`; **`linux-x64/vexcom` 1,438,536 B also exists** |
| C++ toolchain | `tools/cpp/toolchain_win32/{clang,gcc,tools}/bin` | `clang.exe`, `arm-none-eabi-{ar,ld,objcopy,size}.exe`, `make.exe`; 65 MB. **Windows only: no Linux toolchain was downloaded** |
| C++ SDK | `sdk/cpp/V5/V5_20240802_15_00_00/vexv5/` | `libv5rt.a`, `lscript.ld`, `stdlib_0.lib`, headers, `license.pdf` |
| Python SDK | `sdk/python/V5/V5_1_0_1_25/` | `manifest.json`, `vexv5/` |
| Firmware images, drivers, logs | `vexos/*`, `drivers/VEX Devices Driver Installer.exe`, `logs/`, `buildText/tempBuildLog.txt` | Your last builds of `override` (2026-10-08/09) logged `windows build for platform vexv5`, a 56,288-byte text segment, and "Downloading User Program … Finished" |

- **The build is a plain makefile.** The template `vex/mkenv.mk` takes
  `P=` (project name), `T=` (SDK path), `V=` and `PRINTF_FLOAT=` "passed from
  app". It calls `clang`, `arm-none-eabi-*` and `make`, and switches to
  `SHELL = cmd.exe` when `OS=Windows_NT`.
- **Templates** live in `resources/templates/projects/V5/`:
  - 7 C++ VS Code templates (`cpp_v5_vsc_{empty,competition_template,…}.zip`,
    each holding `makefile`, `vex/mkenv.mk`, `vex/mkrules.mk`,
    `include/vex.h` and `src/main.cpp`);
  - 10 Python ones (`py_v5_vsc_*.zip`, each holding `src/main.py`);
  - about 70 VEXcode example zips;
  - an `index.json` of categories.
- **Project settings** are `.vscode/vex_project_settings.json`, described by
  the extension's `vex_project_settings.schema.json`. Its fields are
  `project.name`, `platform`, `language` (`cpp` | `python`), `slot` (1–8),
  `sdkVersion`, `cpp.includePath`, `cpp.printf_float` and `python.main`.

**USB.**
- WSL has no `/dev/ttyACM*` and no `/sys/bus/usb`: nothing is attached.
- `usbipd-win` is not installed.
- The WSL kernel (6.6.87.2) builds `CONFIG_USB_ACM=m` and
  `CONFIG_USBIP_VHCI_HCD=m`, so passthrough *could* work after setup.
- Windows has VEX's V5 brain, controller and DFU serial drivers in its
  DriverStore.
- WSL interop is enabled (`/proc/sys/fs/binfmt_misc/WSLInterop`), and
  `wsl.conf` has `systemd=true`.

**Linux build tools.** WSL has no `clang`, `make` or `arm-none-eabi-*`.

**Your VEX code** (names and settings files only):
- **One VS Code-extension project**,
  `/mnt/c/Users/johnw/OneDrive/Desktop/override` (`.vscode/vex_project_settings.json`:
  name `override`, platform V5, language `cpp`, slot 1, SDK
  `V5_20240802_15_00_00`), with `src/`, `include/` and `vex/`.
- **52 `.v5cpp`, 1 `.v5python` and 2 `.v5blocks`** VEXcode-app files under
  `OneDrive/Desktop/Robotics/`.
- **No PROS projects.**
- No Jarvis project points at any of them yet.

**Licences** (all that can be cited):
- The extension (`LICENSE.txt`) and `vexcom` (`tools/vexcom/1_0_2_2/LICENSE`)
  carry the same "Limited License terms": you may install and use any number
  of copies on your devices. You **may not decompile, reverse engineer or
  disassemble** the software, and **may not transfer it to a third party on a
  standalone basis**.
- Neither says anything specific about use outside VS Code.
- The C++ SDK has its own `license.pdf`, which I did not read.
- So this plan:
  - runs the installed tools **in place, on your machine**;
  - **never copies, bundles or commits** any of them;
  - **never learns their interface by decompiling.** The only `vexcom` flags
    known here come from DishPy's public, MIT-licensed source
    (https://github.com/aadishv/dishpy): `--name N --slot S --write FILE
    --timer --progress` for download, and `--user` for the user-port
    terminal. Everything else comes from `vexcom --help`, which **you** run
    once.

## 2. Design

### 2.1 Ground rules

1. **No editor surface may become the script injection that reaches the
   approval window.**
   - File text, diagnostics and terminal or build output are drawn as text.
   - Colours and font names are validated before they reach CSS.
2. **Only code bundled at build time runs in the HUD.** This covers scripts,
   workers, WebAssembly and highlight queries. None comes from a project
   folder, the network, or a URL built at run time. A CSP enforces it
   (§2.7).
3. **There is one write path.**
   - Every change to a file goes through `PUT /projects/{id}/file`, with a
     content precondition and the same refusals: a keystroke and Save, a
     "Take theirs", a template unpacked into a new folder.
   - There is no force flag anywhere.
4. **Physical actions are your clicks.** Download to the robot is a button in
   the HUD and nothing else. "Run after download" does not exist (§4.2, V-3).
5. **The agent has no lever.**
   - No tool, MCP tool, fast-path tool or Discord verb names the editor, the
     VEX routes or the device.
   - The tests prove it in the `archive_check` pattern.

### 2.2 A whole Monaco, without intellisense (ED-1)

**Upgrade to `monaco-editor` 0.57.0, pinned exactly**, using the supported
entry points instead of `editor.api`:
- `monaco-editor/editor`;
- a **curated feature list** of `monaco-editor/features/<name>/register`:
  - find, folding, comment, multicursor, bracketMatching, linesOperations,
    wordHighlighter, wordOperations, wordPartOperations, smartSelect,
    stickyScroll, gotoLine, quickCommand, contextmenu, clipboard, cursorUndo,
    caretOperations, dnd, hover (link and unicode explanations),
    unicodeHighlighter, links, indentation, suggest, snippet,
    **semanticTokens** (tree-sitter rides on it, §2.6), placeholderText,
    readOnlyMessage, inPlaceReplace, middleScroll, insertFinalNewLine,
    diffEditor;
  - **left out**, because they only light up with a language service: rename,
    gotoSymbol, referenceSearch, quickOutline, parameterHints, codeAction,
    codelens, inlayHints, colorPicker, inlineCompletions, inlineEdits, format
    and documentSymbols;
- `monaco-editor/languages/definitions/register.all`: about 80 Monarch
  definitions, **each tokenizer loaded only when a model needs it**. This is
  the fallback colouring (§2.6) and the source of each language's
  comment-toggle, bracket and auto-close rules.
- **No language-service workers.** The TypeScript, JSON, CSS and HTML workers
  are not loaded: that is what "no intellisense" means here, and it saves
  about 6 MB. `editor.worker` stays, for word suggestions, diffs and links.

**What remains, and what does not:**

| Kept (works in every language) | Not offered |
|---|---|
| Find/replace with regex, case and whole word (Ctrl+F, Ctrl+H) | Hover info, signature help |
| Folding: indentation first, syntax-aware after ED-6 | Go to definition, references, rename symbol |
| Comment toggle (Ctrl+/, Shift+Alt+A) | Errors as you type, quick fixes |
| Multi-cursor (Alt+click, Ctrl+Alt+↑/↓, Ctrl+D), column selection | Format Document |
| Bracket matching, auto-close, bracket-pair colours | JSON schema validation |
| **Word suggestions** from the words in your open files (Ctrl+Space, and as you type) | AI completions |
| Sticky scroll, go to line (Ctrl+G), move or copy lines, smart select | Breadcrumbs, symbol search |
| Monaco's own command list (F1), merged into the HUD palette (ED-7) | |
| Links in comments (http/https only), unicode highlighter (invisible and look-alike characters) | |

**Settings that matter.**
- **The language comes from Monaco's registry, not `languageFor()`.**
  - Models are created with a `file://<abs path>` URI, so extensions, file
    names and shebang lines resolve.
  - `.h` is registered as **C++**: VEX headers are C++.
  - `.toml` gets a language id of its own (Monaco has none) with `#`
    comments.
  - `languageFor` is deleted.
- **Links** open only `http`/`https`, in a new window with `noopener`
  (`registerLinkOpener`). `file:` links inside the project open in the
  editor; nothing else opens.
- **`isolation: isolate` on the editor host**, so Monaco's z-indexed layers
  stack inside the editor and never above `#authveil` (z 100).
- **EditContext:** pinned off if the headless suites cannot type into it (a
  spike decides). `inMonaco()` works either way.

**Key and lifecycle fixes in the same package.**
- **Space**:
  - goes to push-to-talk only when the event's target is not a typing target;
  - a pure `isTypingTarget(el)` covers input, textarea, contenteditable,
    `.monaco-editor` and WP-D's xterm host;
  - one rule replaces a dozen `stopPropagation`s.
- **Ctrl+S** saves the focused buffer. Its default is always prevented, and
  it is inert under a card.
- **Ctrl+B** folds the sidebar **from the editor too, except as the second key
  of a Ctrl+K chord** (decision E-10). A pure `chordPending` tracker in the
  capture-phase handler sees Ctrl+K inside Monaco and lets the next key
  through.
- **While a card is up**, every editor goes read-only and loses focus, as
  WP-D's terminal does. Escape then reaches the card, and a stray `y⏎` lands
  nowhere.
- **The diff view disposes its models.** They use a `jarvis-diff:` URI
  scheme.

### 2.3 Buffers, tabs and panes (ED-2)

**A buffer is one Monaco model per file on disk, keyed by its absolute path.**
- Projects nest: the Inbox is rooted at `~`. So one file opened from two
  projects is one buffer, never two that can diverge.
- The read route returns `abs`.
- A buffer remembers:
  - the project it was read through, and saves through;
  - the content `etag`;
  - the alternative version id at the last save. Dirty means "not equal", so
    undoing back to the saved text reads clean.
  - its encoding, EOL and read-only reason;
  - its conflict state.

**The registry lives outside React** (`state/buffers.ts`).
- Unmounting a view never loses an edit.
- The text is read once, on save.

**Tabs belong to file panes.**
- Each pane has an ordered list and **one Monaco editor whose model is
  swapped**.
- View state is kept per pane and buffer: cursor, selection, scroll and folds.
- A single click opens a *preview* tab in italics, which the next single
  click replaces. A double click or an edit pins it.
- Middle-click or × closes a tab.
- Closing the last tab on a dirty buffer asks: Save / Don't save / Cancel.

**The same file in two panes** shares the model, the dirty dot and the undo
stack, with separate view states.

**Persistence.**
- WP-A's `PaneSpec` gains an optional `editors: {paths, active}`, with
  per-field fallback. Tabs come back after a reload and are re-read from disk.
- **Unsaved text is not persisted.** Instead, `beforeunload` asks while
  anything is dirty, which also covers Ctrl+W, the one close key a page
  cannot take.
- A model is disposed when no tab references it and it is clean.

### 2.4 Saving next to agents (ED-2)

**Content preconditions.**
- `GET /file` returns `etag`: the first 128 bits of the SHA-256 of the bytes
  on disk.
- `PUT` takes `expected_etag`; `null` means "must not exist".
- Under the lock, the daemon re-hashes the current bytes and returns 409 with
  the current etag on a mismatch.
- `expected_mtime` is accepted for one release, then removed.

**Noticing changes: the HUD polls.**
- `POST /projects/{id}/files/stat {paths}` takes at most 100 paths. It
  re-hashes only when `(mtime_ns, size)` moved.
- It runs every 2 s while the window is visible, and on focus.
- **inotify cannot see changes made from Windows on a 9p `/mnt/c` mount**,
  and your VEX project lives on one.

| Buffer | Disk changed | What happens |
|---|---|---|
| Clean | yes | Reloads in place, with `pushEditOperations` so undo still steps back. A toast says the file changed on disk. It names the writer ("Codex · task 41") when the activity feed has said a running turn touched that path. |
| Dirty | yes | An amber **conflict bar** offers *Compare*, *Keep mine* and *Take theirs*. **Compare** shows the disk version on the left, read-only, and your buffer on the right, editable. **Keep mine** saves against the disk etag you were shown, so another write brings the bar back. **Take theirs** asks once. |
| Saving | 409 | The same bar. |
| Gone | — | *Save to recreate* (`expected_etag: null`) or *Close*. |

Amber is right here because it means "waiting on your decision", and this is
HUD chrome, not syntax.

**Read-only kinds**, decided by one classifier, `jarvis/v2/editor_policy.py`.
The file routes, search and the VEX template writer all use it.

| Kind | Examples | In the editor |
|---|---|---|
| **withheld** | Protected names; anything under `V2_CREDENTIAL_DIRS` (`~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.config/jarvis`, `~/.gnupg`); `~/.claude/.credentials.json`; `~/.codex/auth.json` (decided, E-8) | "withheld", no content |
| **read-only: gate state** | `protected_paths()`, including the new `vex.json` (§2.9), even inside `~/.config/jarvis` | Opens with a lock |
| **read-only: safety layer** | SELF_PROTECTED files | Opens with a lock |
| **read-only: not text** | Invalid UTF-8, or a NUL byte in the first 8 KiB | Opens read-only. The PUT also refuses to overwrite non-UTF-8 bytes |

**The status bar** shows the EOL, and says when line endings are mixed.

### 2.5 The Graphite editor scheme (ED-3)

**One scheme, in the spirit of Neovim 0.10's default.** Most code is the
normal text colour, told apart by weight and slant, and colour is kept for a
few kinds of thing.
- **The neutrals are the HUD's own tokens.**
- **The four hues are new `--syn-*` tokens**, each a quieter sibling of a HUD
  hue and declared on `:root` beside them. They are siblings rather than the
  tokens themselves so the chrome's meanings stay single: the glacier accent
  means focus, selection, links and "running", and `--green` means "done".
- **No syntax colour sits in the amber or red family.** Red appears only as
  an error squiggle (nothing raises one today) and as the existing low-alpha
  "removed" tint in diffs.

| Token | Value | From | Hue | On `--bg-1` #1a1a19 | On line highlight #222221 | On selection #2f444b |
|---|---|---|---|---|---|---|
| `--text` | #e2dfd7 | HUD | — | 13.1 : 1 | 12.0 | 7.7 |
| `--text-2` | #b4b0a8 | HUD | — | 8.1 | 7.4 | 4.7 |
| `--text-muted` | #98948b | HUD | — | 5.8 | 5.3 | 3.4 |
| `--syn-fn` | #8ec5d6 | glacier (`--accent` family) | 194° | 9.2 | 8.4 | 5.4 |
| `--syn-string` | #a6c79b | sage (`--green` family, desaturated) | 105° | 9.3 | 8.5 | 5.5 |
| `--syn-const` | #b9aade | lavender (`--badge-claude` family) | 257° | 8.2 | 7.5 | 4.8 |
| `--syn-type` | #a8b5c2 | steel (`--badge-win` family) | 210° | 8.3 | 7.6 | 4.9 |

**Every pair passes WCAG AA (4.5:1) on the background and on the current-line
highlight.** Comments over a selection drop to 3.4:1, which still passes AA
for large text. That is a passing state while text is selected, as in VS
Code.

**Captures → colours.** The capture names are nvim-treesitter's. The same
rows style Monarch's coarser tokens when Monarch is the fallback.

| Capture (nvim-treesitter) | Example | Colour | Style |
|---|---|---|---|
| `@variable`, `@variable.member`, `@variable.parameter`, `@property` | `speed`, `self.x`, `int inches` | `--text` | — |
| `@keyword.*` (`if`, `return`, `def`, `class`, `const`, `import`, `for`) | | `--text` | **bold** |
| `@keyword.directive`, `@keyword.import` in C/C++ (`#include`, `#define`), `@attribute`, `@function.macro` | `#include "vex.h"` | `--syn-type` | — |
| `@function`, `@function.call`, `@function.method`, `@function.method.call`, `@constructor` | `driveFor(…)` | `--syn-fn` | — |
| `@function.builtin` | `print`, `len` | `--syn-fn` | *italic* |
| `@type`, `@type.builtin`, `@type.definition`, `@module`, `@module.builtin` | `int`, `motor`, `vex`, `std` | `--syn-type` | — |
| `@string`, `@string.special`, `@character` | `"Auton"` | `--syn-string` | — |
| `@string.escape`, `@string.regexp`, `@character.special` | `\n` | `--syn-const` | — |
| `@number`, `@number.float`, `@boolean`, `@constant`, `@constant.builtin` | `90.0`, `true`, `nullptr`, `None` | `--syn-const` | — |
| `@variable.builtin` | `self`, `this` | `--text` | *italic* |
| `@operator` | `+ = -> ::` | `--text-2` | — |
| `@punctuation.delimiter`, `@punctuation.bracket`, `@punctuation.special` | `; , ( ) { }` | `--text-2` | — |
| `@comment`, `@comment.documentation` | | `--text-muted` | *italic* |
| `@comment.todo`, `@comment.note`, `@comment.warning`, `@comment.error` (TODO, FIXME) | | `--text` | **bold**. **Never amber or red**, unlike nvim's default |
| `@label` | `case` labels, goto | `--text-2` | — |
| `@tag` | `<div>` | `--syn-fn` | — |
| `@tag.attribute` | `class=` | `--syn-type` | — |
| `@tag.delimiter` | `< > />` | `--text-2` | — |
| `@property` in JSON, YAML and TOML keys | `"name":` | `--syn-type` | — |
| `@markup.heading` | `# Plan` | `--syn-fn` | **bold** |
| `@markup.strong` / `@markup.italic` / `@markup.strikethrough` | | `--text` | bold / italic / strike |
| `@markup.raw` (code spans and fences) | | `--syn-string` | — |
| `@markup.link.label` / `@markup.link.url` | | `--syn-fn` / `--text-muted` | underline |
| `@markup.quote` | | `--text-muted` | *italic* |
| `@markup.list` | `-`, `1.` | `--text-2` | — |
| `ERROR` / `@error` | | `--text` | none. Parse errors are not underlined, as in nvim's default |

**Editor chrome, pinned so nothing amber or red appears except errors:**
- **Background, cursor, gutter:** background `--bg-1`; current line
  `--bg-2`; line numbers `--text-muted`, with the active one `--text-2`;
  cursor `--accent-hi`.
- **Selection and matches:**
  - selection is accent at 25%, and an inactive selection accent at 13%;
  - **find matches** are accent at 35% with an accent border, and other
    matches accent at 15% (Monaco's default is orange);
  - word highlight is white at 8%.
- **Brackets:** bracket match has a `--text-muted` border.
  **Bracket-pair colours** are `--syn-fn`, `--syn-const` and `--syn-type`
  (Monaco's default is gold, which reads as amber).
- **Unicode highlight border** is `--syn-const`; Monaco's default is
  amber-ish.
- **Error and warning:** `editorError.foreground` is `--red`, for squiggles
  only. `editorWarning.foreground` is `--text-2`, so nothing can paint a
  warning amber.
- **Diff:** inserted is `--green` at 15% and 7%, removed is `--red` at 15%
  and 7%. This is today's choice, kept.
- **Git gutter (ED-9):** added `--syn-string`, modified `--syn-fn`, deleted
  `--red` at 60%, the same "removed" convention as the diff.
- **Whitespace and sticky scroll:** whitespace marks `--border-strong`;
  sticky scroll `--bg-1` with a `--border` rule.

**Tested as data, not by eye.**
- The scheme is one table in `lib/editorTheme.ts`.
- A vitest test recomputes every contrast and fails below 4.5:1 on
  `--bg-1` and `--bg-2`.
- It also fails if any syntax colour's hue falls in the amber band
  (25°–65°) or the red band (340°–20°) at saturation ≥ 0.35.

### 2.6 Syntax colour the Neovim way: tree-sitter (ED-5, ED-6)

**The shape.**
- One **syntax worker** (`lib/syntax/worker.ts`, built by Vite) runs
  **`web-tree-sitter` 0.27.0** (MIT; `web-tree-sitter.wasm` 209,613 B).
- For each open model it holds a parser, a tree and the compiled highlight
  query.
- The main thread registers a **`DocumentRangeSemanticTokensProvider`** for
  each covered language. Monaco asks for the visible range on open, scroll and
  edit, and the worker answers with delta-encoded tokens.
- **Monarch stays underneath as the base tokenizer**, so colour is never
  missing. Semantic tokens from tree-sitter override it as soon as they
  arrive.
- Semantic tokens are the right Monaco layer for this:
  - they are range-based, so only the viewport is queried;
  - Monaco shifts them with edits until they are refreshed;
  - they style through the same theme rules as Monarch tokens.
- Decorations would mean thousands of DOM class names per view.
- Monaco's line-by-line tokens provider cannot see a tree.

**Incremental reparse.**
- Each Monaco change event goes to the worker as `{rangeOffset, rangeLength,
  text, versionId}`.
- The worker applies it to its own copy of the text, calls `tree.edit()`,
  then `parser.parse(text, oldTree)`. Tree-sitter reuses every untouched
  subtree.
- Changes within 30 ms coalesce into one reparse.
- web-tree-sitter parses a JS string as UTF-16. The worker converts every
  offset and point in one module, `lib/syntax/positions.ts`, tested with
  astral characters and CRLF. The spike confirms the unit.
- A token request carries the model version. Monaco discards and re-asks if
  the model has moved on.
- **Captures are flattened**: the most specific capture wins, and later
  patterns break ties, as tree-sitter's highlighter does. This gives
  non-overlapping spans, which semantic tokens require.

**Injections**, one level deep, sharing the parser cache:
- Markdown's inline grammar inside paragraphs.
- Markdown fenced code, by the fence's info string, for any covered language.
- HTML `<script>` → JavaScript and `<style>` → CSS.
- Each injected range reparses whenever the edit falls inside it. Injected
  bytes are capped at 256 KB per document; beyond that, fences show as plain
  code.

**Budgets and fallbacks.** You never see a broken file, only a quieter one.

| What | Budget | Over budget |
|---|---|---|
| File size | Tree-sitter up to **1 MB**; the editor opens up to 2 MB (`FILE_CAP`) | Monarch only, and the status bar reads "syntax: basic (large file)" |
| First parse | **1 s**, using web-tree-sitter's `progressCallback` to cancel | Monarch only for that model |
| Incremental reparse | ~10–50 ms typical; **cancelled at 250 ms** | Keep the last tree (stale colour for a moment), retry after 500 ms idle. Two cancellations means Monarch for that model |
| Viewport query | ≤ 8 ms, ≤ 20k captures; lines over 10k characters skipped | Truncate to the visible lines |
| Grammar load | First file of a language: fetch plus compile, roughly 50–150 ms even for C++'s 3.4 MB | Monarch until ready |
| Memory | Trees only for open models; `tree.delete()` on dispose | — |

Diff views get colour too: their models are parsed once and never edited.

**The language set.** Each grammar is MIT-licensed, and its prebuilt `.wasm`
ships inside its npm package, verified by the lockfile's integrity hash.

| Language | Grammar (npm, ≥ 2 weeks old) | `.wasm` | Covers |
|---|---|---|---|
| **C/C++** | `tree-sitter-cpp` 0.23.4 | 3,434,931 B | `.cpp .cc .cxx .hpp .hh .h` **and `.c`** |
| Python | `tree-sitter-python` 0.25.0 | 457,883 B | `.py` |
| JavaScript, JSX | `tree-sitter-javascript` 0.25.0 | 411,770 B | `.js .mjs .cjs .jsx` |
| TypeScript, TSX | `tree-sitter-typescript` 0.23.2 | 1,413,849 + 1,445,638 B | `.ts .mts .cts` / `.tsx` |
| JSON | `tree-sitter-json` 0.24.8 | 5,596 B | `.json .jsonc` |
| Markdown (block + inline) | `tree-sitter-grammars/tree-sitter-markdown` **v0.5.3** release (2026-02-26) | ~0.5–1 MB (two files) | `.md .markdown` |
| Bash | `tree-sitter-bash` 0.25.1 | 1,358,224 B | `.sh .bash`, shebangs |
| CSS | `tree-sitter-css` 0.25.0 | 128,668 B | `.css` |
| HTML | `tree-sitter-html` 0.23.2 | 18,385 B | `.html .htm` |
| YAML | `@tree-sitter-grammars/tree-sitter-yaml` 0.7.1 | 189,255 B | `.yml .yaml` |
| TOML | `@tree-sitter-grammars/tree-sitter-toml` 0.7.0 | 24,040 B | `.toml` |

**Notes on the set:**
- **Markdown is the one exception to npm.** Its npm package (0.3.2, 2024)
  ships no `.wasm`. The two release assets are vendored into
  `hud/vendor/tree-sitter/` with their SHA-256 in a manifest and the
  grammar's licence.
- **C has no grammar of its own.** The C++ grammar parses the C you write
  well enough to colour it, and saves 626 KB. nvim-treesitter's C++ queries
  inherit its C queries, so the C *query text* is vendored, not the C
  grammar.
- **Cut, and left to Monarch:**
  - SCSS/LESS, because you don't use them;
  - Makefiles and `.mk`, because VEX's are templates you rarely edit (a
    candidate for later);
  - Go, Rust, Java, SQL, Dockerfile and the rest, which keep Monarch's
    colour.

**Highlight queries: Neovim's own.**
- They come from **nvim-treesitter's `runtime/queries/<lang>/`**
  (Apache-2.0; the `main` branch, active, last push 2026-10-03), at a pinned
  commit at least two weeks old. They carry `highlights.scm`,
  `injections.scm` and, for ED-6, `folds.scm`.
- They are vendored into `hud/src/syntax/queries/`, with Apache-2.0's
  `LICENSE` and a `NOTICE` naming the commit.
- They are imported as `?raw` strings, so they are bundled text, never
  fetched.
- **A small, dev-run vendoring script** (`hud/scripts/vendor-queries.mjs`)
  makes them work with web-tree-sitter:
  - resolves nvim's `; inherits: c` lines by concatenation;
  - translates `#lua-match?` (Lua patterns) to `#match?` (JS regex) for the
    pattern classes those queries use;
  - keeps `#eq?`, `#match?` and `#any-of?`;
  - turns `#set! priority` into a sort key;
  - drops patterns that use nvim-only predicates it cannot translate, and
    lists each one dropped.
- **The fallback** is each grammar's own `queries/highlights.scm` (MIT,
  shipped in the same npm package). It uses the same capture vocabulary with
  fewer captures.
- **One test decides which source each language uses.** It compiles every
  bundled query against its bundled grammar with the pinned runtime, in
  vitest under Node. The runtime refuses a query that names nodes the grammar
  doesn't have, so a version mismatch between a pinned grammar and nvim's
  queries fails the build, not your screen.

**Folding from the tree (ED-6).** nvim-treesitter's `folds.scm` gives Monaco
a `FoldingRangeProvider` for the same languages. Folds then follow
`{ … }`, functions and Markdown sections rather than indentation, and sticky
scroll uses the same ranges.

### 2.7 Code that runs in the approval window (ED-4)

**What may run there.**
- **Every script, worker, `.wasm` and query is a build output.**
- The syntax worker loads grammars only from a **build-time map** of
  language id to Vite asset URL (`?url` imports with hashed names). The loader
  refuses any URL not in that map.
- It never reads a grammar or query from a project folder, an API response,
  a model's text or the network.
- The daemon serves `/assets/*` only from the HUD's build directory (resolved,
  no symlinks: `hud_api.py:983–986`).

**The CSP, which the HUD does not have today.** Today WebAssembly needs
nothing. This plan **adds** a policy, sent on **every** response from the HUD
and API listeners: the HUD page, `/assets/*` including the worker scripts,
and the JSON routes. Workers take their policy from their own script's
response, not from the page, so sending it on assets is what puts the syntax
worker under it too.

```
Content-Security-Policy:
  default-src 'none';
  script-src 'self' 'wasm-unsafe-eval';
  worker-src 'self';
  connect-src 'self';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data:;
  font-src 'self' data:;
  media-src 'self';
  frame-src http://127.0.0.1:* http://localhost:*;
  frame-ancestors 'none';
  base-uri 'none'; form-action 'none'; object-src 'none'
```

**Why each line:**
- **`'wasm-unsafe-eval'` is the only exception in `script-src`.** It allows
  compiling WebAssembly, which tree-sitter needs, and nothing else. There is
  **no `'unsafe-eval'`**. web-tree-sitter's Emscripten glue does contain
  `eval` in two places, `addEmAsm` and `addEmJs`, reached only when a loaded
  module carries EM_ASM or EM_JS code. A test loads every bundled grammar
  under this CSP. A grammar that needs `eval` is left out, and that language
  falls back to Monarch.
- **`connect-src 'self'`** covers `fetch`, `/events` and, in Chrome (CSP3),
  same-origin `ws://` for WP-C's sockets. If the headless check shows
  otherwise, the two exact `ws://127.0.0.1:<port>` and `ws://localhost:<port>`
  origins are added, built from the listener's port.
- **`style-src 'unsafe-inline'`** is needed for Monaco's injected styles and
  React's `style=` attributes. **`img-src data:`** is for Monaco's squiggle
  SVGs.
- **`frame-src`** covers the Preview pane's loopback frames. The preview
  judge already refuses the daemon's own ports.
- **`frame-ancestors 'none'`** is WP-E's line, carried in the same header.

**The HUD's own files: closing the gap in §1.7.**
- `jarvis hud install`, a human-run CLI, builds `hud/` and copies `dist/` to
  `config.HUD_DIR` (`~/.local/share/jarvis/hud/<build-id>/`) with a manifest
  of SHA-256 hashes.
- The daemon serves the newest installed build. It serves the repo's
  `hud/dist` only with `JARVIS_HUD_DEV=1`, which the headless suites set.
- `HUD_DIR` joins `protected_paths()`, so every agent file tool and v2's
  permit refuse to write it.
- **Stated plainly:** a *shell* command can still write there. That is the
  shell gap you accepted on 2026-10-08. So this narrows the exposure; it does
  not close it.
- It is still a real improvement: an agent editing Jarvis's source no longer
  edits the running approval window by accident, and a change only reaches the
  window when you run `jarvis hud install`. This is decision C-1.

### 2.8 Navigation and "all that good stuff"

| Feature | WP | Notes |
|---|---|---|
| Find/replace, multi-cursor, folding, comment, brackets, word suggestions, sticky scroll, go to line | ED-1 | §2.2 |
| Tabs, dirty dots, preview tabs, same file in two panes, conflict bar | ED-2 | §2.3, §2.4 |
| Graphite scheme | ED-3 | §2.5 |
| Tree-sitter colour; syntax folding | ED-5, ED-6 | §2.6 |
| **Quick open (Ctrl+P)** | ED-7 | `GET /projects/{id}/files`: `git ls-files --cached --others --exclude-standard` in a repo, else a walk skipping `SKIP_DIRS` and credential folders. Capped at 50k. Fuzzy matching is a pure, tested function |
| **Find in files (Ctrl+Shift+F)** | ED-7 | `POST /projects/{id}/search`, on a structured core extracted from `jarvis/tools/search.py`, so it and `grep_files` share **one** protected-file filter. Respects `.gitignore`, with a toggle (decided). 2,000 matches and 10 s at most. Refused on the Inbox, which is too broad |
| **Command palette (Ctrl+Shift+P)** | ED-7 | A static registry (pure, tested): HUD commands plus Monaco's actions when an editor is focused, including VEX: Build and VEX: Open brain terminal. **Download is not in the palette**: it is only the VEX view's button |
| Explorer: new file or folder, rename or move, delete | ED-7 | `POST /projects/{id}/fs`, `owner_only`. **Delete moves to the trash** (`trash.Trash.put`), never unlink |
| "Add selection to chat" | ED-7 | `path:L10-24` plus a fenced excerpt into the voice-target chat's box, **unsent**. This is how your intellisense, Claude, sees what you are looking at |
| Status bar | ED-7 | Ln/Col, selection count, language, indentation, encoding, EOL, and "syntax: tree / basic" |
| Settings | ED-8 | Font family (validated to a safe character set), size, tab size, spaces or tabs, word wrap (on for Markdown), minimap (**off**), sticky scroll, whitespace, bracket-pair colours, cursor style. Auto-save is **off** (decided). Stored in `jarvis.hud.editor` behind try/catch |
| `.editorconfig` | ED-8 | Sets indent style and size and EOL for new lines. **Never rewrites on save**: format-on-save is off, decided |
| Vim keybindings | ED-8 | Open decision E-11 |
| Git gutter, explorer git colours | ED-9 | `GET /projects/{id}/git/base?path=` returns HEAD's text (withheld names refused). A pure Myers line diff against the *buffer*. `git status --porcelain=v2 -z`, cached for 2 s |
| **Cut: Prettier formatting** | | Re-checked without language servers: your code is C++ and Python, which Prettier doesn't format. Agents format when they write. It would cost about 1 MB and a "config is code" exclusion. And format-on-save is off. Nothing formats in the HUD |
| **Cut: replace in files, Emmet, user snippets, hot exit, code lens, debugger, extensions** | | Decided (item 21) |
| **Cut: breadcrumbs, symbol search, hover docs** | | Decided: no intellisense |
| **Cut: importing VEXcode `.v5cpp` files** | | They are VEXcode-app projects. The VS Code extension has its own importer |

### 2.9 VEX in the HUD (VX-1 to VX-3)

**Where it lives.**
- **A `vex` pane view.** WP-A's `View` gains `"vex"`, offered in the view
  strip only when the pane's project is a VEX project. It shows the project
  card, Build, Download and device info.
- **WP-D panel tabs** for the **brain terminal** and the **build output**, so
  you get xterm's colours, scrollback and reattach after a reload.
- Errors from a build are a list in the VEX view, and clicking one opens the
  file at that line (ED-2). This is the build's own output, not a language
  server.

**Setup, a human-run CLI: `jarvis vex setup`.** It follows `jarvis desktop
setup` and `jarvis auth`.
- It looks in VS Code's and Cursor's `globalStorage/vexrobotics.vexcode/` and
  `extensions/vexrobotics.vexcode-*`.
- It prints what it found, by name, version and size: `vexcom`, the
  toolchain, the SDKs, the templates.
- It asks y/N, then writes **`~/.config/jarvis/vex.json`**, holding absolute
  paths for:
  - `vexcom.exe`;
  - `clang.exe`;
  - the four `arm-none-eabi-*` tools;
  - `make.exe`;
  - the C++ and Python SDK folders;
  - the templates folder;
  - the new-project folder (V-4).
- **`vex.json` is gate state.** It decides which executables run when you
  click, so it joins `protected_paths()` (`tools/files.py`, a SELF_PROTECTED
  file, so you review that hunk). No agent file tool can repoint it.
- The daemon re-checks every path at each use:
  - absolute;
  - a regular file;
  - inside one of those two VS Code or Cursor locations;
  - otherwise the action is refused, with "run `jarvis vex setup` again".
- After the extension updates, its version folders change, and you re-run
  setup.

**Detection.**
- A project is a VEX project when its root has
  `.vscode/vex_project_settings.json` that parses within 64 KB to the
  schema's v2 shape: platform `V5`, language `cpp` or `python`, slot 1–8.
- The VEX view shows name, language, slot and SDK version.
- A malformed file is reported, never guessed around.
- A PROS `project.pros` reads "PROS projects aren't supported".

**Build** (C++ only; a Python project has nothing to build).
- **The project must live on a Windows drive (`/mnt/<drive>/…`)**, because
  the toolchain is Windows-only and `cmd.exe` cannot work in a
  `\\wsl.localhost` folder. Your `override` does.
- It runs as a **program terminal**. This is a small WP-C extension: the
  existing `Terminal.spawn(argv, env)`, with an argv that **only `vex.py`
  builds**.
- The argv is `make.exe` plus `P=<name>`, `T=<SDK, as a Windows path>`, each
  tool as `CC=…`, `CXX=…`, `OBJCOPY=…`, `SIZE=…`, `LINK=…` and `ARCH=…`, and
  `PRINTF_FLOAT=1` if the settings say so. Make lets command-line variables
  override the makefile's own `CC = clang`, so nothing touches PATH.
- The cwd is the project, which interop translates to `C:\…`.
- **No route takes a path, a command or an argument.** The only input is
  `{action: build | rebuild | clean}`.
- When it finishes, `vex.py` parses `file:line:col: error|warning:` lines from
  the terminal's ring into the errors list. It reports the `.bin` path, size
  and time, and the exit code.
- **One build per project at a time**, under a lock file shared with
  `vexbuild` (V-2). There is a 5-minute timeout.
- **Spike:**
  - the build must match your VS Code build's log (elf text size 56,288 B for
    the same source);
  - confirm the `P=` naming the extension uses (its elf was
    `override_87867D`);
  - **confirm that stopping the Linux-side interop process ends `make.exe` and
    `clang.exe` on Windows.** If it doesn't, stop by the Windows PID we
    started, read from `tasklist.exe` and filtered to that PID only.

**Download: only your click, from the VEX view.**
- Slot is a picker of 1–8, defaulting to the project's slot.
- Name is a text box, defaulting to the project name. It is validated as
  `^[A-Za-z0-9_][A-Za-z0-9_-]{0,19}$`: it never starts with `-`, so it can
  never read as a flag, and has no character the Windows command line could
  misread. The spike confirms the brain's length limit.
- For C++, if the build is older than the sources, it builds first.
- The argv is `vexcom.exe --name <name> --slot <n> --write <.bin as a Windows
  path> --progress`, the flags DishPy uses. For Python, `--write` takes
  `src/main.py` (the spike confirms whether VEX's Python download takes
  another flag).
- It runs as a short program terminal, with its progress shown in the panel.
- The button says what it does: **"Download to slot 3: replaces whatever is
  in slot 3 on the brain."**
- **There is no "run after download"** (V-3). Starting the program stays a
  physical act at the brain or controller, by someone who can see the robot.
- **There is no keyboard shortcut and no palette entry.** A keystroke should
  never send code to a robot.

**The brain terminal.**
- It is a program terminal running `vexcom.exe --user`, which streams the
  brain's user port both ways. DishPy's `terminal` command uses `--user`.
- It shows `printf` and `print` output, and what you type goes to the
  program's input.
- There is **one at a time**, and it reattaches after a reload with its ring.
- **WP-F's switch applies.** "Jarvis can read" is on by default (robot
  telemetry), and `terminal_read` can read it under W-2's refusals. **No agent
  ever writes to it.**
- If VS Code's VEX terminal holds the port, it shows vexcom's own error ("port
  busy"). Download pauses the brain terminal and resumes it afterwards, unless
  the spike shows the two ports coexist.

**Device info.**
- A **Read device info** button runs vexcom's info option, with a 10-second
  timeout.
- The output is shown as text: brain name, team number, VEXos version and
  battery, as far as vexcom reports them.
- The flag comes from `vexcom --help`, which you run once. The spike records
  it in design §18.
- **Cut:** VEXos, controller and Python-VM updates, erase, brain rename, team
  number, battery medic and screen grab. They are rare, some are destructive,
  and VS Code does them.

**New VEX project.**
- A dialog asks for:
  - name;
  - language (C++ or Python);
  - template. The list comes from the templates folder in `vex.json`: the
    `cpp_v5_vsc_*.zip` and `py_v5_vsc_*.zip` VS Code templates, with display
    names from `index.json` where it names them.
  - location, which defaults to the new-project folder (V-4).
- The dialog shows the **exact target path**.
- **Create goes through the existing project-creation path.**
  - `folders.check_project_folder` decides, then the **`project_folder`
    approval card**, the same one B2 raises from Discord: exact path,
    Approve/Deny, never Always.
  - Then `folders.make_project_folder`, one `os.mkdir`.
  - The confirmation handler moves out of `discord/project_commands.py` into a
    surface-neutral `projects.confirm_folder()` that both Discord and the HUD
    call. `folders_check`'s grep then names that one function: still a single
    caller of `make_project_folder`.
  - An existing empty folder is adopted silently, as now (O5).
- **The template is unpacked by Jarvis's code, never by a tool.**
  - Every zip entry is checked: no absolute paths, no `..`, no drive letters,
    no symlinks, at most 200 entries, 1 MB each and 10 MB in total.
  - `.v5code` entries are skipped.
  - Each file is written through `editor_policy` and `_write_bytes` into the
    new, empty folder.
- **Then the project settings are written.**
  - Jarvis writes `.vscode/vex_project_settings.json` in the extension's v2
    shape: name, `V5`, language, slot 1, `sdkVersion` (from the SDK folder
    name in `vex.json`) and the extension's version. So the project also
    opens in VS Code.
  - Then `projects.create_project(root=…)`, and the project appears in the
    sidebar.

**USB: how the daemon reaches the brain** (decision V-1).

| | **Windows `vexcom.exe` through WSL interop** (recommended) | usbipd-win passthrough plus Linux `vexcom` |
|---|---|---|
| Setup | None beyond what VS Code already did. The drivers are installed | Install usbipd-win (admin). `usbipd bind` per device (admin). **`usbipd attach --wsl` every time the brain is plugged in or reboots.** Load `cdc_acm`; add yourself to `dialout` |
| Sharing | Windows keeps the brain, so VS Code and VEXcode still work, one app per port at a time | While attached, Windows apps cannot see the brain |
| Firmware updates or brain reboots | Transparent | Re-enumerates and detaches: attach again |
| What runs | A Windows exe at a fixed, protected path, the same pattern as `jarvis hud` launching Chrome and the `sw` wrapper | A Linux binary from the same download |
| Paths | Arguments are converted to Windows paths by one tested function | Native |
| Stop | Must end the Windows process (spike) | Normal signals |
| Agents | The same reach either way: any process running as you can drive a plugged-in brain, exactly as today | Same |

CLAUDE.md's "nothing under `/mnt/` ever runs" rule is about resolving the
Claude and Codex CLIs. VEX is a deliberate, narrow exception:
- fixed paths that you recorded;
- in a protected file;
- checked at each use;
- run only from argv tables in `vex.py`.

**Agents and VEX** (decision V-2). The recommendation:
- **Build: yes, as an ordinary gated command.** `jarvis vex setup` offers to
  write **`~/.local/bin/vexbuild`**, a three-line wrapper like `sw`.
  - It runs `python -m jarvis.v2.vex build [--clean]` in the current folder,
    with the same argv table and the same lock.
  - It builds only, and can never touch the device.
  - Its stem is `vexbuild`, so the permit asks unless you allowlist exactly
    this tool.
  - With it, Claude can compile, read the errors and fix them: your
    intellisense loop.
  - Claude workers run unsandboxed on WSL, so interop works for them. Codex
    workers may not reach interop from their sandbox (spike).
- **Download and run: never.**
  - There is no tool, MCP tool, CLI or Discord verb.
  - Any agent command naming `vexcom` or `vexcom.exe` is **DENY** in
    `rules.py`, so no approval card can offer it.
  - Stated plainly, as `rules.py` says of its DENY list: this is token
    matching, an accident guard, not a boundary. A renamed copy is not
    caught. The real boundary is that download exists only as your button.

**Routes.** Every one is `owner_only` plus a strict Host, on the HUD
listener.

| Route | Body → answer |
|---|---|
| `GET /vex/status` | `{configured, hint, vexcom_version, toolchain, sdks, extension_version}` |
| `GET /projects/{id}/vex` | Detection result |
| `POST /projects/{id}/vex/build` | `{action}` → `{build_id, terminal_id}` |
| `GET /vex/builds/{id}` | `{state, exit_code, errors[], bin{path,size,mtime}}` |
| `POST /projects/{id}/vex/download` | `{slot, name}` → `{terminal_id}`; 409 while a device action runs |
| `POST /vex/device/info` | `{}` → `{text}` |
| `POST /vex/brain` | `{}` → `{terminal_id}` (the existing one if open) |
| `GET /vex/templates` | `[{id, language, label}]` |
| `POST /vex/projects` | `{name, language, template, location?}` → raises the `project_folder` card; the new project arrives as usual |

**Device actions** (download, info, and the brain terminal's user port) share
one lock in `vex.py`.

**Logging** is lifecycle only: build or download started and ended, the exit
code, the slot, and the project by name. Build and terminal output stay in the
ring and your window, as WP-C's terminals do.

### 2.10 Keyboard

| Key | Does | Notes |
|---|---|---|
| **Space** | Types a space in any typing target; push-to-talk only elsewhere | The headline fix |
| Ctrl+S | Save | Chrome's dialog is suppressed |
| Ctrl+P | Quick open | |
| Ctrl+Shift+P, F1 | Command palette | Includes Monaco's actions when an editor is focused |
| Ctrl+Shift+F | Find in files | |
| Ctrl+F, Ctrl+H, Ctrl+G, Ctrl+/, Ctrl+D, Alt+click, Ctrl+Alt+↑/↓ | Monaco's | Find, replace, line, comment, multi-cursor |
| Ctrl+K chords | Monaco's | |
| **Ctrl+B** | Folds the sidebar, **in the editor too, unless it is a Ctrl+K chord's second key** | Decided (E-10). VS Code's VEX extension binds Ctrl+B to build; the HUD does not follow that |
| Ctrl+Alt+B, Ctrl+= / - / 0, Ctrl+Alt+1–4, Ctrl+\` | HUD's (WP-A, WP-D) | Unchanged |
| Ctrl+PageUp / Ctrl+PageDown | Previous / next editor tab | Chrome may keep these even in app mode. Confirm live; the palette's "Go to editor" is the fallback |
| Ctrl+W, Ctrl+T, Ctrl+N, Ctrl+Tab | Chrome's; a page cannot take them | `beforeunload` guards Ctrl+W over dirty buffers |
| Escape | Monaco's (closes widgets) | Under a card the editor is blurred and read-only, so Escape is the card's |
| — | **No key downloads to the robot** | A deliberate click only |

**Every HUD-level key is inert while a card is up.** The palette, quick open
and the VEX dialogs close when a card appears, and the VEX buttons are
disabled under it.

### 2.11 Bundle and performance

**Today:** the editor chunk is 2.29 MB, the worker 232 KB and the HUD index
431 KB. Monaco loads only when a File or Diff view first opens, and that stays
true.

| Piece | Size (minified or raw) | When it loads |
|---|---|---|
| Monaco core plus curated features | about 3.3–3.6 MB (0.57's full feature set is about 3.8 MB) | First file or diff view |
| A Monarch definition | 2–30 KB each | When a model of that language opens |
| `editor.worker` | about 250 KB | With Monaco |
| Language-service workers (TS, JSON, CSS, HTML) | **0: not shipped** (saves about 6 MB) | Never |
| web-tree-sitter JS plus wasm | about 156 KB plus 210 KB | First file in a covered language |
| Grammars | 0.006–3.4 MB each, about 11 MB in total on disk | Each only when its language first opens |
| Highlight, injection and fold queries | about 100 KB of text | With their grammar |

- **Everything is served from loopback**, so transfer is cheap; what costs is
  compile time. The biggest grammar, C++, compiles in roughly 50–150 ms, off
  the main thread in the syntax worker.
- **`tests/face/hud_bundle_check.py`** reads `hud/dist/assets` after a build.
  It fails when:
  - the first-paint chunk grows by more than 50 KB without a note;
  - the editor chunk passes 4 MB;
  - any grammar is missing from the build-time map;
  - anything would load from outside the origin.
- **Runtime:**
  - parsing and querying run in the worker;
  - Monarch time-slices on the main thread;
  - stat polling is one request every 2 s for at most 100 paths.

## 3. Work packages

Each package is a separately mergeable PR with free tests, and updates
`docs/hud-api.md`, design §18 and CLAUDE.md in the same PR.

| WP | What | Size | Risk | Depends on |
|---|---|---|---|---|
| **ED-1** | Monaco 0.57, curated features and Monarch, no language workers; Space, Ctrl+S, Ctrl+B and card fixes; overlay fence; diff model leak | M | Medium | WP-A merged |
| **ED-2** | Buffers, tabs, content-hash save, change polling, conflict bar, `editor_policy`, `owner_only` file routes | L | **High** | ED-1, WP-A |
| **ED-3** | Graphite editor scheme: token table, chrome pins, contrast and hue tests | S | Low | ED-1 |
| **ED-4** | HUD CSP on every response; HUD served from a protected installed build (C-1) | M | Medium | WP-E (same headers); WP-D recommended first, so its socket is checked under the CSP |
| **ED-5** | Tree-sitter colour: syntax worker, grammars, vendored nvim queries, injections, budgets, Monarch fallback | L | Medium–high | ED-1, ED-3; ED-4 recommended first |
| **ED-6** | Syntax folding and sticky scroll from `folds.scm` | S | Low | ED-5 |
| **ED-7** | Quick open, find in files, palette, explorer ops, add to chat, status bar | M–L | Medium | ED-2 |
| **ED-8** | Editor settings, `.editorconfig`, vim (if E-11) | S–M | Low | ED-2 |
| **ED-9** | Git gutter and explorer git colours | M | Low | ED-2, ED-3 |
| **VX-1** | VEX backend: `jarvis vex setup`, protected `vex.json`, detection, build, download, info and brain runners as program terminals, routes, `vexbuild`, the DENY rule | L | **High** | WP-C |
| **VX-2** | VEX view, brain and build panel tabs, errors list | M | Medium | VX-1, WP-A, WP-D, ED-2 |
| **VX-3** | New VEX project from templates, through the `project_folder` path | M | Medium | VX-1, VX-2 |

**Order.**
1. ED-1 as soon as WP-A merges. VX-1 can start beside it, after WP-C.
2. ED-2 and ED-3 next.
3. ED-4 with or after WP-E.
4. ED-5 and ED-6.
5. VX-2 and VX-3 once WP-D lands.
6. ED-7, ED-8 and ED-9 in any order.

**ED-1: Monaco 0.57, the editor's missing half** (frontend only)
- **Files:**
  - `hud/package.json` (`monaco-editor` 0.57.0, exact);
  - `lib/monaco.ts`: the entry points and curated list of §2.2, `.h` as C++,
    a `toml` id, the link opener; `languageFor` removed;
  - new `lib/editorkeys.ts` and its test (`isTypingTarget`, `chordPending`);
  - `Editor.tsx`: models by URI, `jarvis-diff:` models disposed, read-only and
    blurred while blocked, a Ctrl+S command;
  - `FileTab.tsx`: a dirty check before switching files or reloading;
  - `App.tsx`: push-to-talk ignores typing targets, `blocked` reaches the
    editors;
  - `Layout.tsx` and `layout.ts`: the chord rule;
  - `theme.css`: `isolation: isolate`;
  - `vite.config.ts`.
- **Tests:**
  - `editorkeys.test.ts`: typing targets, including nested Monaco DOM;
    Ctrl+K then Ctrl+B passes to Monaco while a lone Ctrl+B folds; repeat and
    composition are ignored.
  - `hud_v2_check.file_checks` gains, each written to fail against main:
    - a Python line carries two or more distinct `mtk` token classes;
    - Ctrl+F opens `.find-widget`;
    - Ctrl+Space shows a word suggestion;
    - **typing `a b` leaves `a b` in the buffer and
      `window.__hud.capture.ptt` null**;
    - Ctrl+S sends the PUT with its keydown `defaultPrevented`;
    - after five diff files, at most two diff models remain
      (`window.__hud.monacoModels()`);
    - Ctrl+B in the editor folds the sidebar, but Ctrl+K Ctrl+B does not;
    - **no request goes off loopback**, by a catch-all route;
    - no `ts.worker`, `json.worker`, `css.worker` or `html.worker` request.
  - `hud_v2_layout_check` gains, at 100% and 160%: with Monaco's context menu
    and suggest open, a card that arrives is topmost at each of its buttons;
    the editor is blurred and read-only; Escape denies; a typed `y` does not
    land.
  - The existing `_monaco_zoom_checks` still pass.
- **Risk:** medium, from the five-minor upgrade. A spike first settles
  EditContext and the Vite worker paths. **Every existing HUD suite must pass
  unchanged.**

**ED-2: buffers, tabs and the guarded save** (backend and frontend)
- **Backend:**
  - `hud_api.py`: GET adds `etag`, `abs`, `encoding`, `eol` and `readonly`;
    PUT takes `expected_etag` and refuses to overwrite non-UTF-8 bytes; new
    `POST …/files/stat`; **every file route becomes `owner_only`** (decided,
    E-7);
  - new `jarvis/v2/editor_policy.py`;
  - `docs/hud-api.md`.
- **Frontend:**
  - new pure `lib/buffers.ts` and its test;
  - new `state/buffers.ts`;
  - new `components/EditorTabs.tsx` and `components/ConflictBar.tsx`;
  - the file pane (tabs, editor and tree);
  - `lib/workspace.ts` (`PaneSpec.editors`);
  - `App.tsx` (`beforeunload`, polling);
  - `api.ts`;
  - `tests/face/hud_v2_mock.py`.
- **Tests:**
  - `hud_backend_check.py`:
    - a mismatched etag is 409 and leaves the file byte-identical;
    - **a rewrite that restores the old mtime with `os.utime` is still a 409**
      (it fails against the mtime guard);
    - a `touch` alone is not a conflict;
    - create with `null`, refused when the file exists;
    - with a temp HOME as the Inbox root: `.ssh/id_test` withheld,
      `.claude/.credentials.json` withheld, and a temp `ALLOWLIST_PATH`
      read-only with its PUT refused;
    - a Latin-1 file is read-only, its PUT refused, its bytes unchanged;
    - the stat cap;
    - 403 without an Origin and on the API listener.
  - `buffers.test.ts`: every state transition, and dirty-after-undo.
  - Headless, after WP-A:
    - two panes share a buffer;
    - a view switch keeps the edit;
    - a clean file reloads with a toast;
    - a dirty file brings the bar;
    - Compare, Keep mine (sends the *disk* etag) and Take theirs;
    - closing the last dirty tab asks;
    - `beforeunload` is armed only while dirty.
- **Risk:** high. The single-pane, single-file path must render as today
  first.

**ED-3: the Graphite editor scheme** (frontend)
- **Files:**
  - new `lib/editorTheme.ts`: the tables of §2.5 as data, Monaco theme
    `rules` for Monarch tokens and for the semantic-token legend, and the
    chrome `colors`;
  - `theme.css`: the `--syn-*` tokens;
  - `lib/monaco.ts`.
- **Tests:**
  - `editorTheme.test.ts`:
    - every syntax colour is ≥ 4.5:1 on `--bg-1` and `--bg-2`;
    - **no syntax colour is in the amber or red hue band**;
    - every chrome key that Monaco defaults to orange, gold or amber is
      pinned;
    - every legend type has a rule;
    - every colour is a strict hex.
  - Headless:
    - a Python and a C++ sample show each class of token in its expected
      computed colour;
    - the approval card's computed colours are unchanged with the editor open.

**ED-4: the HUD's CSP and installed build** (backend, CLI and frontend
build)
- **Files:**
  - `hud_api.py`: `binary()` always sends the CSP on HUD and API listeners;
    serving from `config.HUD_DIR`, or `hud/dist` under `JARVIS_HUD_DEV=1`;
  - `daemon.py`;
  - `config.py` (`HUD_DIR`);
  - `jarvis/__main__.py` (`jarvis hud install`: build, copy, hash manifest);
  - `tools/files.py` (`_protected_state` gains `HUD_DIR`; a SELF_PROTECTED
    hunk, reviewed by you);
  - `tests/face/hud_v2_mock.py`, which sends the same CSP so **every headless
    suite runs under it**.
- **Tests:**
  - `hud_backend_check`: the exact header on `/`, `/assets/*.js`,
    `/assets/*.wasm` and JSON, on both listeners; the dev flag; the newest
    build chosen; a symlinked or out-of-tree asset refused.
  - `files_check` and `permissions_check`: `HUD_DIR` refused to every agent
    write path. v1 and v2 sets stay equal.
  - Headless, every existing suite green under the CSP, including WP-D's
    terminal socket.
  - A deliberate inline `<script>` in a mock page is blocked
    (`securitypolicyviolation` fires).
- **Risk:** medium. A CSP breaks quietly, which is why the whole headless
  suite runs under it.

**ED-5: tree-sitter colour** (frontend)
- **Files:**
  - `hud/package.json` (pinned `web-tree-sitter` 0.27.0 and the grammar
    packages of §2.6);
  - `hud/vendor/tree-sitter/` (Markdown wasm, manifest with SHA-256,
    licences);
  - `hud/src/syntax/queries/<lang>/{highlights,injections}.scm`, vendored
    with nvim-treesitter's `LICENSE` and a `NOTICE`;
  - `hud/scripts/vendor-queries.mjs` (dev-run);
  - new `lib/syntax/{worker,client,positions,flatten,legend,grammars}.ts`;
  - `lib/monaco.ts` (providers and `'semanticHighlighting.enabled': true`).
- **Tests (vitest, Node, free):**
  - **every bundled query compiles against its bundled grammar**;
  - each grammar's wasm matches the manifest or lockfile hash;
  - `positions` round-trips with astral characters, CRLF and tabs;
  - `flatten` resolves overlapping captures as the tree-sitter highlighter
    does;
  - **an incremental property test**: 300 random edit sequences, where
    incremental colouring equals a fresh parse's colouring;
  - the predicate translator on the patterns it meets, and its list of
    dropped patterns;
  - the budgets: a 1.5 MB file stays on Monarch, and a cancelled first parse
    falls back.
- **Tests (headless):**
  - a C++ file shows `--syn-fn` on a function call and bold on `void`;
  - a Markdown fence in Python is coloured as Python;
  - a 1.2 MB file says "syntax: basic";
  - **under the CSP, every grammar loads** (no `securitypolicyviolation`);
  - the loader refuses a URL outside its map.
- **Risk:** medium–high. Query and grammar version drift is caught by the
  compile test, and the UTF-16 unit by the positions test.

**ED-6: syntax folding** (frontend)
- **Files:** vendored `folds.scm`; `lib/syntax/folds.ts`.
- **Tests:** fold ranges for a C++ class, a Python function and a Markdown
  section; sticky scroll shows the enclosing function.

**ED-7: quick open, find in files, palette, explorer, status bar** (backend
and frontend)
- **Backend:**
  - `GET /projects/{id}/files`;
  - `POST /projects/{id}/search` on a core extracted from `tools/search.py`,
    which `grep_files` also uses;
  - `POST /projects/{id}/fs`;
  - all `owner_only`.
- **Frontend:**
  - `lib/fuzzy.ts` and `lib/commands.ts` with their tests;
  - `QuickOpen`, `Palette`, `SearchView`, `EditorStatus`;
  - the explorer menu;
  - "Add selection to chat".
- **Tests:**
  - **`grep_files` and the route refuse the same protected files** (parity);
  - quick open lists dotfiles but never a credential folder;
  - fs refuses protected names, escapes and gate state;
  - delete lands in a temp trash with a `.trashinfo`;
  - the palette is inert under a card, has no download entry, and Space in it
    is not push-to-talk;
  - add-to-chat sends nothing.

**ED-8: settings and `.editorconfig`** (frontend)
- **Tests:**
  - garbage in any setting falls back to its default;
  - the font family is validated;
  - `.editorconfig` sets indentation and never rewrites on save;
  - vim's `:w` uses the guarded save, if E-11 is yes.

**ED-9: git gutter** (backend and frontend)
- **Tests:**
  - HEAD text refused for withheld names;
  - the Myers diff checked against a reference over random inputs;
  - the gutter follows unsaved edits.

**VX-1: the VEX backend**
- **Files:**
  - new `jarvis/v2/vex.py`: settings parsing, the argv tables, Windows path
    conversion, error parsing, the device lock, the template unpacker;
  - `jarvis/v2/terminals.py`: program terminals, created only by `vex.py`;
  - routes in `hud_api.py` and `daemon.py`;
  - `config.py` (`VEX_PATH`);
  - `jarvis/__main__.py` (`jarvis vex setup`, which offers the `vexbuild`
    wrapper);
  - `tools/files.py` (`vex.json` in `_protected_state`, reviewed by you);
  - `rules.py` (the `vexcom` DENY; SELF_PROTECTED, reviewed by you);
  - `projects.py` (`confirm_folder`, moved from the Discord handler).
- **Tests:** a new free suite, `tests/v2/vex_check.py`.
  - **A fake `vexcom` and a fake `make`** are scripted with `python3 -I` and
    named by a temp `vex.json` under a temp HOME. Ports are ephemeral. **No
    device, and the real tools never run.**
  - **Exact argv**, asserted for build, clean, download and the brain
    terminal, including Windows path conversion.
  - **Slot and name refusals:** 0, 9, `-x`, `a b`, `a;b`, Unicode, 21
    characters.
  - **Routes:** no Origin, the API listener, 8403 and a body carrying `path`
    or `args` are refused.
  - **Configuration:**
    - a `vex.json` path outside the VS Code locations, relative, or a
      symlink is refused;
    - `vex.json` is refused to every agent write tool, with v1 and v2 equal.
  - **Rules:** an agent command naming `vexcom.exe` is DENY (no card is
    raised), while `vexbuild` is ASK.
  - **Build:**
    - errors are parsed from a fixture log;
    - one build per project;
    - the timeout kills the fake.
  - **Device actions:** download refused while one runs; one brain terminal.
  - **Templates:** zip-slip, absolute paths, symlinks, oversize and
    too-many-entries are all refused; the settings file written matches the
    schema's v2 shape.
  - **New project:** goes through `project_folder` (raises the card; Deny
    creates nothing); `folders_check`'s grep still finds a single caller.
  - **Leaks and reach:**
    - no bus record and no output in the daemon log;
    - **no tool, MCP tool or fast-path tool names `/vex`, `vexcom` or the
      runners** (the `archive_check` pattern).

**VX-2: the VEX view** (frontend)
- **Files:**
  - `components/VexView.tsx`;
  - `lib/vex.ts` and its test (detection view model, name validation
    mirroring the server's);
  - WP-D panel tabs for brain and build;
  - `Workspace.tsx` (the `vex` view);
  - `api.ts`;
  - `hud_v2_mock.py` (`/vex/*`).
- **Tests (headless; sockets faked with `route_web_socket`):**
  - the view appears only for a VEX project;
  - Build shows ✓ or the errors list, and an error click opens the file at
    its line;
  - **Download needs a click on the labelled button, and no key or palette
    entry triggers it**;
  - every VEX button is disabled under a card;
  - the brain terminal reattaches after a reload;
  - device info renders as text, and markup in it is inert.

**VX-3: new VEX project** (backend glue and frontend dialog)
- **Tests (headless):**
  - the dialog shows the exact path;
  - Create raises the card, and Deny leaves nothing behind;
  - Approve creates the project, which appears in the sidebar;
  - the template list comes from the mock's templates folder;
  - a Python template yields `src/main.py` and the settings file.

**Test rules for every package:**
- Free only: no paid call, and no real device.
- Temp `HOME` and temp `config.*_PATH`, `VEX_PATH` and `HUD_DIR`. Never your
  real `~/.config/jarvis`, `~/.local/share/jarvis` or `/mnt/c`.
- Ephemeral ports, with 8402, 8403 and 8405 refused for HTTP and WebSockets.

## 4. Decisions

### 4.1 Decided (the owner, 2026-10-09)

- **No intellisense.** "Claude code is my intellisense."
  - No language servers, LSP client, Problems-from-servers or `jarvis lsp`.
  - No AI ghost text.
  - No symbol index or breadcrumbs.
  - Read here as also: **no TypeScript, JSON, CSS or HTML language workers**,
    so no JSON schema checks or TS squiggles. Say if you want JSON
    validation back.
  - Kept: Monaco's own editing and word suggestions (§2.2).
- **Graphite only.** One editor scheme, with no picker and no theme import
  (§2.5).
- **Highlighting is tree-sitter, the Neovim way.** Monarch is the fallback
  (§2.6).
- **No general extension support.** Instead, native VEX: build, download,
  brain terminal, device info and templates (§2.9).
- **E-7:** every editor file route is HUD-only, reads included.
- **E-8:** credential folders and Claude's and Codex's login files are
  withheld. Gate-state files stay readable and read-only.
- **E-9:** auto-save off, format-on-save off.
- **E-10:** Ctrl+B folds the sidebar, except as a Ctrl+K chord's second key.
- **Accepted defaults (items 12–16, 20–22):**
  - Monaco 0.57.0, pinned exactly;
  - one buffer per file;
  - no hot exit;
  - polling every 2 s;
  - no force-save;
  - find in files respects `.gitignore`, with a toggle;
  - the cuts (replace in files, Emmet, user snippets, hot exit, code lens,
    debugger, extensions);
  - no "Open in VS Code" button.
- **Prettier cut**, re-checked (§2.8).

### 4.2 Still open

**E-11. Vim keybindings?**
- **Recommended:** yes, as a setting, if a spike shows `monaco-vim` 0.4.4
  works on Monaco 0.57. You mentioned vim and Neovim, and `:w` would use the
  guarded save.
- Alternative: cut.

**V-1. How does the HUD reach the brain?**
- **Recommended:** the Windows `vexcom.exe` your VS Code extension downloaded,
  run through WSL interop from the fixed path in `vex.json`. There is no
  setup, and Windows keeps the brain, so VS Code and VEXcode still work.
- Alternative: usbipd-win passthrough plus the Linux `vexcom` that ships
  beside it. That means an admin install, binding each device, and attaching
  again after every plug-in or brain reboot, and Windows loses the brain while
  it is attached.

**V-2. May agents build or download?**
- **Recommended:**
  - build yes, through a `vexbuild` command that only compiles and is asked
    like any command unless you allowlist it;
  - download and run never: no tool of any kind, and any agent command naming
    `vexcom` refused outright.
- Alternatives:
  - no agent build either;
  - agent download behind an approval card that names slot and program.

**V-3. "Run after download"?**
- **Recommended:** no. Starting the program stays something done at the brain
  or controller by someone who can see the robot.
- Alternative: a separate "Run" button behind a second confirmation, never
  combined with download.

**V-4. Where do new VEX projects go?**
- **Recommended:** a Windows folder, default
  `/mnt/c/Users/johnw/Documents/Robotics/<name>`. VEX's toolchain is
  Windows-only and builds only on a Windows drive, and VS Code can open the
  project there too.
- Alternative: `~/jarvis-work/<name>` on the Linux side. That needs V-5's
  alternative to build.

**V-5. Which C++ toolchain?**
- **Recommended:** the extension's Windows toolchain through interop. Its
  builds match VS Code's byte for byte.
- Alternative: Linux `clang` plus `binutils-arm-none-eabi` and `make` from apt
  (you run the `sudo`). Builds would also work in WSL folders, but they could
  differ from VEX's own, so a spike would have to compare them first.

**C-1. Serve the HUD from a protected, installed copy?**
- **Recommended:** yes. `jarvis hud install` copies a build to
  `~/.local/share/jarvis/hud/`, and agent file tools cannot write there. An
  agent editing Jarvis then no longer changes the running approval window,
  but you run one command after each HUD change. Shell writes remain the
  accepted gap.
- Alternative: keep serving `hud/dist` from the repo, which is agent-writable
  today.

### 4.3 Defaulted (say if you want otherwise)

1. A CSP on every HUD response (§2.7): `'wasm-unsafe-eval'` and no
   `'unsafe-eval'`.
2. Highlight queries come from nvim-treesitter (Apache-2.0), with each
   grammar's own queries as the fallback where a query won't compile.
3. `.h` and `.c` are coloured as C++.
4. Parse errors are not underlined.
5. Files over 1 MB use Monarch.
6. The Markdown grammar's `.wasm` is vendored with its hash, since npm's has
   none.
7. Makefiles and `.mk` stay plain.
8. Importing VEXcode `.v5cpp` files is cut.
9. VEXos, controller and Python-VM updates, erase, rename and battery medic
   are cut; use VS Code.
10. The brain terminal is readable by `terminal_read` by default, through
    WP-F's switch, and never writable by an agent.
11. Download pauses the brain terminal unless the spike shows the ports
    coexist.
12. `.editorconfig` sets indentation and never rewrites a file on save.

## 5. Verification

**Free suites, extended per package:**
- `cd hud && npx vitest run && npm run build`: `editorkeys`, `buffers`,
  `editorTheme`, `syntax/*` (including the query-compile test), `fuzzy`,
  `commands` and `vex`, plus the existing `layout` and `workspace` tests.
- `uv run python tests/face/hud_v2_check.py`, run under the CSP, with
  `hud_v2_layout_check` first and the live-port guard on HTTP and
  WebSockets.
- `uv run python tests/face/hud_bundle_check.py` after a build.
- `uv run python tests/v2/vex_check.py`.
- `tests/v2/hud_backend_check.py`, `tests/v2/archive_check.py` (the no-tool
  pattern), `tests/files_check.py` and `tests/permissions_check.py` (the
  protected sets).
- `tests/rules_check.py` (the `vexcom` DENY).

**Test IDs the headless suites drive:**
- **Editor:** `editor` (`data-language`, `data-syntax`: `tree` | `basic`,
  `data-readonly`); `editor-tabs`; `editor-tab-<path>` (`data-dirty`,
  `data-preview`); `editor-save`; `editor-readonly-reason`.
- **Conflict:** `editor-conflict` (`data-state`); `conflict-compare`,
  `conflict-keep`, `conflict-take`.
- **Navigation and status:** `quickopen`, `palette`, `search-view`,
  `search-result-<n>`, `editor-status`, `add-to-chat`, `editor-settings`.
- **VEX:**
  - `vex-view` (`data-configured`);
  - `vex-build` and `vex-build-status` (`data-state`); `vex-error-<n>`;
  - `vex-slot`, `vex-name`, `vex-download`;
  - `vex-info` and `vex-info-text`;
  - `vex-brain` and `panel-tab-<id>` (`data-kind`: `brain` | `build`);
  - `vex-new`, `vex-new-path`, `vex-new-template-<id>`.
- **Test hooks:** `window.__hud.monacoModels()`, `window.__hud.buffers()` and
  `window.__hud.syntax()` (per-model engine and timings), for tests only.

**By hand, live** (`jarvis hud`; device steps need you and the brain):
1. Open a `.cpp` file in `override`. `void` is bold, `driveFor` is glacier,
   strings are sage, comments are grey italic, and the status bar reads
   "syntax: tree". Type spaces: they appear and the mic stays shut.
2. Ask Claude to edit a file you have open and clean: it reloads. Edit it
   yourself, then ask again: the bar appears. Compare, merge one line, Keep
   mine. `git diff` shows exactly your merge.
3. Open a 1.5 MB file: it says "syntax: basic" and scrolls smoothly.
4. Run `jarvis vex setup`: it lists the tools by name and version and writes
   `vex.json`. Then try `echo x >> ~/.config/jarvis/vex.json` through a Claude
   file edit: it is refused.
5. **Build `override`** in the VEX view. The text size matches VS Code's log
   for the same source. Break a line: the error appears, and clicking it opens
   the file at that line.
6. **Download to slot 2 as `jarvis-test`.** The brain lists it, and nothing
   runs on its own. Then start it on the brain, and the brain terminal shows
   its output. `tasklist.exe | findstr vexcom` is empty after Stop.
7. Ask Claude to "download to the robot". It has no tool, and a `vexcom`
   command is refused without a card.
8. Run `vexbuild` from a Claude thread: it asks (or runs, if allowlisted), and
   its errors come back to Claude.
9. **New VEX project → C++ competition template.** The card shows the exact
   path, and Approve creates it. It builds, and it also opens in VS Code's
   extension.
10. Raise an approval while the editor, the VEX view and the brain terminal
    are focused. Everything goes inert, Escape denies, and focus returns.
11. In DevTools on the HUD, `document.querySelector('meta')` shows nothing,
    but the response headers carry the CSP. An inline `<script>` added by
    hand is blocked.
12. Confirm which of Ctrl+P, Ctrl+PageUp and Ctrl+PageDown the app window lets
    the page take, and record the answer in design §18.
