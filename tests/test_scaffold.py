"""``omavroom init`` scaffolding + ``image_*`` reading an existing recipe.

VM-free: detection is pure filesystem reads, the manager runs against
:class:`FakeProvisioner`, and the daemon/CLI layers run in-process. Assertions
check the real written recipe and the real build calls, never a fake seam.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omavroom import cli
from omavroom.client import DaemonClient
from omavroom.config import Config, default_recipe_path, load_recipe, recipe_hash
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.scaffold import (
    AGENTS_HEADER,
    detect_tools,
    init_project,
    merge_tools,
    suggested_image_name,
)


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    return tmp_path


# --------------------------------------------------------------------------
# detect_tools
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("filename", "tool"),
    [
        ("Cargo.toml", "rust"),
        ("package.json", "node"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("setup.py", "python"),
        ("go.mod", "go"),
        ("Dockerfile", "docker"),
    ],
)
def test_detect_tools_per_file_type(tmp_path: Path, filename: str, tool: str) -> None:
    (tmp_path / filename).write_text("x", encoding="utf-8")
    tools = detect_tools(tmp_path)
    assert tool in tools
    # git + build are always present.
    assert "git" in tools and "build" in tools


def test_detect_tools_tex_is_recursive(tmp_path: Path) -> None:
    nested = tmp_path / "docs" / "paper"
    nested.mkdir(parents=True)
    (nested / "main.tex").write_text("\\documentclass", encoding="utf-8")
    assert "tex" in detect_tools(tmp_path)


def test_detect_tools_empty_project_has_only_always() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        assert detect_tools(directory) == ["git", "build"]


def test_detect_tools_order_and_dedup(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("x", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("x", encoding="utf-8")
    # FILE_TOOLS order: rust (Cargo) before python (pyproject), then always.
    assert detect_tools(tmp_path) == ["rust", "python", "git", "build"]


def test_detect_tools_rejects_non_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        detect_tools(tmp_path / "missing")


def test_merge_tools_extends_and_dedupes() -> None:
    assert merge_tools(["git", "build"], ["jq", "git", " rust "]) == [
        "git",
        "build",
        "jq",
        "rust",
    ]
    assert merge_tools(["git"], None) == ["git"]
    assert merge_tools(["git"], []) == ["git"]


# --------------------------------------------------------------------------
# init_project: recipe write, idempotency, --force, AGENTS.md
# --------------------------------------------------------------------------
def test_init_writes_recipe_with_detected_packages(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")

    result = init_project(root)

    assert result["detected_tools"] == ["rust", "git", "build"]
    assert result["tools"] == ["rust", "git", "build"]
    assert result["packages"] == ["rustup", "base-devel", "git"]
    assert result["recipe_written"] is True
    assert result["base"] == "golden-omarchy"
    assert result["image"] == "proj-image"
    assert result["recipe"]["base"] == "golden-omarchy"
    assert result["recipe"]["packages"] == ["rustup", "base-devel", "git"]

    recipe = load_recipe(default_recipe_path(root))
    assert recipe.base == "golden-omarchy"
    assert recipe.packages == ("rustup", "base-devel", "git")


def test_init_empty_project_and_custom_base(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    result = init_project(root, base="golden-desktop")
    assert result["detected_tools"] == ["git", "build"]
    recipe = load_recipe(default_recipe_path(root))
    assert recipe.base == "golden-desktop"
    assert recipe.packages == ("git", "base-devel")


def test_init_is_idempotent_and_force_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    first = init_project(root, tools=["jq"])
    assert first["recipe_written"] is True
    original = default_recipe_path(root).read_text(encoding="utf-8")

    second = init_project(root, tools=["rust"])
    assert second["recipe_written"] is False
    assert default_recipe_path(root).read_text(encoding="utf-8") == original
    # The reported packages track the on-disk recipe, not the new request.
    assert second["recipe"]["packages"] == ["git", "base-devel", "jq"]

    forced = init_project(root, tools=["rust"], force=True)
    assert forced["recipe_written"] is True
    forced_packages = load_recipe(default_recipe_path(root)).packages
    assert "rustup" in forced_packages
    assert "jq" not in forced_packages


def test_init_suggests_image_name_from_directory(tmp_path: Path) -> None:
    root = tmp_path / "MyRepo"
    root.mkdir()
    assert suggested_image_name(root) == "MyRepo-image"


def test_init_agents_created(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    result = init_project(root)
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert result["agents_created"] is True
    assert result["agents_changed"] is True
    assert AGENTS_HEADER in agents
    assert ".omavroom/image.toml" in agents


def test_init_agents_appends_without_clobbering(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    original = "# My Project\n\nExisting guidance.\n"
    (root / "AGENTS.md").write_text(original, encoding="utf-8")

    result = init_project(root)

    updated = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert result["agents_created"] is False
    assert result["agents_changed"] is True
    assert updated.startswith(original)  # nothing removed
    assert AGENTS_HEADER in updated


def test_init_agents_with_section_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    init_project(root)
    after_first = (root / "AGENTS.md").read_text(encoding="utf-8")

    result = init_project(root)
    assert result["agents_created"] is False
    assert result["agents_changed"] is False
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == after_first


def test_init_nonexistent_path_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        init_project(tmp_path / "nope")


def test_init_file_path_errors(tmp_path: Path) -> None:
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        init_project(target)


# --------------------------------------------------------------------------
# image_plan / image_ensure read an existing recipe
# --------------------------------------------------------------------------
def test_plan_reads_written_recipe(make_manager, user_config, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_project(root)
    mgr = make_manager()

    plan = mgr.plan_image("p", project_root=root)
    assert plan["image"] == "p-image"
    assert plan["recipe"]["base"] == "golden-omarchy"
    assert plan["recipe"]["packages"] == ["git", "base-devel"]
    assert plan["resolved_packages"] == ["git", "base-devel"]
    assert plan["missing_packages"] == ["git", "base-devel"]
    assert plan["satisfied"] is False


def test_plan_explicit_request_beats_recipe(make_manager, user_config, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_project(root)  # packages git, base-devel
    mgr = make_manager()

    plan = mgr.plan_image("p", tools=["jq"], project_root=root)
    assert plan["resolved_packages"] == ["jq"]
    assert plan["recipe"]["packages"] == ["jq"]


def test_plan_missing_recipe_with_no_request_errors(
    make_manager, user_config, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    mgr = make_manager()
    with pytest.raises(ValueError, match="no recipe"):
        mgr.plan_image("p", project_root=root)
    with pytest.raises(ValueError, match="no recipe"):
        mgr.ensure_image("p", project_root=root)


def test_ensure_builds_from_recipe_then_satisfied(
    make_manager, user_config, tmp_path: Path
) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    root = tmp_path / "repo"
    root.mkdir()
    # A Rust project recipe: rustup, base-devel, git.
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    init_project(root)

    first = mgr.ensure_image("p", project_root=root)
    assert first["status"] == "built"
    assert fake.built_images[-1]["packages"] == ["rustup", "base-devel", "git"]
    assert fake.built_images[-1]["base"] == "golden-omarchy"
    assert mgr.config.project_image("p") == "p-image"

    # The image now records exactly the recipe hash, so a repeat is satisfied.
    plan = mgr.plan_image("p", project_root=root)
    assert plan["satisfied"] is True
    assert plan["recipe_hash"] == plan["current_recipe_hash"]

    second = mgr.ensure_image("p", project_root=root)
    assert second["status"] == "satisfied"
    assert len(fake.built_images) == 1


def test_ensure_recipe_respects_policy(make_manager, user_config, tmp_path: Path) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "ask"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    root = tmp_path / "repo"
    root.mkdir()
    init_project(root)

    held = mgr.ensure_image("p", project_root=root)
    assert held["status"] == "needs_approval"
    assert fake.built_images == []

    built = mgr.ensure_image("p", project_root=root, approved=True)
    assert built["status"] == "built"
    assert mgr.ensure_image("p", project_root=root, approved=True)["status"] == "satisfied"


def test_daemon_plan_ensure_read_recipe(fake_daemon, user_config, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_project(root)
    with fake_daemon() as pool:
        pool.config.image_build.policy = "auto"
        client = DaemonClient(pool.socket_path)
        try:
            plan = client.image_plan("proj", project_root=str(root))
            assert plan["recipe"]["packages"] == ["git", "base-devel"]

            started = client.image_ensure("proj", project_root=str(root))
            assert started["status"] == "building"
            view = client.job_poll(started["job_id"])
            deadline = time.monotonic() + 10
            while view["state"] == "pending" and time.monotonic() < deadline:
                time.sleep(0.02)
                view = client.job_poll(started["job_id"])
            assert view["state"] == "done"
            assert view["result"]["status"] == "built"

            again = client.image_ensure("proj", project_root=str(root))
            assert again["status"] == "satisfied"
        finally:
            client.close()


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------
def test_cli_init_scaffolds_recipe(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")

    assert cli.main(["init", str(root)]) == 0
    out = capsys.readouterr().out
    assert "detected rust" in out
    assert "proj-image" in out
    assert "rustup" in out

    recipe = load_recipe(default_recipe_path(root))
    assert recipe.packages == ("rustup", "base-devel", "git")
    assert (root / "AGENTS.md").exists()


def test_cli_init_tools_flags_merge(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert cli.main(["init", str(root), "--tools", "jq, fd", "--tools", "zsh"]) == 0
    packages = load_recipe(default_recipe_path(root)).packages
    assert "jq" in packages and "fd" in packages and "zsh" in packages
    assert "base-devel" in packages  # always-on build


def test_cli_init_force_and_idempotent(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert cli.main(["init", str(root)]) == 0
    first = default_recipe_path(root).read_text(encoding="utf-8")
    assert cli.main(["init", str(root)]) == 0
    assert "exists; use --force" in capsys.readouterr().out
    assert default_recipe_path(root).read_text(encoding="utf-8") == first

    assert cli.main(["init", str(root), "--force", "--tools", "jq"]) == 0
    assert "jq" in load_recipe(default_recipe_path(root)).packages


def test_cli_init_defaults_to_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    assert default_recipe_path(tmp_path).exists()


def test_cli_init_nonexistent_path_errors(tmp_path: Path, capsys) -> None:
    assert cli.main(["init", str(tmp_path / "nope")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_init_json(tmp_path: Path, capsys) -> None:
    import json

    root = tmp_path / "proj"
    root.mkdir()
    assert cli.main(["init", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["image"] == "proj-image"
    assert payload["recipe_written"] is True


def test_recipe_hash_ignores_package_order(tmp_path: Path) -> None:
    # Sanity: the recipe init writes is stable under package reordering.
    assert recipe_hash("golden-omarchy", ["git", "base-devel"], ()) == recipe_hash(
        "golden-omarchy", ["base-devel", "git"], ()
    )
