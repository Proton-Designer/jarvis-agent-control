# TUI Framework Research

Researched: 2026-08-17/18. Two parallel research passes (verified via web search/fetch, not
recollection — several of these projects moved fast in the past year and stale memory would
have been actively wrong about release status).

## Decision

**Textual.** Decided by the Lead (jlgivc76), 2026-08-18. Full reasoning:

The deciding factor is the backend boundary, not the framework's features. Ratatui and
Bubble Tea are both better engineered for the actual workload (diffed render loop, Bubble
Tea's v2 "Cursed Renderer" built specifically to solve flicker/perf) — but both require
building, versioning, and debugging an IPC layer between the TUI and a Python backend that
already exists and works. Textual imports `providers.py`, `pane_state.py`, and
`orchestrator_has_tools()` directly, as function calls. That's the single largest
simplification available, and it isn't close.

The distribution advantage of a compiled single binary is real but currently hypothetical —
it matters for an audience that doesn't have Python. Ayman has Python, the backend is
Python, there is no other user today. Paying a concrete cost now (a whole IPC surface) for a
benefit that only exists if this becomes a product was rejected.

The Textualize wind-down (see the Textual section below) is the strongest argument against,
and is being accepted consciously as a risk, not overlooked. Mitigation: v8.2.8 shipped two
months before this decision, it's open source, and the app layer is the cheapest thing in
this system to port — if it stalls, the cost is rewriting a few thousand lines of UI against
a backend that never moved, versus the alternative risk of having built an IPC layer that
turned out unnecessary.

Also: the design itself is unproven (three candidate layouts, not yet chosen). Optimizing
the substrate before knowing what the UI needs to do was judged backwards.

**Two findings taken regardless of the framework choice** — see their own sections below,
since they outlive this decision:
- `tmux -CC` control mode, as a push-based alternative to polling `capture-pane`.
- The k9s dual-clock: sample rate and redraw rate are two separate, deliberately chosen
  clocks, not one refresh interval doing both jobs.

Also taken: synchronized-output escape sequences (`CSI ?2026`) for atomic frame painting,
lazygit's lesson about pollers writing into widget state unsynchronized against the render
pass, and lazydocker's logs-plus-controls-in-one-screen as a layout model.

---

## 1. Framework landscape, verified current status (as of Aug 2026)

### Textual (Python) — Textualize / Will McGugan

**Status:** Actively maintained, but the company behind it shrank to a skeleton crew.
Textualize posted ["The future of Textualize"](https://textual.textualize.io/blog/2025/05/07/the-future-of-textualize/)
(May 2025) announcing the company would wind down as a funded business; Textual continues
as a community/maintainer-led open-source project, no longer full-time-funded.
Third-party company-data sources put current headcount at ~2-3 people
([Tracxn](https://tracxn.com/d/companies/textualize/__xWa_U0-hR023fzVEf30z2Nq2jKm2CtyUcgV0TOvusu0)).
Despite that, release cadence has stayed healthy: latest is **v8.2.8** ("The more super
release," June 30 2026), with steady point releases through spring 2026 (v8.2.0 "The Select
Release," Mar 27 2026; v8.2.7 "The more Kitty Release," May 19 2026) —
[github.com/Textualize/textual/releases](https://github.com/Textualize/textual/releases),
confirmed on [PyPI](https://pypi.org/project/textual/).

**Capabilities:** CSS-like styling (`.tcss`), full flexbox/grid-ish layout, reactive
attributes that auto-trigger re-render on data change (ideal for live panels), full mouse
support (click/hover/drag/wheel), true 24-bit color (inherited from Rich), OSC 8 hyperlinks
(Rich has shipped this since 2020). Built-in `Sparkline` widget
([docs](https://textual.textualize.io/widgets/sparkline/)), and `textual-plotext` gives full
live-updating line/bar plots via a reactive `PlotextPlot` widget. "The more Kitty Release"
(v8.2.7, May 2026) suggests active work on Kitty-graphics-protocol image rendering, and the
separate `textual-image` package explicitly renders images via Kitty graphics protocol,
Sixel, and iTerm2 protocols
([PyPI](https://pypi.org/project/textual-image/0.6.3)). Async workers and timers make
background polling (pgrep, tmux queries) straightforward without blocking the render loop.

**Verdict:** Most "batteries included" of the four — best out-of-the-box look, easiest path
to something polished, real plotting widgets, real mouse support. Risk is organizational: a
2-3 person team, not a funded company, so long-term velocity is a bet on one maintainer's
continued interest, not a guarantee. Distribution is trivial (`pip install`/`uvx`, no
compiled binary, no signing concerns at all).

### Ratatui (Rust) — successor to tui-rs

**Status:** Actively and rapidly maintained community project (forked from tui-rs in 2023
after that project stalled —
[orhun/tui-rs-revival](https://github.com/orhun/tui-rs-revival) documents the abandonment
that motivated the fork — real precedent that even a well-regarded TUI project can stall).
Latest: **v0.30.2** (~June 2026), which added a new `Termina` backend; v0.30.1 shipped Jan 23
2026 — [ratatui.rs/highlights/v0302](https://ratatui.rs/highlights/v0302/),
[GitHub releases](https://github.com/ratatui/ratatui/releases). ~14k+ GitHub stars and
1,000+ downstream crates per third-party comparison coverage
([TUI Renaissance 2026, youngju.dev](https://www.youngju.dev/blog/culture/2026-05-14-tui-development-ratatui-bubbletea-ink-textual-terminal-ui-renaissance-deep-dive-2026.en)).

**Capabilities:** Immediate-mode rendering — you own the render loop entirely (works
naturally with `tokio`/async-std: spawn a tick interval + render interval + event stream,
`tokio::select!` over them —
[ratatui.rs async tutorial](https://ratatui.rs/tutorials/counter-async-app/full-async-events/)).
This is a genuine advantage for "poll tmux/pgrep continuously without flicker": Ratatui diffs
the in-memory buffer against the last frame and only writes changed cells to the terminal, so
idle CPU and flicker are both near-zero when nothing changes
([ratatui.rs rendering concepts](https://ratatui.rs/concepts/rendering/)). Full mouse support
via crossterm's `EnableMouseCapture`. True color, built-in `Sparkline` widget, and the
community `ratatui-image` crate unifies Sixel, Kitty graphics protocol, and iTerm2 image
protocols with automatic terminal capability detection (queries the terminal, falls back to
Unicode half-blocks) — explicitly tests against Ghostty in its compatibility matrix
([github.com/ratatui/ratatui-image](https://github.com/ratatui/ratatui-image)).

**Verdict:** Most "advanced" in the literal terminal-protocol-exploitation sense — best
flicker/CPU story, but nothing is free: layout, styling, and the render loop are all your
responsibility (immediate-mode, not a batteries-included app framework). Distribution:
compiles to a single static binary (`cargo install`), trivial one-command install, no code
signing needed since `cargo install` builds locally (no Gatekeeper quarantine).

### Bubble Tea (Go) — Charm

**Status:** Actively maintained, backed by a funded company (Charm) with a broad
complementary ecosystem (Lip Gloss for styling, Bubbles for components, Glamour for
markdown). Charm shipped **v2.0.0 on Feb 23, 2026** — the first breaking-change release in
the project's 6-year history — with a from-scratch "Cursed Renderer" based on
ncurses-style diffing algorithms, described as "orders of magnitude" faster than v1
([byteiota.com writeup](https://byteiota.com/bubble-tea-v2-10x-faster-terminal-uis-for-go-developers/),
[GitHub discussion #1374](https://github.com/charmbracelet/bubbletea/discussions/1374)).
Patch releases have continued steadily since (v2.0.7, v2.0.8 through mid-2026) —
[github.com/charmbracelet/bubbletea/releases](https://github.com/charmbracelet/bubbletea/releases).
40k+ stars, used in production by NVIDIA, Microsoft Azure, AWS, and GitHub tooling per the
same source. Note: a May 2026 third-party comparison blog cited it as "v1.3" — that appears
to be stale/inaccurate against the primary GitHub release history, which is the authoritative
source here.

**Capabilities:** Elm-architecture (Model/Update/View) — a deliberate, opinionated pattern
for live-updating state (very natural fit for "continuously poll external state, update
model, re-render"). Lip Gloss v2 gives true color with automatic terminal-gamut coercion and
native OSC 8 hyperlink support (`Style.Hyperlink()`, degrades gracefully) —
[Lip Gloss v2 discussion](https://github.com/charmbracelet/lipgloss/discussions/506). v2's
renderer added flicker-free Kitty image passthrough and re-enabled Sixel passthrough with DA1
capability detection
([pkg.go.dev/charm.land/bubbletea/v2](https://pkg.go.dev/charm.land/bubbletea/v2)); a
real-world example is TUIOS, a terminal multiplexer built on Bubble Tea v2 + Lip Gloss v2
claiming "near-zero idle CPU" via event-driven rendering
([github.com/Gaurav-Gosain/tuios](https://github.com/Gaurav-Gosain/tuios)). Mouse support
built in. Sparklines/plots aren't in core Bubbles, but the third-party `ntcharts` library adds
Sparkline, Barchart, LineChart, and real-time StreamLineChart/TimeSeriesLineChart widgets
specifically for Bubble Tea
([github.com/NimbleMarkets/ntcharts](https://github.com/NimbleMarkets/ntcharts)).

**Verdict:** Best balance of "advanced terminal features" and "batteries included app
framework," with an actively-funded team behind it and a v2 rewrite specifically targeting
the flicker/performance problem this project cares about. Distribution: single static Go
binary (`go install`), trivial, no signing concerns.

### Ink (Node.js / React)

**Status:** Actively maintained by Vadim Demedes; latest is **v7.1.1** (July 16 2026) —
[GitHub](https://github.com/vadimdemedes/ink). In-progress docs show continued investment:
`kittyKeyboard` protocol option, an incremental-rendering mode, and concurrent rendering
support are recent/upcoming additions.

**Capabilities:** Full React component model — most familiar to web/JS developers, uses Yoga
(Meta's Flexbox engine) for layout, so CSS-flexbox-style layout reflow works well. True color
via Chalk (hex/RGB support). **Gaps relative to the other three, verified directly against
the Ink readme:** no built-in mouse support (requires a third-party addon), no built-in image
rendering (community package `ink-picture` fills the gap, supporting Kitty/iTerm2/Sixel/
Braille/ASCII fallback —
[github.com/endernoke/ink-picture](https://github.com/endernoke/ink-picture)), no built-in
hyperlink widget (community `ink-link` package). React's diffing plus Ink's own incremental-
rendering mode mitigate flicker, but Ink re-renders a React tree rather than diffing a
terminal buffer directly the way Ratatui/Bubble Tea do, so it tends toward a higher
performance ceiling cost for very high-frequency live data (e.g., per-frame sparkline ticks).

**Verdict:** Fastest ramp-up for a JS/React-fluent team and best for "form-like" interactive
CLIs (wizards, prompts), but the weakest of the four for this project's specific asks — mouse
support and image/graphics-protocol support are both bolted-on via third-party packages
rather than native, and it's most associated with quick coding-agent CLIs (this is literally
how Claude Code's own CLI and OpenAI Codex CLI are built) rather than dense live-dashboard
TUIs.

### Emerging framework worth knowing: OpenTUI

A genuinely new entrant not on the original candidate list: **OpenTUI**
([github.com/anomalyco/opentui](https://github.com/anomalyco/opentui)) — a terminal UI core
written in Zig (compiled, GPU-adjacent rendering ambitions, exposes a C ABI) with
TypeScript/Bun bindings and React and SolidJS reconcilers on top. Already powers OpenCode in
production and is slated to power terminal.shop. 13k+ stars, MIT licensed, 1,000+ commits —
active, but young and still explicitly iterating on things like full Kitty/Sixel/iTerm2 image
rendering (open issue tracking that work —
[anomalyco/opentui#92](https://github.com/anomalyco/opentui/issues/92)). Also worth flagging:
`@mariozechner/pi-tui`, a minimal TS TUI toolkit built specifically for AI-agent CLIs with
differential rendering, CSI-2026 synchronized-output atomic screen updates (see the
synchronized-output note below — both Ghostty and Kitty support this), and native
Kitty/iTerm2 inline image widgets — small in scope but a good reference implementation of
"flicker-free by construction"
([npm](https://www.npmjs.com/package/@mariozechner/pi-tui)). Neither is "safe" for a
production dependency the way the big four are — treat as watch-list, not foundation.

### What "most advanced" actually buys you — the one technique worth pulling out on its own

**Terminal synchronized-output escape sequences** (`CSI ?2026h` ... render ... `CSI ?2026l`)
tell a supporting terminal to buffer the whole frame and paint it atomically rather than
repainting cell-by-cell. Traced to a user feature request against bpytop (the Python
predecessor to btop)
([bpytop#327](https://github.com/aristocratos/bpytop/issues/327)) — btop implements it, and
it's directly usable in Textual too, since it's a terminal protocol, not a framework
feature. Ghostty supports it.

### Comparison summary

| | Textual | Ratatui | Bubble Tea | Ink |
|---|---|---|---|---|
| Lang | Python | Rust | Go | Node/React |
| Latest (Aug 2026) | v8.2.8 (Jun 2026) | v0.30.2 (~Jun 2026) | v2.0.8 (Jul 2026, v2.0 shipped Feb 2026) | v7.1.1 (Jul 2026) |
| Maintenance | Active, but tiny team, unfunded | Active, community-driven, growing | Active, funded (Charm) | Active, single maintainer |
| Mouse support | Native, full | Native, full | Native, full | Bolted-on (3rd party) |
| True color | Yes (Rich) | Yes | Yes (Lip Gloss) | Yes (Chalk) |
| OSC 8 hyperlinks | Native (Rich) | Manual/3rd-party | Native (Lip Gloss v2) | 3rd-party (`ink-link`) |
| Kitty/Sixel/iTerm2 images | Yes (`textual-image`) | Yes (`ratatui-image`) | Yes (native in v2 renderer) | 3rd-party (`ink-picture`) |
| Sparkline/live plot | Native `Sparkline` + `textual-plotext` | Native `Sparkline`, 3rd-party charts | 3rd-party (`ntcharts`, incl. real-time streams) | None built-in |
| Flicker/CPU control model | Reactive + async workers | Manual diffed render loop (most control) | Elm-arch + diffed Cursed Renderer | React diffing + incremental mode |
| Install friction | `pip`/`uvx`, no signing issue | `cargo install`, static binary, no signing issue | `go install`, static binary, no signing issue | `npm i`, needs Node runtime |

---

## 2. Language tradeoff, argued honestly (both directions)

### Python/Textual packaging reality
- **`pip install` footprint**: Textual's dependency chain is light (`rich`, `markdown-it-py`,
  `pygments`) — low tens of MB of pure-Python wheels, assuming a compatible interpreter is
  already present ([PyPI: textual](https://pypi.org/project/textual/0.1.18/)).
- **Single-binary bundling is the weak point.** A live Textualize discussion
  ([#4512](https://github.com/Textualize/textual/discussions/4512)) documents PyInstaller
  failing with `ModuleNotFoundError` on Textual's dynamically-loaded internal widgets —
  fixable via manual `hiddenimports`, but real friction, and macOS `.app` bundling for a
  terminal app is reported as unresolved in that community. Nuitka `--onefile` reportedly
  works better out of the box, but no Textual-specific size benchmark could be found
  (unverified). A general (non-Textual) 2026 PyInstaller-vs-Nuitka comparison found Nuitka
  onefile ~22.5MB vs PyInstaller ~26.5MB for an equivalent app, with Nuitka onefile adding
  measurable startup latency (257.9ms vs 152.7ms for the plain script) — a real cost for
  something meant to feel instant
  ([2026 comparison](https://ahmedsyntax.com/2026-comparison-pyinstaller-vs-cx-freeze-vs-nui/)).
- **Code signing/Gatekeeper**: Gatekeeper only inspects files carrying the
  `com.apple.quarantine` xattr, set on browser download. A script run via `python foo.py`, or
  a binary installed via `pip`/`cargo install`/`go install`/`brew`/`curl|sh` rather than
  downloaded as a `.app`/DMG, generally isn't gated. This holds independent of language — it's
  about distribution channel, not runtime
  ([Gatekeeper mechanics](https://hacktricks.wiki/en/macos-hardening/macos-security-and-privilege-escalation/macos-security-protections/macos-gatekeeper.html)).
- **`uv tool install` / `pipx`**: now the accepted "trivial install" path for Python CLIs —
  one command, isolated venv, auto PATH setup; `uv` is ~10-100x faster than `pipx` on cold
  installs and can even fetch a matching Python version. The catch is unchanged: it still
  requires `uv`/`pipx` pre-installed as a bootstrap dependency, and still runs interpreted
  Python on every invocation
  ([uv vs pipx](https://pydevtools.com/handbook/explanation/how-do-uv-tool-and-pipx-compare/)).

### Rust/Ratatui and Go/Bubble Tea packaging reality
- **Verified real binary sizes**: lazygit (Go) ships a **6.6–6.9MB** compressed single binary
  per platform; ripgrep (Rust) ships **~1.76–1.98MB**. k9s (Go) is an outlier at
  **~37–43MB** because it bundles a full Kubernetes client SDK — showing binary size tracks
  what you link in, not the language itself. For a TUI at lazygit's scale, single-digit MB is
  realistic.
- **Install story**: `cargo install`, `go install`, Homebrew formulas, and `curl|sh` scripts
  (via `cargo-dist` / `goreleaser`) are one-line installs placing a working static binary on
  PATH — no venv, no interpreter/version resolution step. Simpler *in kind* than the Python
  path, not just smaller.
- **Gatekeeper**: symmetric with Python — a `brew`/`curl|sh`-installed CLI binary generally
  isn't quarantined either. Not a Rust/Go-specific advantage; both avoid it the same way (skip
  the `.app`/DMG download path).
- **Notable counter-signal**: even a committed Rust-CLI author (`celq`) additionally
  published to PyPI and npm via `maturin`+`cargo-zigbuild`, purely because those ecosystems
  already have installers on more machines — evidence that "meets the user where their
  package manager already is" can outweigh binary purity
  ([Ivan Carvalho](https://ivaniscoding.github.io/posts/rustpackaging1/)).

### IPC patterns for a compiled TUI talking to a Python backend
Ranked roughly by fit:
1. **Unix domain socket + JSON/JSON-RPC** — most commonly recommended for same-host
   TUI↔backend; libraries exist both sides (`tokio-unix-ipc`, `ipckit` targets Rust+Python
   explicitly) ([dvlv.co.uk writeup](https://www.dvlv.co.uk/how-to-do-inter-process-communication-ipc-w-python-and-rust.html)).
2. **Local HTTP/JSON API** (e.g. FastAPI) — heavier but far easier to debug (curl-able);
   general guidance treats this as the simpler default, with raw sockets as the later
   optimization.
3. **stdio JSON-RPC** (subprocess) — simplest to wire, but ties process lifecycle together,
   no multi-client support.
4. **tmux control mode** (`tmux -CC`) is directly relevant prior art for this exact shape —
   emits structured `%`-prefixed events instead of polling. `libtmux` wraps this for Python;
   on the Go side, **`tmux-orchestration`** is a real project explicitly built to "back
   Bubbletea TUIs with robust, programmatic pane/process management" — architecturally
   near-identical to Jarvis's needs. **Honeymux** (Show HN) is another live example of a TUI
   wrapping tmux control mode specifically for agent-driven multi-session workflows
   ([HN thread](https://news.ycombinator.com/item?id=47799791)).

No case-study blog post was found titled exactly "why we chose compiled-TUI-over-IPC vs
same-language," but the tmux-orchestration and Honeymux projects are direct architectural
analogs, just without published rationale.

### Real-world reasoning, both directions
- **Pro-compiled**: recurring argument across
  [Lobsters](https://lobste.rs/s/nyprxb/go_vs_rust_writing_cli_tool)/
  [HN](https://news.ycombinator.com/item?id=24044043) and
  [Smiling Dev's Rust rewrite post](https://smiling.dev/blog/rust-shined-over-python-for-my-cli-tool/):
  distribution is the main win ("hand out a static binary" vs. users fighting
  `pip install --user`), plus fewer runtime bugs caught earlier by the type system.
- **Pro-Python-parity**: not found as an explicit team postmortem, but the standard "don't
  add a language boundary you don't need" argument holds up mechanically — every IPC option
  above is real added complexity (protocol design, versioning, process-lifecycle
  coordination) that a same-process Textual app simply avoids by importing backend modules
  directly.

**Bottom line, as researched (before the Lead's decision above):** both sides are legitimate
and the tradeoff is real, not illusory. Python/Textual gives zero-boundary backend access and
a genuinely one-line install via `uv tool install`/`pipx` *if* a Python-runtime-manager
prerequisite is acceptable, but true single-binary distribution is friction-prone and adds
startup latency. Ratatui/Bubble Tea gives verified single-digit-MB static binaries with a
simpler-in-kind install story, at the cost of building and maintaining a real IPC layer — for
which mature, directly-analogous prior art exists (tmux control mode, `tmux-orchestration`,
Honeymux).

---

## 3. Prior art — TUIs that do this well

**lazygit** (Go, `gocui`/`tcell`) — [repo](https://deepwiki.com/jesseduffield/lazygit).
Flicker fix is explicit in the changelog: v0.64.0's "Synchronize async view rendering" (PR
#5791) serializes writes from concurrent background loaders (git log, file watchers) against
rendering, because unsynchronized goroutines painting into views was producing
partial/interleaved frames — exactly the bug a first-timer hits by having pollers write
directly into widget state. Layout is **persistent multi-panel**: Status/Files/Branches/
Commits/Stash stacked in a fixed left column plus a large main panel, always visible, with a
`ContextMgr` context stack tracking focus. Keybindings are vim-derived with mnemonic single
letters (`s` stage, `d` delete).

**k9s** (Go, `tview`/`tcell`) — [repo](https://deepwiki.com/derailed/k9s). **The most
transferable pattern found in this research.** It does **not** tight-poll. It uses
client-go informers — one long-lived watch connection per resource type, server pushes
deltas into a local cache — and only the cluster-info sidebar is genuinely polled, on a slow
15s interval with exponential backoff to 2 minutes on failure. Separately, on-screen table
repaint has its own `refreshRate` (default 2.0s, hard-coded minimum) that's decoupled from
how often the underlying data actually changes. "Sub-processors" filter/transform raw API
payloads in the background before they hit the renderer, explicitly so navigation stays
responsive during expensive fetches. Layout is **drill-down stack**
(cluster→namespace→resource→pod→logs) via a `PageStack`, a deliberately different model from
lazygit's flat panels — fits hierarchical data. Log viewer: configurable tail (default 100
lines), 1000–5000 line ring buffer, live tail via `sinceSeconds: -1`, ANSI passthrough, and a
documented **autoscroll toggle** so users can scroll back without losing the live tail — with
known gotchas (issues #155, #1608, #3668) about not resetting scroll position on re-engage,
worth designing around upfront.

**btop/btop++** (C++, successor to Python's bpytop specifically for CPU-overhead reasons) —
[repo](https://deepwiki.com/aristocratos/btop). Threading: a dedicated Runner thread owns
data collection + composition so slow syscalls never block input. The synchronized-output
technique (see above) came from a user feature request against bpytop
([bpytop#327](https://github.com/aristocratos/bpytop/issues/327)). Default refresh is
`update_ms=2000`, explicitly documented by the maintainer as chosen for **graph
sample-smoothing, not just CPU savings** — the sharper version of the k9s dual-clock point:
"how often you redraw" and "how often you sample" are a deliberate design choice, not just a
perf knob. Layout uses named presets of toggleable "boxes" cycled via numbered hotkeys rather
than freeform resize.

**lazydocker** (Go, `gocui`, same author/toolkit as lazygit) —
[repo](https://github.com/jesseduffield/lazydocker). Arguably the **closest existing analog
to Jarvis itself**: a dashboard supervising multiple long-running parallel processes
(containers, here: Claude Code sessions) with live logs AND controls in the same screen, not
a separate mode. Container list top-left; dedicated log panel per selected container streams
live output alongside the control actions (restart/stop/exec/remove). CPU/memory shown as
per-container sparkline graphs, user-configurable — a directly reusable idea for a "which
Claude Code session is burning tokens/CPU" panel.

**tmux control mode** (`tmux -CC`) — see the dedicated feasibility section below; also
directly relevant prior art since Jarvis already polls tmux panes today.

**zellij** (Rust) — architecturally relevant even though it's a different product category:
client-server split over Unix domain sockets, server owns all pane/PTY state, clients only
handle input+render, communication via Protocol Buffers. Real-world validation of the
"compiled TUI shells out over IPC to a stateful backend" pattern named in the language
tradeoff section above.

**Cross-cutting pattern taxonomy** (secondary source, one analyst's synthesis —
[hyperbliss.tech, April 2026](https://hyperbliss.tech/blog/2026.04.04_terminal-renaissance/),
weight lower than the primary repos): names three recurring layouts — Persistent
Multi-Panel (lazygit, btop), Drill-Down Stack (k9s), Widget Dashboard (btop, bottom) — and a
four-layer keybinding model (universal arrows/Esc/`q` → vim motions → mnemonic single-key
actions → power commands), plus a three-tier help system convention: an always-visible
footer hint bar, an on-demand `?` full overlay, and separate full docs.

### Gaps flagged by the original researchers (worth knowing, not filled in)
- Exact refresh trigger for lazygit (file watcher vs interval poll) unconfirmed from public
  docs.
- tcell/gocui's internal diff/double-buffering implementation not independently verified from
  primary source in this pass.
- No Textual-specific PyInstaller/Nuitka size benchmark found — only general-Python numbers.
- No primary "why we picked compiled-TUI-over-same-language" postmortem matching Jarvis's
  exact shape — closest analogs (tmux-orchestration, Honeymux) are architectural matches
  without published rationale.

---

## 4. `tmux -CC` control mode — feasibility investigation

Investigated separately from the framework decision above, per the Lead's explicit request,
since it's independent of which TUI gets built and bears directly on L4's existing
`capture-pane`-polling architecture (`pane_state.py`, `transport.py`). **Empirically tested
against a real Claude Code session, not read from docs alone** — same standard as the rest of
this research.

### The decisive question: does control mode carry the ANSI/SGR detail the ghost-text discriminator depends on?

**Yes — confirmed two different ways, empirically:**

1. **`%output` push notifications carry raw bytes, octal-escaped.** The man page states this
   plainly ("value escapes non-printable characters and backslash as octal \xxx"), and it was
   verified live: attached a real control-mode client (via a Python `pty.fork()` harness,
   since `-CC`/`-C` requires a real TTY and fails with `tcgetattr failed` when stdout is
   redirected to a plain file) to a throwaway Claude Code session, triggered real new pane
   output, and captured genuine SGR color codes in the stream:
   `%output %7 \033[?2026h\033[?25l\033[H\015...\033[38;5;153mfizz-*\033[39m...` — real 256-color
   foreground-set/reset codes, octal-escaped exactly as documented.

2. **Sending `capture-pane -e -p` as a COMMAND over the same persistent control-mode
   connection returns byte-identical content to today's CLI-based polling** — same SGR codes,
   *not* octal-escaped this time (only `%output` notification values get escaped; command
   response blocks between `%begin`/`%end` carry raw bytes directly). Verified against a pane
   showing genuine ghost/autosuggest text: the response contained the exact same dim-SGR-wrapped
   text `pane_state.py`'s discriminator already depends on.

### The complication that changes the design: `%output` is a raw write stream, not a screen snapshot

This is the detail that determines how much of L4 could ever move to this. `capture-pane`
gives you tmux's own internally-maintained, already-resolved "what does the screen currently
show" 2D grid. `%output` gives you the raw bytes as the program *wrote* them to the pty —
**and Claude Code's TUI does NOT write linearly.** The captured stream shows real cursor-
positioning sequences interleaved with content: `\033[H` (cursor home), `\033[2C` (forward 2),
`\033[20B` (down 20), selectively repainting only the screen regions that changed. To turn
this raw stream into "what does the screen currently look like, with SGR state per cell" — the
same thing `capture-pane` already hands you for free — a consumer would need to implement (or
adopt a library implementing) a real VT100/xterm terminal-emulator state machine: track cursor
position, interpret every positioning/erase/scroll sequence, maintain a persistent screen
buffer with per-cell SGR state. That's what tmux itself already does internally to produce a
`capture-pane` result. Mature libraries exist for this (Python's `pyte`, Rust's `vte`/
`alacritty_terminal`), but adopting one is a real, non-trivial dependency and engineering
commitment — not a parsing change.

### What was also verified: idle panes are genuinely silent

Attached in control mode to an idle (no activity) pane and captured for 6 seconds: **zero
bytes received.** No heartbeat, no keepalive, no periodic re-send of anything. Confirms the
"wake on activity, otherwise silent" property that makes this valuable as an event source at
all — there is no polling-disguised-as-push happening under the hood.

### Feasibility verdict

**Control mode is not a drop-in replacement for the `capture-pane`-based classifier, and the
Lead's instruction to leave the working classifier alone stands** — reconstructing resolved
screen state from `%output` requires real terminal emulation, which is new engineering
surface with its own correctness risk (a bug in a hand-rolled or newly-adopted VT100 emulator
could reintroduce exactly the kind of misclassification this project has spent real effort
eliminating).

**But there is a genuinely valuable middle-ground design, confirmed feasible by everything
above, that changes nothing about the classifier itself:**

- Open one persistent control-mode connection (instead of one-off CLI subprocess spawns).
- Treat the arrival of *any* `%output` event for a given pane as a **wake signal** — "something
  changed here, worth checking now" — without ever interpreting its content.
- On that signal, send `capture-pane -e -p` as a command over the same persistent connection
  and feed the response into the **existing, unmodified** `classify_pane_ansi()` — identical
  input shape to what it gets today, just event-triggered instead of timer-triggered, and
  without spawning a new subprocess per check.
- Idle panes produce zero events (confirmed), so this eliminates polling overhead precisely
  when it's wasted — the timer-driven approach today burns cycles checking panes that haven't
  changed since the last check; this design only checks when there's something to see.

This is a supplement to the polling architecture, not a replacement for the classification
logic — exactly the distinction the Lead asked to have determined. It would change *when* L4
decides to call `capture-pane`, not *what it does* with the result.

### Sources
- `man tmux`, CONTROL MODE section (local, tmux 3.7b) — the authoritative protocol reference;
  the `%output`/octal-escaping and `%subscription-changed`/format-subscription behavior
  described above are both drawn from this section.
- [tmux Control Mode wiki](https://github.com/tmux/tmux/wiki/Control-Mode) (referenced during
  the earlier prior-art research pass, corroborates the man page).
- Empirical captures performed 2026-08-17/18 against a real, throwaway Claude Code session
  (`claude-cc-test`), using a Python `pty.fork()` harness to give `tmux -C` the real TTY it
  requires (`-CC`/`-C` fails immediately with `tcgetattr failed: Operation not supported by
  device` when given a non-TTY stdout, e.g. a redirected file — worth knowing for anyone
  automating this further).
