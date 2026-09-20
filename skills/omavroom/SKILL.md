---
name: omavroom
description: >-
  Use whenever a task needs a graphical desktop, a display, or a window -
  building or testing Omarchy/Hyprland plugins, bar widgets, quickshell
  components, GUI applications, screenshots, or anything that would otherwise
  launch a window on the user's own session. Provides disposable Omarchy VMs
  (seats) via the omavroom MCP so the user's session is never disturbed. Do NOT
  use for purely headless/terminal work - that is ordinary bash. Use ONLY when a
  real screen (or a clean-room machine) is required.
---

# omavroom: disposable Omarchy machines for agents

The user works on this machine. Launching a GUI app here would steal focus and
interrupt them. Instead, run the work inside a **seat**: a disposable Omarchy VM
with its own display, invisible to the host. You drive it over SSH (exec), see
it via screenshots, and type into it with input. On release the VM is destroyed
and your work is exported.

If the project's seats need a toolchain the base image lacks, read
**`omavroom-project-images`** first - don't install tools by hand in a seat.

## When to use this

- Developing/testing an **Omarchy plugin, bar widget, or quickshell component**
- Running a **GUI app** to verify how it looks or behaves
- A **clean-room build/test** that must not touch the host
- Anything where you would otherwise open a window on the host

Do NOT use it for builds/tests that need no display and no isolation - just run
those with bash.

## The loop

1. **Check the pool** - `omavroom_pool_status`. Note `per_type.desktop` /
   `per_type.terminal`, `builds`, and whether admission needs an override.
2. **Take a seat** - `omavroom_request_seat` with `seat_type="desktop"` (or
   `"terminal"`), `agent_label`, and `project`. Poll `omavroom_seat_status` (or
   `omavroom_wait_for_seat`) until `state=ready`; the payload gives `seat_id`.
   Passing `project=<name>` binds the seat to that project's image.
3. **Work inside it** - `omavroom_exec_run` (short) or `omavroom_exec_start` +
   `omavroom_exec_poll` (long). It's a real SSH shell as `agent` (passwordless
   sudo). There is no fixed exec time limit; use start/poll for long builds.
4. **See it** - `omavroom_screenshot`. Omit `max_width` for a **native
   full-resolution** frame (best for reading a TUI); pass `region=(x,y,w,h)` to
   crop. Take a fresh one after each change.
5. **Type/click** - `omavroom_input`:
   - `{"kind":"text","value":"..."}` - bulk text (fast)
   - `{"kind":"type","value":"...","delay_ms":N}` - per-character typing
     (exercise incremental rendering / as-you-type features)
   - `{"kind":"key","value":"Super+Return"}` - keys and modifier combos
   - `{"kind":"click","value":"x,y[,button]"}` - mouse
6. **Finish** - `omavroom_release_seat`. With `repo`/`branch`, committed work is
   exported (git bundle) back to the host and pushed; without one the VM is
   destroyed.

### Helpers (act like a real user)

- **Windows/apps (desktop):** `omavroom_launch_app` (prefer `omarchy-launch-tui`
  for TUIs), `omavroom_list_windows`, `omavroom_focus_window`,
  `omavroom_resize_window`, `omavroom_move_window`, `omavroom_float_window`.
- **Theme:** `omavroom_set_theme` (live `omarchy theme set`).
- **Clipboard:** `omavroom_clipboard_get` / `omavroom_clipboard_set`.
- **Files in/out:** `omavroom_copy_in` / `omavroom_copy_out` (host side is
  confined to omavroom's transfer dir - the docker-cp equivalent).

## Omarchy specifics

- Both seat types run real Omarchy. Desktop = Hyprland + the quickshell bar;
  terminal = the same system booting to a shell. `omarchy`, `omarchy-shell`,
  `quickshell`, `hyprctl`, `foot`, `grim`, `wtype`, `wl-copy` are available.
- Omarchy plugin/shell dirs: `/usr/share/omarchy/shell` (built-ins) and
  `~/.config/omarchy/`.
- After changing a plugin, reload the shell, then screenshot to confirm it
  rendered in the bar (bar geometry: `hyprctl monitors`).
- **Hyprland is Lua-configured here.** `hyprctl dispatch '<lua>'` (e.g.
  `hyprctl dispatch 'hl.dsp.focus({ window = "address:0x..." })'`); the old
  `hyprctl dispatch exec "..."` form fails. Prefer the MCP window helpers.

## Caveats

- **Concurrent seats are fine** (each has its own static IP) - just mind RAM.
- **Seat lifetime is automatic.** Do NOT call `heartbeat`; the MCP renews the
  lease and there is **no fixed time limit**. If your process dies, the seat is
  preserved (stasis) under Needs attention rather than destroyed.
- **RAM admission.** On this host, `insufficient_ram` is common; ask the
  operator for `omavroom admission --override allow` (restore `auto` after).
- Agent screenshots are **on demand** (native/region); the Command Center's
  monitor wall is a separate ~2s thumbnail feed.
- **Never launch a GUI/TUI on the host.** That's the whole point.
