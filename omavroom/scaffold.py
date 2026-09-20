"""Project scaffolding for isolated work: ``omavroom init``.

A project that wants an omavroom seat needs a recipe declaring the tools its
seats must have. :func:`init_project` detects those tools from the files a
project already has (``Cargo.toml`` -> Rust, ``package.json`` -> Node, ...),
resolves them to Arch packages with :func:`omavroom.images.resolve_tools`, and
writes ``<root>/.omavroom/image.toml`` so ``image_ensure(project,
project_root=<root>)`` can build the project image from it with no other
arguments.

The scaffold is deliberately *pure and testable*: :func:`detect_tools` only
reads filenames, and :func:`init_project` is idempotent -- an existing recipe
is left untouched unless ``force`` is set, and the project's ``AGENTS.md`` is
never clobbered (the isolated-work section is only appended when missing).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from omavroom.config import (
    ImageRecipe,
    default_recipe_path,
    is_safe_image_name,
    load_recipe,
    write_recipe,
)
from omavroom.images import resolve_tools

#: The default base image for a scaffolded project recipe.
DEFAULT_BASE = "golden-omarchy"

#: Filename -> high-level tool. Evaluated in order; the first match per tool
#: wins, and the resulting list preserves this order.
FILE_TOOLS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "rust"),
    ("package.json", "node"),
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("setup.py", "python"),
    ("go.mod", "go"),
    ("Dockerfile", "docker"),
)

#: Tools every scaffolded project gets (a checkout + a compiler/base-devel).
ALWAYS_TOOLS: tuple[str, ...] = ("git", "build")

#: Heading of the section ``init`` appends to a project's ``AGENTS.md``.
AGENTS_HEADER = "## Isolated work (omavroom)"

#: The short, host-safe guidance appended to (or created as) ``AGENTS.md``.
AGENTS_SECTION = f"""\
{AGENTS_HEADER}

- **Never launch a GUI/TUI on the host session.** Do graphical work in an
  omavroom **seat** instead (see the `omavroom` skill).
- Builds/tests that must not disturb the host also belong in a seat, not on the
  host.
- If the project needs tools the seat image lacks, **do not install them by
  hand in a seat.** Use the project-image flow: `image_ensure(project,
  project_root=".")` reads `.omavroom/image.toml` (scaffolded by
  `omavroom init`) and builds the project image per policy.
"""


def detect_tools(root: str | Path) -> list[str]:
    """Detect high-level tools from the files in ``root`` (pure, no writes).

    ``Cargo.toml`` -> ``rust``; ``package.json`` -> ``node``;
    ``pyproject.toml``/``requirements.txt``/``setup.py`` -> ``python``;
    ``go.mod`` -> ``go``; ``Dockerfile`` -> ``docker``; any ``*.tex`` (at any
    depth) -> ``tex``. ``git`` and ``build`` are always included. The result is
    de-duplicated and ordered by :data:`FILE_TOOLS`, then ``tex``, then the
    always-on tools. Raises :class:`ValueError` when ``root`` is not a
    directory.
    """
    base = Path(root)
    if not base.is_dir():
        raise ValueError(f"not a directory: {base}")

    tools: list[str] = []
    for filename, tool in FILE_TOOLS:
        if (base / filename).is_file() and tool not in tools:
            tools.append(tool)
    if "tex" not in tools and next(base.rglob("*.tex"), None) is not None:
        tools.append("tex")
    for tool in ALWAYS_TOOLS:
        if tool not in tools:
            tools.append(tool)
    return tools


def merge_tools(detected: Iterable[str], extra: Iterable[str] | None) -> list[str]:
    """Union of ``detected`` and ``extra``, order preserved, de-duplicated."""
    merged = list(detected)
    for tool in extra or ():
        token = str(tool).strip()
        if token and token not in merged:
            merged.append(token)
    return merged


def suggested_image_name(root: str | Path) -> str:
    """The ``<project>-image`` name ``image_ensure`` would derive for ``root``."""
    project = Path(root).resolve().name
    candidate = f"{project}-image"
    if not is_safe_image_name(candidate):
        raise ValueError(f"cannot derive an image name from project {project!r}")
    return candidate


def ensure_agents_section(root: str | Path) -> tuple[bool, bool]:
    """Create or extend ``<root>/AGENTS.md`` with the isolated-work section.

    Never clobbers existing content. Returns ``(created, changed)``: ``created``
    is true when the file did not exist; ``changed`` is true when the file was
    written (created, or the section appended). An existing section is left
    untouched.
    """
    path = Path(root) / "AGENTS.md"
    if not path.exists():
        path.write_text(AGENTS_SECTION, encoding="utf-8")
        return True, True

    text = path.read_text(encoding="utf-8")
    if AGENTS_HEADER in text:
        return False, False

    addition = AGENTS_SECTION.rstrip("\n")
    if not text.strip():
        new_text = addition + "\n"
    elif text.endswith("\n\n"):
        new_text = text + addition + "\n"
    elif text.endswith("\n"):
        new_text = text + "\n" + addition + "\n"
    else:
        new_text = text + "\n\n" + addition + "\n"
    path.write_text(new_text, encoding="utf-8")
    return False, True


def init_project(
    path: str | Path = ".",
    *,
    tools: Iterable[str] | None = None,
    base: str | None = None,
    force: bool = False,
) -> dict:
    """Scaffold a project for isolated work.

    Detects tools, unions them with ``tools`` (the ``--tools`` override), and
    writes ``<path>/.omavroom/image.toml`` with ``base`` (default
    :data:`DEFAULT_BASE`) and the resolved packages. Idempotent: an existing
    recipe is left alone (and reported) unless ``force`` is true. Ensures the
    project's ``AGENTS.md`` carries the isolated-work section. Returns a summary
    dict; never runs a VM and never needs a daemon.
    """
    root = Path(path)
    if not root.exists():
        raise ValueError(f"path does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    detected = detect_tools(root)
    merged = merge_tools(detected, tools)
    packages = resolve_tools(merged)
    image = suggested_image_name(root)
    recipe_path = default_recipe_path(root)

    if recipe_path.exists() and not force:
        recipe = load_recipe(recipe_path)
        recipe_written = False
    else:
        recipe = ImageRecipe(base=base or DEFAULT_BASE, packages=tuple(packages))
        write_recipe(recipe_path, recipe)
        recipe_written = True

    agents_created, agents_changed = ensure_agents_section(root)

    return {
        "path": str(root),
        "project": Path(root).resolve().name,
        "detected_tools": detected,
        "tools": merged,
        "packages": packages,
        "base": base or DEFAULT_BASE,
        "image": image,
        "recipe_path": str(recipe_path),
        "recipe_written": recipe_written,
        "recipe": {
            "base": recipe.base,
            "packages": list(recipe.packages),
            "post": list(recipe.post),
        },
        "agents_created": agents_created,
        "agents_changed": agents_changed,
    }


__all__ = [
    "AGENTS_HEADER",
    "AGENTS_SECTION",
    "ALWAYS_TOOLS",
    "DEFAULT_BASE",
    "FILE_TOOLS",
    "detect_tools",
    "ensure_agents_section",
    "init_project",
    "merge_tools",
    "suggested_image_name",
]
