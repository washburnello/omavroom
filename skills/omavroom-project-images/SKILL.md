---
name: omavroom-project-images
description: >-
  Use when a project's isolated Omarchy seats need tools the base image lacks -
  a clean-room toolchain, compilers/runtimes (rust, node, python, go, ...), or
  anything you would otherwise install by hand in every disposable seat. Teaches
  the Dockerfile-like flow: declare the tools your plan needs, let omavroom bake
  a per-project golden image once, and every later seat already has them. Use
  ONLY for preparing/using a project image; for everyday seat use load the
  `omavroom` skill instead.
---

# Project images: bake the toolchain once, use it in every seat

Seats boot a stock Omarchy golden. If your project needs more (a Rust
toolchain, a language runtime, extra system packages), do NOT install it by hand
in each disposable seat - every seat starts from the same golden, so you'd
repeat the work every time and waste minutes per seat.

Instead, give the project its own **image**: a golden baked once with your
dependencies. It is the Dockerfile idea applied to seats - a recipe in your
repo, built on request, reused by every seat and every future session.

## When to use it

- Your `plan.md` implies tooling the base lacks (e.g. `cargo`, `pip`, `node`).
- Terminal seats are your clean-room builds, and they're missing the toolchain.
- You catch yourself running `pacman -S ...` or `rustup ...` inside a seat.

## The flow

1. **Read the project's plan** and list the *tools* it needs, by intent -
   `rust`, `python`, `node`, `go`, `docker`, `tex`, `git`, `build`, `jq`, ...
   (full set: the resolver's `KNOWN_TOOLS`). Not package names.
2. **Preview** - `omavroom_image_plan(project="omatype", tools=["rust","python"])`
   returns the resolved recipe, the packages that would be added, and whether
   the current image already satisfies it. Show this to the user if you're
   unsure.
3. **Ensure** - `omavroom_image_ensure(project="omatype", tools=["rust","python"],
   project_root="/abs/path/to/project")`. It is idempotent:
   - already satisfied -> returns `{"status":"satisfied","image":...}` (seconds);
   - otherwise it writes/updates `.omavroom/image.toml` in the project repo and
     either starts a build (`{"status":"building","job_id":...}`) or returns
     `{"status":"needs_approval","recipe":...}`.
4. **Approval** - the build policy decides:
   - `allowlist` (default): builds automatically when every package is on the
     operator's allowlist; otherwise `needs_approval`.
   - `ask`: never auto-builds - surface the recipe to the user and re-call with
     approval, or have them run `omavroom image build <name> --yes`.
   - `auto`: always builds.
   When approval is needed, show the proposed recipe (base + packages) and ask.
5. **Wait** - builds take a while and run one at a time. Poll `omavroom_image_status`
   or `omavroom_job_poll(job_id)`; `omavroom_image_logs` shows progress. You can
   keep working on the host meanwhile.
6. **Use it** - `omavroom_request_seat(project="omatype", seat_type=...)`. The
   seat is now provisioned from your project image, toolchain included.

## The recipe (versioned in your repo)

`image_ensure` writes `.omavroom/image.toml`; commit it. It's the source of
truth and is reviewable:

```toml
base = "golden-omarchy"                 # a registered golden
packages = ["rustup", "base-devel"]     # resolved from tools=[...]
# post = ["cmd1", "cmd2"]               # optional commands run after install
```

To add a dependency later, re-run `image_ensure` with the extra tool - it
merges, updates the recipe, and rebuilds only the **delta** (the existing image
is reused as the base), so it stays cheap.

## Going forward

- **Always request seats with `project=<name>`** so they use your image.
- **Never hand-install toolchains in a seat** that a project image should own.
- Keep the recipe in git; when it changes, `image_ensure` rebuilds the delta.
- The image is just a golden + your packages - the same image serves desktop and
  terminal seats.

## Notes

- `image_build` explicitly (with `approved=True`, or `omavroom image build <name>
  --yes`) is the manual escape hatch; `image_rm` removes an image (refused if a
  seat is using it).
- Builds are serialized (one VM at a time) and can take 10-40 minutes; the
  result is cached and reused, so it's a one-time cost per dependency change.
