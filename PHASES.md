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
Arch + Hyprland + guest tools, snapshotted read-only — full Omarchy
install deferred to Phase 7, this phase proves the invisible-desktop
mechanism); per-VM overlay; boot with no local display; Hyprland session
up; `grim` screenshot readable off-VM; SSH provisioned with key auth;
`peek` endpoint proven (listening, never opened); **nothing ever appears
on the host session**.

Accept: a scripted proof run producing the screenshot artifact, the SSH
 transcript, and a signed checklist (no host window, compositor up,
 screenshot legible, peek works, teardown clean).

## Phase 2 — Terminal seat + seat reset

Spec: headless seat profile from the same base (no compositor, lower RAM);
`reset` reverting a seat's overlay to golden without releasing it.

Accept: terminal seat boots/clones/pushes/destroys; wedged-seat reset
demonstrated (break it on purpose, reset, show clean).

## Phase 3 — Prove work export (host-side, no PAT)

Spec: no PAT anywhere — the guest never holds credentials. Install `git`
in the seat image; agent commits on a task branch in the guest (guest
`git status --porcelain` must be clean before export, forcing everything
into commits); host pulls a `git bundle` over SSH, verifies it, applies it
to its own checkout, and pushes with the host's own credentials; verify
pushed SHA == `ls-remote` before destroy; failed export holds the VM for
recovery instead of deleting.

Accept: hermetic proof (bundle → host bare repo, SHA match) plus one live
push of a `phase3-proof` branch to GitHub and remote deletion afterwards;
a simulated export failure demonstrably preserves the VM.

## Phase 4 — Scheduler + manager daemon

Spec: capacity pool with per-type min/max from config; atomic seat
claiming; fair queue with positions; leases + independent heartbeat
channel with auto-reclaim; dynamic admission (live free-RAM check minus
headroom floor); mandatory libvirt CPU/RAM caps + overlay disk quotas;
sqlite state surviving daemon restarts; destroy-on-release. Autostart
policy (learned Phase 1): golden bases never autostart — only the manager
starts seats, so a host reboot never resurrects a 4 GiB guest outside
manager authority.

Accept: concurrency tests (over-claim refused, queue order fair, dead
lease reclaimed, daemon restart reattaches, release destroys); resource
limit tests (fork-bomb/memory-hog contained).

Phase 3 audit deltas (binding on the daemon design):
- **Content gate before push (must-have).** Phase 3 proved export
  faithfully pushes whatever the guest committed — including a commit
  that deleted nearly the whole tree. The daemon must enforce diffstat
  sanity limits, protected paths, and/or approval for destructive
  exports before anything reaches a remote.
- **Lock scope + ordering:** per-VM *and* per-repo locks with a defined
  order across export vs reset vs release vs concurrent exports (the
  update-ref CAS fix in Phase 3 closed the instance; the daemon needs
  the general mutex).
- **Quarantine-before-trust:** fetch to `refs/omavroom/*` staging, then
  `git fsck`, pack/blob/size caps, and the content gate. Bundle
  integrity proves self-consistency with a guest-advertised SHA, not
  benign-ness — the guest chooses the SHA.
- Base prerequisite (host checkout must contain the bundle base or fall
  back to fuller bundles); `git stash list` must be empty (porcelain
  misses stashes); submodule policy decided; guest host keys pinned
  (no TOFU); branch names validated (`git check-ref-format` + allowlist,
  fully-qualified refspecs); remote-is-truth retry semantics; export
  provenance logged (consider host-signed tag on push).
- Snapshot lineage rule: `term-git` is a disposable Phase 3 convenience;
  Phase 7 golden automation installs git in the base and collapses the
  fork. Per-seat identity regeneration (machine-id + SSH host keys)
  required before multi-seat snapshotting.
- Disposition: Phase 3 proof commit `2bb6cb1` remains publicly reachable
  by SHA on GitHub despite branch deletion (GitHub serves unreferenced
  objects indefinitely) — accepted as a benign public test artifact
  (4-line text file, no secrets); "GC will clean it" is struck.

Phase 4B1 deltas (binding, from the real LibvirtProvisioner audit):
- **`list_vms` scope.** The provisioner returns managed seat domains only
  (`omavroom-seat-*`); the `omavroom-base`/`omavroom-term` templates are
  never returned, because `reconcile` destroys any unreferenced VM it
  sees. The 4A "all domains" wording is superseded.
- **Overlay quota limitation.** A qcow2 overlay must span the golden's full
  virtual disk and qcow2 has no per-image quota. `overlay_max_gb` is a
  sparse-aware allocated-bytes monitor/refuse check at provision and
  ready, not a hard block-layer quota. True per-seat disk quota (guest
  filesystem project quota / in-guest enforcement) is deferred to Phase 7.
  CPU/RAM caps remain hard (`cputune`/`memtune`).
- **Exec transport.** `LibvirtProvisioner.run` (pinned-key, blocking SSH)
  and `agent_exec` (qemu-guest-agent) are the real exec primitives;
  Phase 5's `exec_*` wraps `run` on a worker thread.
- **Manager-mediated export needs branch/ref plumbed** through
  `export_seat`/`release_seat` (4B2), or release-with-export is unusable
  outside direct provisioner calls.
- Golden ownership note: libvirt dynamic DAC relabel can chown read-only
  golden files to `libvirt-qemu`; content is untouched and mode stays 444,
  but rebuild-with-force then needs care.

## Seat lifetime (added after the first real agent test)

The first end-to-end agent test exposed that agents had to babysit
heartbeats: an agent doing GUI work for ~5 minutes never called
`heartbeat`, the daemon reclaimed the seat (`reason=heartbeat_timeout`) and
destroyed the VM under it. Decisions:

- The **MCP server auto-heartbeats** seats the session holds; heartbeat
  timeout now only detects a dead agent/MCP process.
- A stale seat enters **stasis** (`held`): VM and overlay preserved,
  reason recorded, listed under Needs attention. Nothing destroys a stale
  seat except an explicit `force_discard`/`retry_release` or
  `lease.held_ttl_s > 0` (discard without export; default 0 = keep).
- On stasis, an **automated gated export** runs if a host export intent was
  recorded (explicit `export_seat`); otherwise hold for inspection.
- Export intent is a host repo path; a `prepare_repo` clone URL is never an
  export target.

## Phase 5 — MCP server + opencode wiring

Spec: FastMCP server exposing the async toolset (`pool_status`,
`request_seat`/`seat_status`, `exec_start`/`exec_poll`/`exec_kill`,
`heartbeat`, `screenshot` downscaled, `input`, `peek_*`,
`release_seat`); nothing agent-facing blocks; wired into opencode as an
MCP server and exercised by a real agent task.

Accept: an agent completes a hello-world-grade task end-to-end through MCP
tools only (request → exec → screenshot → release), with a long-running
exec polled (not blocked) mid-phase.

Status: COMPLETE (2026-09-18). Exec engine (per-exec workers, bounded
output, race-free kill), FastMCP server (27 tools, protocol v1), daemon
autostart, docs/mcp.md, and opencode wiring. Proven end-to-end over MCP
stdio on a real terminal seat: request → ready (14s) → exec_run returned
the guest's real output → terminal-seat screenshot correctly refused →
release destroyed the VM. Human must restart opencode to load the MCP
server.

## Phase 6 — CLI + TUI companion

Spec: `omavroom status [--watch]`, `screenshot`, `peek`, `destroy`,
settings commands; metadata-only Textual TUI (ASCII slot layout,
attachments, seat state, queue — no framebuffer contents), usable over SSH.

Accept: full VM lifecycle drivable from CLI alone; TUI renders correct
state against a live manager, including queued waiters.

Status: COMPLETE (2026-09-18). `omavroom` CLI (status/seats/queue/request/
screenshot/peek/release/reset/retry-release/force-discard/destroy/events/
admission/settings/tui/daemon) over protocol v1, plus a metadata-only
Textual TUI (settings-derived fixed slot grid, sticky no-reflow teardown,
queue sidebar, needs_attention, daemon-down recovery, SSH/Alacritty-safe).
425 tests.

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

Status: PHASE 8A COMPLETE (2026-09-18). PySide6/QML `omavroom-gui`
(+ `omavroom gui`): responsive packed grid of permanent settings-derived
slots, desktop thumbnails polled on a worker thread, terminal exec-activity
text, sticky no-reflow teardown, queue sidebar, needs_attention with
recovery actions, settings + admission override, desktop-only click-to-peek,
interruptible non-blocking shutdown. 470 tests. Remaining for Phase 8B
before this phase fully closes: editable settings (min/max/timeouts),
in-GUI image management, and the Phase 9 add-ons below. PySide6 is
currently a core dependency (648 MB) — move to an optional `gui` extra in
Phase 10.

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

## Remaining work (backlog)

Consolidated list of what is left, carried review advisories, and loose
ends. Nothing here blocks day-to-day use. Phase specs/statuses are above;
this is the single backlog of record.

### Phase 7 — Golden automation + multi-image (L, largest remaining)
- [ ] `golden build --profile stock|mirror`: `mirror` is plumbing only today;
      `stock` = Omarchy repo install, `mirror` = install Omarchy + copy this
      host's package set/config
- [ ] Durable guest identity: first-boot machine-id/DUID regeneration in the
      golden (removes the shared-identity window; non-urgent now that static
      IPs fixed the collision)
- [ ] Automate the ESP restore (the Omarchy install rewrites it; it broke boot
      during the hand build)
- [ ] Named images + default-per-seat-type + pin a request to a non-default
      image; in-GUI image management
- [ ] Herder workspace → image mapping

### Phase 8B — Command Center completeness (M)
- [ ] Editable settings: min/max seats, timeouts, images (currently
      display-only; `[gui]` knobs + admission override are editable)
- [ ] In-GUI image management (list/build/set-default)
- [ ] Needs-attention: show the stasis reason and add an inspect action
      (peek/screenshot)
- [ ] Move PySide6 from core deps to an optional `gui` extra (648 MB)

### Phase 9 — Visual polish (M/L)
- [ ] Easing animations throughout (queue→slot glide, power on/off, state
      transitions)
- [ ] Live boot/install/init streaming on the monitor
- [ ] Per-monitor CRT shader
- [ ] Cadence: skip unchanged frames, adaptive throttle

### Phase 10 — Docs + release (M)
- [ ] Install guide, capacity-tuning guide, example agent workflows,
      Herder-binding guide, contributor notes
- [ ] Versioned GitHub release
- [ ] Clean-machine install test (fresh account, ends in a Phase 1 proof)

### Quick wins / loose ends (S)
- [x] Stale docs from the collision fix: the `omavroom` skill and the
      omarchy-pomodoro `AGENTS.md` still say "one seat at a time until the
      collision is fixed" — it is fixed
- [ ] `omavroom_input`: support modifier combos (`Super+Return`,
      `Ctrl+Alt+t`) — `input` only sends single keys or literal text today, so
      agents cannot trigger Omarchy shortcuts. Map Super/Win→`logo`,
      Ctrl/Alt/Shift, and emit `wtype -M … -k … -m …`.
- [ ] Expose `cancel_request` as an MCP/CLI tool (agents cannot withdraw a
      queued request today)
- [ ] Type the stale-heartbeat error (`heartbeat` on a closed lease returns a
      raw `KeyError: no active lease` instead of a clean `not_found`).
- [ ] MCP client version handshake: report the client's version and have the
      daemon warn when a client predates a capability (e.g. auto-heartbeat), so
      an un-restarted opencode is obvious. Document "restart opencode after
      upgrading omavroom" (MCP servers are loaded per opencode process, not per
      session).
- [ ] `omarchy-fcitx5` user-service restart loop in the golden (log spam)
- [ ] Re-run the pomodoro agent test end-to-end as the regression (seats no
      longer die; a dead agent should now land in Needs attention)

### Carried advisories (from reviews; none gate-blocking)
- Stasis: auto-export runs inline on the pump thread (a hung remote can stall
  reclaim) — med
- Stasis: no per-session lease ownership (any session referencing a seat
  renews it; no auth model) — med
- Stasis: `held_ttl_s>0` discards without export (documented, untested) — low
- VNC live: no backoff before re-trying a failed endpoint each poll (churn) —
  med
- VNC live: no staleness watchdog (a silent-but-open socket freezes the tile)
  — med
- VNC live: `stop()` can join up to 2s on the GUI thread — low
- VNC live: unbounded server-controlled RFB reads (localhost only) — low
- Concurrency: static IP pool overlaps libvirt's DHCP range (duplicate risk
  with non-seat VMs) — med
- Concurrency: allocation TOCTOU (only matters with multiple daemons) — low
- Concurrency: frozen `static_ip` vs a later subnet config change — low
- Capture A: status cadence is now tied to `wall_interval_s` (document) — low
- Ops: quickshell "no network backend" warning (no NetworkManager by design)
  — cosmetic
- Testing: desktop-seat screenshot/input only partly integration-tested at
  full RAM — low

### Suggested order
1. Quick wins (stale docs, `cancel_request`, fcitx5)
2. Re-run the pomodoro regression (confirms seat-lifetime in the real flow)
3. Phase 7 `golden build` (mirror profile) — last big functional gap
4. Phase 8B, then 9, then 10
