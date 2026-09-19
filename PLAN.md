# omavroom

A room full of disposable Omarchy VMs where coding agents can work without
ever touching my desktop session.

The name: Omarchy + VM + room.

## The problem

I run coding agents (opencode and friends) on this same machine I work on.
When an agent needs to launch, poke at, restart, or stress-test an
application — especially anything graphical — its windows grab focus, its
test keystrokes land in whatever document I'm typing, and my session becomes
a shared input device with something that doesn't know I exist.

I want agents to get *their own computer*: one they can break, reboot, fill
with test data, and eventually throw away — while my session stays mine.

Hard requirement: **an agent VM must never open a window on my host session.
No focus stealing, no keyboard interception, ever.**

## The core idea

The VM runs its own display server, but the host never renders it.

- QEMU runs as a background process with `-display none`; the guest's screen
  exists only as a framebuffer accessible via VNC/SPICE sockets.
- On the host's Wayland session, nothing appears, nothing takes focus, and
  my keyboard never reaches the VM. The VM's inputs are its virtual devices.
- The guest still runs a *real* Omarchy desktop (Hyprland, waybar, the
  omarchy bar), so anything graphical is genuinely rendered — it's just
  rendered into a framebuffer nobody is watching.

### How agents "see" the GUI

- `hyprctl` inside the VM reports true geometry: layer positions, bar size,
  monitor resolution, workspace state.
- `grim` takes screenshots inside the VM; the agent reads the PNG itself
  (agents are multimodal). Same loop a human would use — launch, poke,
  screenshot, edit, relaunch — with the picture delivered as a file instead
  of to a monitor.
- Typing/clicking when needed happens via `wtype`/`ydotool` or VNC input,
  inside the VM only.
- When *I* want to look, `omavroom peek <vm>` attaches a viewer on demand.
  Disconnect, and it goes back to being invisible. Great for reviewing an
  agent's work or debugging what it did.
- Worst case, `omavroom destroy <vm>` kills a flaky or runaway VM and my
  session was never part of the blast radius.

### Discipline rule

The agent never launches GUI apps on the host, only inside the VM via SSH.
That single invariant is what keeps my session untouched.

## What omavroom is

A small central VM manager with an MCP interface.

- Two seat types:
  - `desktop` — full Omarchy guest with its own Hyprland session; for
    graphical work (e.g. building an omarchy plugin, watching where the bar
    actually renders).
  - `terminal` — headless; SSH in and do builds/tests/terminal stuff.
- A capacity pool: this machine declares a budget (starting point: 4 units —
  a `desktop` costs 4, a `terminal` costs 1; tune after measuring real RAM
  use. 15 GB total with ~11 GB already in use means roughly one desktop seat
  today). Admission is dynamic — the manager checks actual free host RAM
  minus a configured headroom floor at claim time and refuses when there is
  no room — with static budgets, seat costs, and overrides settable in
  config. Static values bound, live measurement admits. (Decision: the
  settings expose a min/max seat count per seat type — graphical and
  terminal — so the operator decides how resources split between agent
  lanes instead of the scheduler imposing one policy.)
- Agents request a seat, do their work, and release it. Seat claiming is
  atomic (no "check then claim" races); if no seat is free, requests queue
  fairly and wait for the next release.
- Leases + heartbeats: a crashed or abandoned agent's seat is reclaimed
  automatically.
- Releasing a seat **destroys** that disposable VM — the next agent gets a
  fresh machine, never someone's dirty leftovers.
- Work is preserved by host-side export *before* release: the agent
  commits on a task branch in the guest, the host pulls a `git bundle`
  over SSH, applies it to its own checkout, and pushes with the host's
  own credentials. The VM holds zero GitHub credentials — no PAT to
  create, scope, rotate, or leak. If export fails, the VM is held for
  recovery instead of deleted.

MCP is the right interface for this: opencode (and other agent hosts) speak
it natively, so any agent can call tools like:

- `pool_status` — what seats exist, what's busy, who's queued
- `request_seat` — returns a pending request (queues if full); `seat_status`
  follows it through queued → provisioning → ready
- `exec_start` / `exec_poll` / `exec_kill` — long-running commands stream
  output as a capped ring buffer; nothing agent-facing blocks
- `heartbeat` — lightweight lease renewal on its own channel, independent
  of execs, so long commands never look like dead agents
- `screenshot` — grab the guest framebuffer as a PNG (desktop seats;
  downscaled to protect agent context budgets)
- `input` — inject keystrokes/clicks inside the guest (desktop seats)
- `peek_url` / `peek_attach` — let *me* watch when I want to
- `release_seat` — kill execs, verify work is exported, then destroy

The manager is the only component that talks to libvirt/QEMU; agents only
ever see the MCP tools. The agent itself runs on the host (opencode); the
VM is purely an execution sandbox, so LLM API keys never enter disposable
guests.

## Machine fit (this box)

- Bare metal, `/dev/kvm` present, 12 cores, ~835 GB free disk — plenty.
- 15 GB RAM (~11 GB already in use) is the real limit: ~1 desktop seat
  today. A RAM upgrade raises the seat count more than any software change.
- Golden qcow2 base image (Arch + Omarchy preinstalled) + per-VM overlay
  images: boot in seconds, teardown = delete the overlay.
- virtio-gpu/virgl gives the guest Hyprland real OpenGL acceleration
  without GPU passthrough — enough for bar/layout work.

## Existing prior art (why we're building our own)

- **microsandbox** — libkrun microVMs, MCP server, secrets stay host-side.
  Closest general match, but headless-oriented; no real Omarchy desktop.
- **SmolVM** — Firecracker/QEMU/libkrun VMs for agents with forwarded git
  credentials; also headless-oriented.
- **E2B / Beam beta9 / Mitos** — agent sandbox platforms; E2B self-host is
  experimental, Mitos wants Kubernetes.
- Avoid Daytona (went closed-source in June 2026).

Nothing off-the-shelf spins up literal Omarchy desktop VMs and keeps them
off my screen, so omavroom is a thin custom layer over libvirt/QEMU +
cloud-init, with a scheduler and an MCP server in front. libvirt (not
hand-rolled QEMU processes) is the control plane: lifecycle, DHCP leases,
`virsh screenshot`, cgroup CPU/RAM limits, and reattaching to running VMs
after a daemon restart.

## Build order

1. **Prove the invisible desktop.** One Omarchy VM: boots with `-display
   none`, runs Hyprland, screenshots via `grim` come back readable, SSH
   provisioning works, `peek` works, nothing ever appears on my session.
2. **Prove the terminal seat.** Same golden image, headless profile.
3. **Prove work export.** Commit on a task branch in the VM, pull a `git
   bundle` to the host over SSH, apply and push with host credentials,
   verify SHA, then destroy. (Decision: no PAT at all — host-side export.
   The VM never holds a token, which also moots the planned credential
   proxy: there is no secret left to proxy.)
4. **Scheduler + pool.** Atomic seat claiming, fair queue, leases with
   heartbeats, auto-reclaim of dead leases, destroy-on-release.
5. **MCP server.** Expose the tools above; wire it into opencode.
6. **Golden image automation.** Scripted build of the base image (cloud-init
   + Omarchy install), stored locally on this machine, so it's reproducible
   and shareable. (Decision: golden image lives locally — qcow2 base kept
   read-only, per-VM copy-on-write overlays. v1 is hand-built and
   snapshotted: the slow layer — OS, Omarchy, desktop, guest tools
   (hyprctl/grim/wtype) — is baked in, while per-VM uniqueness (hostname,
   machine-id, SSH host keys, user key, PAT, repo clone) is injected at
   provision time in seconds. Fully scripted builds come later. Multiple
   named images come later, managed from the Command Center.)
7. **Polish for sharing.** Install docs, capacity tuning guide, example
   agent workflows.

Steps 1–3 are the risk-reduction core; everything after is straightforward
engineering.

## Command Center (proposed, not yet built)

An opt-in local dashboard window — the VMs never force windows open; this
is a window *I* choose to keep on the side.

- **Monitor wall.** Slot counts come from settings (e.g. max 3 graphical +
  max 2 terminal) and every slot is always rendered — slots never appear
  or disappear on VM lifecycle events; a torn-down VM leaves its slot in
  place showing "off / no signal". Within that, the layout is a responsive
  grid that packs all slots to fill the window pleasingly: graphical tiles
  large (~16:9 live thumbnails), terminal tiles compact (live text views).
  No rigid lanes with permanent empty holes; no reflow that moves occupied
  slots when a VM exits. Window resizes may rescale/repack (user-initiated,
  expected); VM exits only ever turn a screen off in place.
- **Labels on each monitor.** Agent name, repo/project, seat type, elapsed
  time, lease/heartbeat state. Agents self-report a label when requesting
  a seat (required field).
- **Terminal seats** get a monitor too: a live text view of the guest's
  terminal output (polled over SSH — e.g. a tmux/scrollback snapshot or a
  streaming tail), in the same fixed slot with the same label treatment.
  No framebuffer needed — the "screen" is the terminal.
- **Queue sidebar.** The scheduler already knows who's waiting (agent
  label, project, requested seat type, position, wait time) — the sidebar
  just renders it. Shows who's next and what each waiter wants; when a
  seat frees, the handoff is visible: the waiter leaves the sidebar and
  its slot's monitor comes alive.
- **Click to peek.** Clicking a live slot opens the full viewer; closing it
  returns the VM to invisible.
- **Settings.** Capacity budget, seat costs, lease/heartbeat timeouts,
  default images per seat type, PAT storage (system keyring).
- **Golden image management.** List local images, build new ones from the
  scripted flow, set defaults. Multiple named images supported here.
- **Thumbnails, technically:** poll each running VM's framebuffer
  (VNC/SPICE screenshot) every ~1–2 s, downscale, display. Cheap and
  decoupled from the guest.
- **TUI companion (decided).** Alongside the native GUI, a Textual-based
  terminal view with exactly one job: monitoring over SSH. By design it
  shows no framebuffer contents — just the monitor layout in ASCII, which
  agent/project is attached to each slot, seat state, and the queue
  sidebar. Metadata-only on purpose: no image-protocol dependency, so it
  works in any terminal (Alacritty included) and stays light over SSH.
  (Terminal image protocols — Kitty graphics, Sixel, iTerm2 inline —
  were evaluated and rejected for this view; they would buy thumbnails at
  the cost of emulator dependence.) The native GUI remains the primary
  Command Center and the only place that renders monitor contents. Both
  read the same manager API.
- **Build it last, thin, native.** The Command Center is a real desktop
  application (explicitly not a web page) and a view over the manager's
  local API — not the manager itself. Build the headless manager + MCP +
  CLI first; the dashboard comes after as a native client (likely a Python
  GUI toolkit, given the daemon choice; Qt6 vs GTK4/libadwaita TBD).

## Visual polish (final milestone, after everything works)

Explicitly last: make it beautiful once it's functional.

- Smooth animations with easing throughout: waiters gliding out of the
  queue sidebar into their slot, monitors flickering on at boot, state
  changes (booting → ready → busy → tearing down) expressed visually, not
  just as text.
- The monitor shows the VM's actual boot/install/init streaming live as it
  happens — watching a machine come alive in its slot.
- CRT shader per monitor so each slot reads as a physical screen.
- Rationale: polish makes the tool feel approachable and intuitive, which
  is what gets people to actually use it (Grocbot-style precedent).
- Toolkit implication (noted, not decided): this level of animation and
  per-monitor shader work favors a toolkit with real animation + shader
  support — among the Python candidates, that points more toward Qt6/QML
  than GTK4/libadwaita. Revisit when the Command Center milestone starts.

## Herder integration (proposed)

Note: Herdr (the terminal workspace manager for coding agents) calls its
project containers **workspaces**, not spaces — one workspace per repo,
task, or investigation, owning tabs and panes, with agent states
(working/blocked/done/idle) rolling up per workspace.

- The idea is sound: bind a Herdr workspace to a Command Center image, so
  seats spun up for that workspace use the right golden image.
- Keep authority split: omavroom owns VM/seat truth; Herdr owns
  pane/agent-state truth; join on the workspace name passed as the seat
  label. v1 is a config map (workspace name or pattern → image) plus
  showing the workspace name on the monitor card.
- Deeper integration (querying Herdr's socket API/CLI to show live agent
  state next to each VM, or driving Herdr panes from omavroom) is possible
  — Herdr documents a CLI + local socket API — but deferred until the core
  VM path works.

## Decisions log

- Golden image: stored **locally** on this machine (qcow2 base + overlays).
- Git auth: **no PAT** — host-side export via `git bundle` over SSH; the
  guest never holds credentials. (Supersedes the earlier v1-PAT decision:
  push-from-guest was a leftover from before the host-side-agent topology
  was locked; host-driven export fits that topology and removes the
  secret entirely.)
- Seat lifetime: **agents never babysit heartbeats.** The MCP server
  refreshes the lease on the agent's behalf for any seat the session is
  using; a heartbeat timeout therefore only means the agent process/MCP
  actually died, never "the agent was busy for five minutes".
- Stasis, not teardown: a stale seat is **preserved** (state `held`) with a
  reason, never destroyed. It surfaces in Needs attention with
  retry-release (export → destroy) and force-discard. `lease.held_ttl_s`
  (`0` = keep until an operator acts) is the only automatic teardown, and it
  discards without export — a capacity-vs-work-loss trade-off.
- Automated exfiltration: on entering stasis, if a host export intent was
  recorded (an explicit `export_seat`), the normal gated export runs
  automatically and the outcome is recorded; with no intent the VM is held.
  Export intent is a **host repo path**, never a clone URL.
- Daemon language: **Python** (FastMCP server; fastest path to a working
  proof; clean QEMU/MCP boundary preserved in case of a later rewrite).
- Command Center: accepted as the post-core UI milestone — a **native
  desktop application** over the manager's local API; fixed-slot "monitor
  wall" metaphor adopted; terminal seats get live terminal-output
  monitors; queue sidebar rendering scheduler state. GUI toolkit TBD
  (Qt6 vs GTK4/libadwaita — polish milestone leans Qt6/QML).
- Visual polish (animations, live boot stream, CRT shader): final
  milestone, after core + Command Center.
- TUI companion: metadata-only Textual app (layout + labels + queue, no
  framebuffer contents) for SSH monitoring; native GUI stays primary.
- Agent topology: **host-side** — opencode runs on the host, VM is purely
  an execution sandbox.
- Control plane: **libvirt**, not raw QEMU.
- Capacity: dynamic admission (live free-RAM check minus headroom floor)
  with static config budgets and manual overrides; static bounds, live
  measurement admits.
- MCP API is async-first (`exec_start`/`exec_poll`/`exec_kill`,
  pending seat requests, independent `heartbeat`); nothing blocks.
- Per-seat CPU/RAM caps are mandatory and hard from day one (`cputune`
  quota + `memtune`). Overlay disk is different: a qcow2 overlay must span
  the golden's full virtual disk and qcow2 has no per-image quota, so
  `overlay_max_gb` is a monitor/refuse check on measured allocated bytes,
  not a hard cap. True per-seat disk quota (guest filesystem project
  quota or in-guest enforcement) is deferred to Phase 7.
- Golden image v1: hand-build + snapshot (slow layer baked, per-VM fast
  layer injected at provision); scripted builds later.
- Seat split: user-configured min/max seat counts per seat type
  (graphical vs terminal) in settings; no hardcoded scheduler policy.
- License: **MIT** (see LICENSE).

## Repository

- Remote: <https://github.com/washburnello/omavroom> (public)
- License: MIT (see LICENSE)
