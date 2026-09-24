"""Agent-presented image needs: resolver, plan, ensure, policy, build visibility.

VM-free: the manager runs against :class:`FakeProvisioner`, and the
daemon/client/MCP layers run in-process against ``fake_daemon``. Everything
asserts real behavior (built commands, recorded hashes, on-disk recipe files,
policy decisions) rather than faking the seam under test.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from omavroom.client import DaemonClient
from omavroom.config import (
    DEFAULT_IMAGE_ALLOWLIST,
    Config,
    ImageBuildConfig,
    ImageConfig,
    ImageRecipe,
    ProjectConfig,
    default_recipe_path,
    load_recipe,
    recipe_hash,
    write_recipe,
)
from omavroom.images import KNOWN_TOOLS, UnsafeToolError, resolve_tools
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.mcp.server import IMAGE_GUIDE, MCP_TOOL_NAMES, OmavroomTools


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    return tmp_path


# --------------------------------------------------------------------------
# 1. tool -> package resolver
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tool", "packages"),
    [
        ("rust", ["rustup", "base-devel"]),
        ("node", ["nodejs", "npm"]),
        ("python", ["python", "python-pip"]),
        ("go", ["go"]),
        ("java", ["jdk-openjdk"]),
        ("docker", ["docker"]),
        ("tex", ["texlive"]),
        ("git", ["git"]),
        ("build", ["base-devel"]),
        ("jq", ["jq"]),
        ("ripgrep", ["ripgrep"]),
        ("fd", ["fd"]),
    ],
)
def test_resolve_tools_known(tool: str, packages: list[str]) -> None:
    assert resolve_tools([tool]) == packages
    assert KNOWN_TOOLS[tool] == tuple(packages)


def test_resolve_tools_dedupes_and_preserves_order() -> None:
    # ``build`` is base-devel, already pulled in by ``rust``; only once.
    assert resolve_tools(["rust", "build", "jq"]) == ["rustup", "base-devel", "jq"]
    assert resolve_tools([]) == []


def test_resolve_tools_passthrough_unknown_safe() -> None:
    assert resolve_tools(["zsh"]) == ["zsh"]
    assert resolve_tools(["extra/neovim"]) == ["extra/neovim"]
    assert resolve_tools([" zsh "]) == ["zsh"]


@pytest.mark.parametrize("bad", ["-oops", "--noconfirm", "", "   ", "..", "a b", "../x", None, 5])
def test_resolve_tools_rejects_unsafe(bad: object) -> None:
    with pytest.raises((UnsafeToolError, ValueError)):
        resolve_tools([bad])


# --------------------------------------------------------------------------
# 2/5. recipe hash + config plumbing
# --------------------------------------------------------------------------
def test_recipe_hash_is_order_stable_and_content_sensitive() -> None:
    a = recipe_hash("golden-omarchy", ["base-devel", "rustup"], ["echo hi"])
    b = recipe_hash("golden-omarchy", ["rustup", "base-devel"], ["echo hi"])
    assert a == b  # package order is irrelevant
    assert a != recipe_hash("golden-omarchy", ["rustup", "base-devel", "jq"], ["echo hi"])
    assert a != recipe_hash("golden-omarchy", ["rustup", "base-devel"], [])
    assert a != recipe_hash("golden-desktop", ["rustup", "base-devel"], ["echo hi"])
    # post order matters (commands run in sequence)
    assert recipe_hash(None, [], ["a", "b"]) != recipe_hash(None, [], ["b", "a"])


def test_config_parses_images_build_policy_and_recipe_metadata(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "[images]\n"
        'build_policy = "ask"\n'
        'allowlist = ["git", "jq"]\n'
        "[images.omatype]\n"
        'golden = "/tmp/omatype.qcow2"\n'
        'seat_type = "desktop"\n'
        'base = "golden-omarchy"\n'
        'packages = ["rustup", "base-devel"]\n'
        'post = ["echo hi"]\n'
        'recipe_hash = "abc"\n',
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.image_build.policy == "ask"
    assert cfg.image_build.allowlist == ("git", "jq")
    entry = cfg.images["omatype"]
    assert entry.base == "golden-omarchy"
    assert entry.packages == ("rustup", "base-devel")
    assert entry.post == ("echo hi",)
    assert entry.recipe_hash == "abc"


def test_config_rejects_bad_build_policy_and_allowlist(tmp_path: Path) -> None:
    for body, match in (
        ('[images]\nbuild_policy = "yolo"\n', "build_policy must be one of"),
        ('[images]\nallowlist = ["-oops"]\n', "not a valid package"),
        ('[images]\nallowlist = "git"\n', "must be a list"),
        ("[images]\nbuild_policy = 5\n", "build_policy must be a non-empty string"),
    ):
        cfg_file = tmp_path / "bad.toml"
        cfg_file.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            Config.from_toml(cfg_file)


def test_config_set_images_build_policy_in_place() -> None:
    cfg = Config.default()
    assert cfg.image_build.policy == "allowlist"
    cfg.set_value("images", "build_policy", "auto")
    cfg.set_value("images", "allowlist", ["git"])
    assert cfg.image_build.policy == "auto"
    assert cfg.image_build.allowlist == ("git",)
    with pytest.raises(ValueError, match="unknown key in \\[images\\]"):
        cfg.set_value("images", "bogus", "x")


def test_image_build_config_defaults() -> None:
    build = ImageBuildConfig()
    assert build.policy == "allowlist"
    assert "base-devel" in build.allowlist
    assert "docker" not in build.allowlist  # heavy tools stay approval-gated
    assert DEFAULT_IMAGE_ALLOWLIST == build.allowlist


def test_write_recipe_round_trips(tmp_path: Path) -> None:
    path = default_recipe_path(tmp_path)
    write_recipe(path, ImageRecipe(base="golden-omarchy", packages=("rustup",), post=("echo",)))
    recipe = load_recipe(path)
    assert recipe.base == "golden-omarchy"
    assert recipe.packages == ("rustup",)
    assert recipe.post == ("echo",)


# --------------------------------------------------------------------------
# 3. image_plan (read-only)
# --------------------------------------------------------------------------
def _image(name: str, tmp_path: Path, **kwargs) -> ImageConfig:
    golden = tmp_path / "images" / f"{name}.qcow2"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_bytes(b"img")
    return ImageConfig(golden=str(golden), seat_type="desktop", **kwargs)


def test_image_plan_not_satisfied_shows_missing(make_manager, user_config, tmp_path) -> None:
    cfg = Config.default()
    cfg.images["p-image"] = _image(
        "p-image",
        tmp_path,
        base="golden-omarchy",
        packages=("jq",),
        recipe_hash=recipe_hash("golden-omarchy", ("jq",), ()),
    )
    cfg.projects["p"] = ProjectConfig(image="p-image")
    mgr = make_manager(cfg)
    plan = mgr.plan_image("p", tools=["rust"])
    assert plan["image"] == "p-image"
    assert plan["resolved_packages"] == ["rustup", "base-devel"]
    assert plan["missing_packages"] == ["rustup", "base-devel"]
    assert plan["current_packages"] == ["jq"]
    # The would-be recipe merges existing + new.
    assert plan["recipe"]["packages"] == ["jq", "rustup", "base-devel"]
    assert plan["satisfied"] is False


def test_image_plan_satisfied_and_no_side_effects(make_manager, user_config, tmp_path) -> None:
    cfg = Config.default()
    cfg.images["p-image"] = _image(
        "p-image",
        tmp_path,
        base="golden-omarchy",
        packages=("jq",),
        recipe_hash=recipe_hash("golden-omarchy", ("jq",), ()),
    )
    cfg.projects["p"] = ProjectConfig(image="p-image")
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    project_root = tmp_path / "repo"
    project_root.mkdir()
    plan = mgr.plan_image("p", packages=["jq"], base=None)
    assert plan["satisfied"] is True
    assert plan["missing_packages"] == []
    assert fake.built_images == []
    assert not (project_root / ".omavroom").exists()


def test_image_plan_derives_target_name_when_unbound(make_manager, user_config) -> None:
    mgr = make_manager()
    plan = mgr.plan_image("myproj", tools=["git"])
    assert plan["image"] == "myproj-image"
    assert plan["recipe"]["base"] == "golden-omarchy"  # desktop default
    assert plan["satisfied"] is False


# --------------------------------------------------------------------------
# 4/5. image_ensure: idempotency, policies, recipe write, delta
# --------------------------------------------------------------------------
def test_ensure_auto_builds_then_satisfied(make_manager, user_config, tmp_path) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    project_root = tmp_path / "repo"
    project_root.mkdir()

    first = mgr.ensure_image("p", tools=["rust"], project_root=project_root)
    assert first["status"] == "built"
    assert first["image"] == "p-image"
    assert first["delta"] is False
    assert fake.built_images[-1]["packages"] == ["rustup", "base-devel"]
    assert mgr.config.project_image("p") == "p-image"
    recipe = load_recipe(default_recipe_path(project_root))
    assert recipe.base == "golden-omarchy"
    assert recipe.packages == ("rustup", "base-devel")

    second = mgr.ensure_image("p", tools=["rust"], project_root=project_root)
    assert second["status"] == "satisfied"
    assert len(fake.built_images) == 1  # no rebuild


def test_ensure_allowlist_builds_only_allowlisted(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build = ImageBuildConfig(policy="allowlist", allowlist=("git", "jq"))
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)

    ok = mgr.ensure_image("p", tools=["jq"])
    assert ok["status"] == "built"
    blocked = mgr.ensure_image("p", tools=["docker"])
    assert blocked["status"] == "needs_approval"
    assert blocked["missing_packages"] == ["docker"]
    assert len(fake.built_images) == 1  # docker did not build

    forced = mgr.ensure_image("p", tools=["docker"], approved=True)
    assert forced["status"] == "built"
    assert fake.built_images[-1]["packages"] == ["jq", "docker"]


def test_ensure_ask_never_auto_builds(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build = ImageBuildConfig(policy="ask")
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)

    result = mgr.ensure_image("p", tools=["jq"])
    assert result["status"] == "needs_approval"
    assert fake.built_images == []
    # Re-calling after approval is idempotent and builds once.
    built = mgr.ensure_image("p", tools=["jq"], approved=True)
    assert built["status"] == "built"
    assert len(fake.built_images) == 1
    assert mgr.ensure_image("p", tools=["jq"], approved=True)["status"] == "satisfied"
    assert len(fake.built_images) == 1


def test_ensure_delta_uses_existing_image_as_base(make_manager, user_config, tmp_path) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    project_root = tmp_path / "repo"
    project_root.mkdir()

    mgr.ensure_image("p", tools=["jq"], project_root=project_root)
    second = mgr.ensure_image("p", tools=["jq", "git"], project_root=project_root)
    assert second["status"] == "built"
    assert second["missing_packages"] == ["git"]
    assert second["delta"] is True
    assert fake.built_images[-1]["base"] == "p-image"
    assert fake.built_images[-1]["requested_base"] == "golden-omarchy"
    recipe = load_recipe(default_recipe_path(project_root))
    assert recipe.packages == ("jq", "git")


def test_image_status_reports_recorded_packages(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    mgr.ensure_image("p", tools=["rust"])
    status = mgr.image_status("p-image")
    assert status["packages"] == ["rustup", "base-devel"]
    assert status["state"] == "done"
    # The recorded package list survives a config reload (persisted).
    assert Config.load().images["p-image"].packages == ("rustup", "base-devel")


def test_delta_build_records_cumulative_packages(make_manager, user_config) -> None:
    """A delta build must record existing + new packages, not just the delta.

    Otherwise ``image_status``/``image_plan`` would report only the latest
    packages and think the prior tools are missing again.
    """
    cfg = Config.default()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    first = mgr.build_image("p-image", packages=["jq"], approved=True)
    assert first["packages"] == ["jq"]
    second = mgr.build_image("p-image", packages=["git"], approved=True)
    assert second["delta"] is True
    assert second["packages"] == ["jq", "git"]
    assert mgr.image_status("p-image")["packages"] == ["jq", "git"]
    assert Config.load().images["p-image"].packages == ("jq", "git")
    # A subsequent ensure for a package already recorded sees it as present.
    plan = mgr.plan_image("p", packages=["jq"])
    assert plan["missing_packages"] == []
    assert plan["current_packages"] == ["jq", "git"]


def test_ensure_hash_change_triggers_rebuild(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    mgr.ensure_image("p", tools=["git"])
    # Same packages but a different base changes the recorded recipe hash.
    result = mgr.ensure_image("p", tools=["git"], base="golden-desktop")
    assert result["status"] == "built"
    assert len(fake.built_images) == 2
    assert mgr.config.images["p-image"].base == "golden-desktop"


def test_ensure_preserves_recorded_post_and_base(make_manager, user_config, tmp_path) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    project_root = tmp_path / "repo"
    project_root.mkdir()
    mgr.build_image("p-image", packages=["jq"], post=["systemctl enable foo"], approved=True)

    result = mgr.ensure_image("p", tools=["git"], project_root=project_root)
    assert result["status"] == "built"
    recipe = load_recipe(default_recipe_path(project_root))
    assert recipe.packages == ("jq", "git")
    assert recipe.post == ("systemctl enable foo",)
    assert recipe.base == "golden-omarchy"


def test_plan_rejects_unsafe_package(make_manager, user_config) -> None:
    mgr = make_manager()
    with pytest.raises(ValueError, match="invalid package"):
        mgr.plan_image("p", packages=["--noconfirm"])


# --------------------------------------------------------------------------
# 6. build visibility: tracker, pool_status.builds, image_status/logs, events
# --------------------------------------------------------------------------
class _BlockingProvisioner(FakeProvisioner):
    """FakeProvisioner whose build blocks until released (observable 'running')."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def build_image(self, base_image, *, name, packages=(), post=(), on_log=None):
        if on_log is not None:
            on_log("pacman -S --needed rustup\ndownloading...\n")
        self.entered.set()
        self.release.wait(timeout=5)
        return super().build_image(
            base_image, name=name, packages=packages, post=post, on_log=on_log
        )


def test_build_visibility_running_and_done(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = _BlockingProvisioner()
    mgr = make_manager(cfg, provisioner=fake)

    thread = threading.Thread(target=lambda: mgr.ensure_image("p", tools=["rust"]))
    thread.start()
    assert fake.entered.wait(timeout=5)

    builds = {entry["name"]: entry for entry in mgr.pool_status().builds}
    assert builds["p-image"]["state"] == "running"
    assert builds["p-image"]["started_at"]
    assert "downloading" in builds["p-image"]["log_tail"]

    status = mgr.image_status("p-image")
    assert status["state"] == "running"

    logs = mgr.image_logs("p-image", tail=2)
    assert logs["state"] == "running"
    assert logs["lines"][-1] == "downloading..."

    fake.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert mgr.image_status("p-image")["state"] == "done"
    event_types = {event.event_type for event in mgr.list_events(limit=50)}
    assert {"image_build_start", "image_build_done"} <= event_types


def test_build_event_on_error(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.image_build.policy = "auto"
    fake = FakeProvisioner(fail_on={"build_image"})
    mgr = make_manager(cfg, provisioner=fake)
    from omavroom.manager.provisioner import ProvisionerError

    with pytest.raises(ProvisionerError):
        mgr.ensure_image("p", tools=["rust"])
    assert mgr.image_status("p-image")["state"] == "error"
    assert "image_build_error" in {event.event_type for event in mgr.list_events(limit=50)}


# --------------------------------------------------------------------------
# daemon / client round trips
# --------------------------------------------------------------------------
def test_daemon_image_plan_ensure_status_logs(fake_daemon, user_config, tmp_path) -> None:
    with fake_daemon() as pool:
        pool.config.image_build.policy = "auto"
        client = DaemonClient(pool.socket_path)
        try:
            plan = client.image_plan("proj", tools=["jq"])
            assert plan["image"] == "proj-image"
            assert plan["missing_packages"] == ["jq"]

            started = client.image_ensure("proj", tools=["jq"])
            assert started["status"] == "building"
            assert started["job_id"]
            view = client.job_poll(started["job_id"])
            deadline = time.monotonic() + 10
            while view["state"] == "pending" and time.monotonic() < deadline:
                time.sleep(0.02)
                view = client.job_poll(started["job_id"])
            assert view["state"] == "done"
            assert view["result"]["status"] == "built"

            assert client.image_status("proj-image")["state"] == "done"
            assert isinstance(client.image_logs("proj-image", tail=5)["lines"], list)

            again = client.image_ensure("proj", tools=["jq"])
            assert again["status"] == "satisfied"
        finally:
            client.close()


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
def test_mcp_image_flow_tools_registered() -> None:
    assert {"image_plan", "image_ensure", "image_status", "image_logs", "guide"} <= set(
        MCP_TOOL_NAMES
    )


def test_mcp_guide_mentions_the_flow() -> None:
    for token in ("image_plan", "image_ensure", "request_seat", "build_policy"):
        assert token in IMAGE_GUIDE


def test_mcp_image_ensure_round_trip(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        pool.config.image_build.policy = "auto"
        client = DaemonClient(pool.socket_path)
        tools = OmavroomTools(client, autostart_heartbeat=False)
        try:
            assert "image_plan" in MCP_TOOL_NAMES
            assert tools.guide() == IMAGE_GUIDE
            plan = tools.image_plan("proj", tools=["jq"])
            assert plan["image"] == "proj-image"
            started = tools.image_ensure("proj", tools=["jq"])
            assert started["status"] == "building"
            view = tools.job_wait(started["job_id"], timeout_s=10)
            assert view["state"] == "done"
            assert view["result"]["status"] == "built"
            assert tools.image_status("proj-image")["state"] == "done"
        finally:
            tools.stop()
            client.close()
