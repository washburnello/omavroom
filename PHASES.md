# omavroom build phases

## Operating mode: manager-gate loop

Three standing subagent roles, one manager (me):

- **Builder** — implements the phase spec. Returns what changed + how to run it.
- **Tester** — writes and runs tests/proof scripts against the implementation.
  Returns pass/fail with evidence (logs, artifacts). Never trusts the
  Builder's word.
- **Reviewer** — audits implementation + test results against PLAN.md and the
  phase acceptance criteria. Returns sign-off or a gap list.

**Gate rule.** A phase exits only when all four hold: implementation
complete per spec, tests written and passing with evidence, Reviewer
sign-off, and my own verification (I read the code and re-run the key
checks myself). Any failure → targeted fix task → re-verify. No phase is
skipped, no gate is waived. Loop until the work meets or exceeds the spec.

Decisions locked in PLAN.md (host-side agent, libvirt, Python, dynamic
admission + static overrides, async MCP API, mandatory seat limits,
user-configured min/max per seat type, MIT) are binding on all phases.

## Phase 0 — Project skeleton

Spec: Python project managed with `uv` (ruff, pytest configured); package
layout for manager, MCP server, CLI (TUI/GUI come later); config schema
with seat min/max per type, host headroom floor, lease/heartbeat timeouts,
exec caps; sqlite state module stub; README quickstart stub; `.gitignore`.

Accept: `uv run pytest` green on skeleton tests; `omavroom --help` works;
config loads with sane defaults.

## Phase 1 — Prove the invisible desktop (risk-reduction core)

Spec: libvirt control plane on this box; golden base image v1 (hand-built
Arch + Omarchy + Hyprland + guest tools, snapshotted read-only); per-VM
overlay; boot with no local display; Hyprland session up; `grim`
screenshot readable off-VM; SSH provisioned with key auth; `peek` attaches
on demand; **nothing ever appears on the host session**.

Accept: a scripted proof run producing the screenshot artifact, the SSH
 transcript, and a signed checklist (no host window, compositor up,
 screenshot legible, peek works, teardown clean).

## Phase 2 — Terminal seat + seat reset

Spec: headless seat profile from the same base (no compositor, lower RAM);
`reset` reverting a seat's overlay to golden without releasing it.

Accept: terminal seat boots/clones/pushes/destroys; wedged-seat reset
demonstrated (break it on purpose, reset, show clean).

## Phase 3 — Prove work export

Spec: clone in VM, commit, push with scoped fine-grained PAT injected at
provision; verify via clean `git status` + local SHA == `ls-remote` before
destroy; failed export holds the VM for recovery instead of deleting.

Accept: end-to-end run ending in a real commit on GitHub from inside a VM;
a simulated push failure demonstrably preserves the VM.

## Phase 4 — Scheduler + manager daemon

Spec: capacity pool with per-type min/max from config; atomic seat
claiming; fair queue with positions; leases + independent heartbeat
channel with auto-reclaim; dynamic admission (live free-RAM check minus
headroom floor); mandatory libvirt CPU/RAM caps + overlay disk quotas;
sqlite state surviving daemon restarts; destroy-on-release.

Accept: concurrency tests (over-claim refused, queue order fair, dead
lease reclaimed, daemon restart reattaches, release destroys); resource
limit tests (fork-bomb/memory-hog contained).

## Phase 5 — MCP server + opencode wiring

Spec: FastMCP server exposing the async toolset (`pool_status`,
`request_seat`/`seat_status`, `exec_start`/`exec_poll`/`exec_kill`,
`heartbeat`, `screenshot` downscaled, `input`, `peek_*`,
`release_seat`); nothing agent-facing blocks; wired into opencode as an
MCP server and exercised by a real agent task.

Accept: an agent completes a hello-world-grade task end-to-end through MCP
tools only (request → exec → screenshot → release), with a long-running
exec polled (not blocked) mid-phase.

## Phase 6 — CLI + TUI companion

Spec: `omavroom status [--watch]`, `screenshot`, `peek`, `destroy`,
settings commands; metadata-only Textual TUI (ASCII slot layout,
attachments, seat state, queue — no framebuffer contents), usable over SSH.

Accept: full VM lifecycle drivable from CLI alone; TUI renders correct
state against a live manager, including queued waiters.

## Phase 7 — Golden image automation + multi-image

Spec: scripted base-image build (as automated as the Omarchy installer
allows); named local images; workspace-name → image mapping for Herder
workspace binding; default image per seat type.

Accept: a fresh image built from script; two images coexisting; a seat
request pinned to the non-default image.

## Phase 8 — Native GUI Command Center

Spec: native app (toolkit TBD, Qt6/QML favored); responsive packed grid of
permanent slots (graphical tiles large, terminal tiles compact); live
thumbnails + live terminal views; queue sidebar; labels (agent, project,
seat, elapsed, lease); click-to-peek; settings (min/max seats, timeouts,
images, PAT via keyring); image management.

Accept: side-by-side run with CLI showing identical state; lifecycle events
never move occupied slots; teardown reads as screen-off.

## Phase 9 — Visual polish (final functional milestone)

Spec: easing animations throughout (queue→slot handoff, power on/off,
state transitions); live boot/install/init streaming on the monitor;
per-monitor CRT shader.

Accept: side-by-side against the Phase 8 build — same state, visibly
better feel; no functional regressions (re-run Phase 8 acceptance).

## Phase 10 — Docs + release

Spec: install guide, capacity-tuning guide, example agent workflows,
Herder-binding guide, contributor notes; versioned release on GitHub.

Accept: a clean-machine (fresh user account on this box) install following
only the docs, ending in a passing Phase 1 proof run.
