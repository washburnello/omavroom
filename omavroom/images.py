"""High-level tool -> Arch package resolver (the "agent presents its needs" seam).

An agent should not have to know that "I need Rust" means ``rustup`` plus
``base-devel``. :func:`resolve_tools` maps a small, curated table of high-level
tool names to the Arch packages that provide them, passes unknown-but-safe
tokens through as literal package names, and rejects anything that is not a
plain package token (empty, option-like, path traversal, whitespace).

The resolver is deliberately *data only* (no VM, no config): it turns a
request into a concrete, ordered package list that the image planner diffs
against the current project image. Safety comes from
:func:`omavroom.config.is_safe_package`, the same token rule the recipe parser
uses, so a tool name can never smuggle a ``pacman`` option or traversal.
"""

from __future__ import annotations

from collections.abc import Iterable

from omavroom.config import is_safe_package

#: Curated high-level tool -> Arch packages. Keep this small and explicit; an
#: unknown (but safe) token is treated as a literal package name instead.
KNOWN_TOOLS: dict[str, tuple[str, ...]] = {
    "rust": ("rustup", "base-devel"),
    "node": ("nodejs", "npm"),
    "python": ("python", "python-pip"),
    "go": ("go",),
    "java": ("jdk-openjdk",),
    "docker": ("docker",),
    "tex": ("texlive",),
    "git": ("git",),
    "build": ("base-devel",),
    "jq": ("jq",),
    "ripgrep": ("ripgrep",),
    "fd": ("fd",),
}

#: Convenience view: every package any known tool expands to.
KNOWN_TOOL_PACKAGES: frozenset[str] = frozenset(
    package for packages in KNOWN_TOOLS.values() for package in packages
)


class UnsafeToolError(ValueError):
    """A tool token is not a safe literal Arch package name."""


def resolve_tools(tools: Iterable[str]) -> list[str]:
    """Resolve high-level tool names to a de-duplicated, ordered package list.

    - A known tool expands to its package tuple (:data:`KNOWN_TOOLS`).
    - An unknown token that is a safe package name (:func:`is_safe_package`)
      passes through unchanged, so ``resolve_tools(["zsh"])`` works even
      though ``zsh`` is not in the curated table.
    - Anything else (empty/whitespace, a leading dash such as ``"--noconfirm"``,
      ``".."`` traversal, a non-string) raises :class:`UnsafeToolError`.

    Order is preserved (tools first, in the order given, then their packages in
    table order); duplicates are dropped so ``resolve_tools(["rust", "build"])``
    yields ``["rustup", "base-devel"]`` once.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, str):
            raise UnsafeToolError(f"tool name must be a string, got {tool!r}")
        token = tool.strip()
        if not token:
            raise UnsafeToolError("tool name must be a non-empty string")
        packages = KNOWN_TOOLS.get(token)
        if packages is None:
            if not is_safe_package(token):
                raise UnsafeToolError(f"unsafe tool/package token: {tool!r}")
            packages = (token,)
        for package in packages:
            if package not in seen:
                seen.add(package)
                resolved.append(package)
    return resolved


__all__ = [
    "KNOWN_TOOLS",
    "KNOWN_TOOL_PACKAGES",
    "UnsafeToolError",
    "resolve_tools",
]
