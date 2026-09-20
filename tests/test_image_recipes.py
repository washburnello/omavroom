"""Per-project image recipes: config, provisioner build, daemon/MCP/CLI.

No VMs: the libvirt build path is driven through a recorded host runner that
answers ``virsh``/``qemu-img``/guest-agent calls, so the *exact* install
commands and the delta base are asserted (never fake-masked). The
daemon/client/MCP/CLI layers run in-process against a FakeProvisioner.
"""

from __future__ import annotations

import base64
import json
import tomllib
from pathlib import Path

import pytest

from omavroom import cli
from omavroom.client import DaemonClient, DaemonRequestError
from omavroom.config import (
    Config,
    ImageConfig,
    ProjectConfig,
    default_config_path,
    default_recipe_path,
    is_safe_image_name,
    is_safe_package,
    load_recipe,
    parse_recipe,
    register_image,
    remove_image,
    remove_project,
    set_project_image,
)
from omavroom.manager.libvirt_provisioner import (
    BUILD_DOMAIN_PREFIX,
    PACMAN_CLEAN_COMMAND,
    CommandResult,
    LibvirtProvisioner,
    build_pacman_install_command,
)
from omavroom.manager.provisioner import (
    FakeProvisioner,
    ImageBuildNotApproved,
    ProvisionerError,
)
from omavroom.mcp.server import MCP_TOOL_NAMES, OmavroomTools


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point the per-user config at a fresh tmp XDG dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    return default_config_path()


# --------------------------------------------------------------------------
# recipe parse / validate
# --------------------------------------------------------------------------
def test_parse_recipe_ok() -> None:
    recipe = parse_recipe(
        {
            "base": "golden-omarchy",
            "packages": ["wl-clipboard", "base-devel", "extra/python-pip"],
            "post": ["systemctl enable --now foo"],
        }
    )
    assert recipe.base == "golden-omarchy"
    assert recipe.packages == ("wl-clipboard", "base-devel", "extra/python-pip")
    assert recipe.post == ("systemctl enable --now foo",)


def test_parse_recipe_defaults() -> None:
    recipe = parse_recipe({})
    assert recipe.base is None
    assert recipe.packages == ()
    assert recipe.post == ()
    assert recipe.installs_anything is False


def test_load_recipe_from_file_and_default_path(tmp_path: Path) -> None:
    recipe_path = tmp_path / ".omavroom" / "image.toml"
    recipe_path.parent.mkdir(parents=True)
    recipe_path.write_text('base = "golden-omarchy"\npackages = ["wl-clipboard"]\n')
    recipe = load_recipe(recipe_path)
    assert recipe.base == "golden-omarchy"
    assert recipe.packages == ("wl-clipboard",)
    assert default_recipe_path(tmp_path) == recipe_path


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ([1, 2], "recipe must be a table"),
        ({"bogus": 1}, "unknown recipe keys"),
        ({"base": 5}, "recipe base must be a non-empty string"),
        ({"base": "../evil"}, "recipe base is not a valid image name"),
        ({"packages": "wl-clipboard"}, "recipe packages must be a list"),
        ({"packages": [""]}, "recipe packages must be a list of non-empty strings"),
        ({"packages": ["-oops"]}, "recipe package is not a valid package name"),
        ({"post": "echo hi"}, "recipe post must be a list of non-empty strings"),
        ({"post": [5]}, "recipe post must be a list of non-empty strings"),
    ],
)
def test_parse_recipe_bad(data, match) -> None:
    with pytest.raises(ValueError, match=match):
        parse_recipe(data)


def test_recipe_dataclass_validates() -> None:
    with pytest.raises(ValueError, match="valid package name"):
        parse_recipe({"packages": ["../escape"]})
    with pytest.raises(ValueError, match="cannot read recipe"):
        load_recipe("/does/not/exist/image.toml")


def test_safe_names() -> None:
    assert is_safe_image_name("golden-omarchy")
    assert not is_safe_image_name("../x")
    assert not is_safe_image_name("-x")
    assert is_safe_package("wl-clipboard")
    assert is_safe_package("extra/python-pip")
    assert not is_safe_package("-oops")
    assert not is_safe_package("a b")


# --------------------------------------------------------------------------
# project -> image resolution (config)
# --------------------------------------------------------------------------
def test_projects_parse_and_resolve(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "[projects.omatype]\n"
        'image = "omatype-image"\n'
        "[images.omatype-image]\n"
        'golden = "/tmp/omatype.qcow2"\n'
        'seat_type = "desktop"\n',
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.project_image("omatype") == "omatype-image"
    assert cfg.resolve_image("desktop", project="omatype") == "omatype-image"
    # Project image resolves to its registered path.
    assert cfg.golden_for("desktop", cfg.resolve_image("desktop", project="omatype")) == Path(
        "/tmp/omatype.qcow2"
    )
    # Explicit image wins over the project binding.
    assert cfg.resolve_image("desktop", image="pinned", project="omatype") == "pinned"
    # Unknown / absent project falls back to the seat type default.
    assert cfg.resolve_image("desktop", project="nope") == "golden-omarchy"
    assert cfg.resolve_image("desktop") == "golden-omarchy"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[projects.p]\n", r"projects\.p\.image is required"),
        ("[projects.p]\nimage = 5\n", r"projects\.p\.image must be a non-empty string"),
        ('[projects.p]\nimage = "../x"\n', "not a valid image name"),
        ('[projects.p]\nimage = "x"\nbogus = 1\n', r"unknown keys in \[projects\.p\]"),
        ('[projects]\nimage = "x"\n', r"\[projects\.image\] must be a table"),
    ],
)
def test_bad_projects_rejected(tmp_path: Path, body: str, match: str) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_project_persistence_round_trip(user_config) -> None:
    set_project_image("omatype", "omatype-image")
    cfg = Config.load()
    assert cfg.project_image("omatype") == "omatype-image"
    remove_project("omatype")
    assert Config.load().project_image("omatype") is None


def test_register_and_remove_image_config(user_config) -> None:
    register_image("omatype", "/tmp/omatype.qcow2", "desktop")
    data = tomllib.loads(user_config.read_text(encoding="utf-8"))
    assert data["images"]["omatype"]["golden"] == "/tmp/omatype.qcow2"
    assert data["images"]["omatype"]["seat_type"] == "desktop"
    remove_image("omatype")
    data = tomllib.loads(user_config.read_text(encoding="utf-8"))
    assert "omatype" not in data.get("images", {})


# --------------------------------------------------------------------------
# scheduler project binding
# --------------------------------------------------------------------------
def test_request_seat_uses_project_image(tmp_path: Path, make_manager) -> None:
    cfg = Config.default()
    cfg.projects["omatype"] = ProjectConfig(image="omatype-image")
    cfg.images["omatype-image"] = ImageConfig(
        golden=str(tmp_path / "omatype.qcow2"), seat_type="desktop"
    )
    mgr = make_manager(cfg, provisioner=FakeProvisioner())
    handle = mgr.request_seat("agent", "desktop", project="omatype")
    mgr.run_until_idle()
    view = handle.status()
    assert view.image == "omatype-image"
    assert view.seat is not None and view.seat.image == "omatype-image"


def test_request_seat_explicit_image_beats_project(tmp_path: Path, make_manager) -> None:
    cfg = Config.default()
    cfg.projects["omatype"] = ProjectConfig(image="omatype-image")
    cfg.images["omatype-image"] = ImageConfig(
        golden=str(tmp_path / "omatype.qcow2"), seat_type="desktop"
    )
    mgr = make_manager(cfg, provisioner=FakeProvisioner())
    handle = mgr.request_seat("agent", "desktop", image="pinned", project="omatype")
    mgr.run_until_idle()
    assert handle.status().image == "pinned"


# --------------------------------------------------------------------------
# libvirt build orchestration (recorded runner)
# --------------------------------------------------------------------------
class _BuildRunner:
    """Recorded host runner modelling virsh/qemu-img + the guest agent."""

    def __init__(self, *, existing_domains=(), fail_install: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.defined: set[str] = set(existing_domains)
        self.states: dict[str, str] = {}
        self.agent_commands: list[str] = []
        self.fail_install = fail_install
        self._pids: dict[int, str] = {}
        self._next_pid = 100

    def __call__(self, argv, timeout, input_text):  # noqa: ANN001
        self.calls.append(list(argv))
        if argv[0] == "qemu-img":
            if "create" in argv or "convert" in argv:
                Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(argv[-1]).write_bytes(b"qcow2")
            return CommandResult(0, "", "")
        if argv[0] != "virsh":
            return CommandResult(0, "", "")
        sub = argv[3] if len(argv) > 3 else ""
        args = argv[4:]
        if sub == "dominfo":
            if args and args[0] in self.defined:
                return CommandResult(0, "Name: x\nAutostart: disable\n", "")
            return CommandResult(1, "", "domain not found")
        if sub == "define":
            xml = Path(args[0]).read_text(encoding="utf-8")
            name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
            self.defined.add(name)
            self.states[name] = "shut off"
            return CommandResult(0, "", "")
        if sub == "start":
            self.states[args[0]] = "running"
            return CommandResult(0, "", "")
        if sub in ("shutdown", "destroy"):
            self.states[args[0]] = "shut off"
            return CommandResult(0, "", "")
        if sub == "domstate":
            return CommandResult(0, self.states.get(args[0], "shut off") + "\n", "")
        if sub == "undefine":
            self.defined.discard(args[0])
            return CommandResult(0, "", "")
        if sub == "qemu-agent-command":
            return self._agent(json.loads(args[1]))
        return CommandResult(0, "", "")

    def _agent(self, payload: dict) -> CommandResult:
        execute = payload.get("execute")
        if execute == "guest-ping":
            return CommandResult(0, json.dumps({"return": {}}), "")
        if execute == "guest-exec":
            pid = self._next_pid
            self._next_pid += 1
            command = payload["arguments"]["arg"][1]
            self._pids[pid] = command
            self.agent_commands.append(command)
            return CommandResult(0, json.dumps({"return": {"pid": pid}}), "")
        if execute == "guest-exec-status":
            pid = payload["arguments"]["pid"]
            command = self._pids.get(pid, "")
            exitcode = 1 if (self.fail_install and "pacman -S" in command) else 0
            out = base64.b64encode(b"ok").decode("ascii")
            return CommandResult(
                0,
                json.dumps({"return": {"exited": True, "exitcode": exitcode, "out-data": out}}),
                "",
            )
        return CommandResult(0, json.dumps({"return": {}}), "")

    def sequence(self) -> list[str]:
        """virsh subcommands and qemu-img verbs, in call order."""
        out: list[str] = []
        for argv in self.calls:
            if argv[0] == "virsh" and len(argv) > 3:
                out.append(argv[3])
            elif argv[0] == "qemu-img":
                out.append(f"qemu-img:{argv[1]}")
        return out


def _golden(tmp_path: Path, cfg: Config, name: str = "golden-omarchy", seat_type="desktop") -> Path:
    path = tmp_path / "images" / f"{name}.qcow2"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"golden")
    cfg.images[name] = ImageConfig(golden=str(path), seat_type=seat_type)
    return path


def _build_prov(tmp_path: Path, runner: _BuildRunner, cfg: Config) -> LibvirtProvisioner:
    prov = LibvirtProvisioner(
        cfg,
        base_dir=tmp_path,
        host_runner=runner,
        sleep=lambda _seconds: None,
    )
    (prov.nvram_dir / "omavroom-base_VARS.fd").write_bytes(b"vars")
    (prov.nvram_dir / "omavroom-term_VARS.fd").write_bytes(b"vars")
    return prov


def test_build_image_orchestration_and_commands(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    logs: list[str] = []
    path = prov.build_image(
        "golden-omarchy",
        name="omatype",
        packages=("wl-clipboard", "base-devel"),
        post=("systemctl enable --now foo",),
        on_log=logs.append,
    )
    assert path == str(tmp_path / "images" / "omatype.qcow2")
    assert Path(path).exists()
    assert (Path(path).stat().st_mode & 0o777) == 0o444
    # Guest commands, exactly and in order.
    assert runner.agent_commands == [
        "pacman -S --needed --noconfirm wl-clipboard base-devel",
        "systemctl enable --now foo",
        PACMAN_CLEAN_COMMAND,
    ]
    assert build_pacman_install_command(()) == "pacman -S --needed --noconfirm"
    # Host order: overlay, define, start, flatten, remove.
    seq = runner.sequence()
    assert seq.index("qemu-img:create") < seq.index("define") < seq.index("start")
    assert seq.index("start") < seq.index("qemu-img:convert") < seq.index("undefine")
    assert any("installing" in line for line in logs)


def test_build_image_delta_uses_existing_target(tmp_path: Path) -> None:
    cfg = Config.default()
    golden = _golden(tmp_path, cfg)
    target = tmp_path / "images" / "omatype.qcow2"
    target.write_bytes(b"existing")
    cfg.images["omatype"] = ImageConfig(golden=str(target), seat_type="desktop")
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    create = next(a for a in runner.calls if a[0] == "qemu-img" and "create" in a)
    backing = create[create.index("-b") + 1]
    assert backing == str(target)
    assert backing != str(golden)


def test_build_image_fresh_uses_base_golden(tmp_path: Path) -> None:
    cfg = Config.default()
    golden = _golden(tmp_path, cfg)
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    create = next(a for a in runner.calls if a[0] == "qemu-img" and "create" in a)
    assert create[create.index("-b") + 1] == str(golden)


def test_build_rejects_scratch_domain_in_use(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner(existing_domains={f"{BUILD_DOMAIN_PREFIX}omatype"})
    prov = _build_prov(tmp_path, runner, cfg)
    with pytest.raises(ProvisionerError, match="build domain already exists"):
        prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))


def test_build_failure_still_removes_scratch_vm(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner(fail_install=True)
    prov = _build_prov(tmp_path, runner, cfg)
    with pytest.raises(ProvisionerError, match="package install failed"):
        prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    assert f"{BUILD_DOMAIN_PREFIX}omatype" not in runner.defined
    assert not (tmp_path / "images" / "omatype.qcow2").exists()


def test_build_image_rejects_bad_name(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    with pytest.raises(ProvisionerError, match="invalid image name"):
        prov.build_image("golden-omarchy", name="../x")


# --------------------------------------------------------------------------
# manager build / approval / registry
# --------------------------------------------------------------------------
def test_manager_build_image_registers_and_delta(tmp_path: Path, user_config, make_manager) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    recipe_path = tmp_path / "image.toml"
    recipe_path.write_text(
        'base = "golden-omarchy"\npackages = ["wl-clipboard"]\npost = ["echo done"]\n',
        encoding="utf-8",
    )
    result = mgr.build_image("omatype", recipe_path=recipe_path, approved=True)
    assert result["base"] == "golden-omarchy"
    assert result["delta"] is False
    assert result["packages"] == ["wl-clipboard"]
    assert result["post"] == ["echo done"]
    assert mgr.config.images["omatype"].golden == result["path"]
    data = tomllib.loads(user_config.read_text(encoding="utf-8"))
    assert data["images"]["omatype"]["golden"] == result["path"]
    assert fake.built_images[0]["requested_base"] == "golden-omarchy"

    second = mgr.build_image("omatype", recipe_path=recipe_path, approved=True)
    assert second["delta"] is True
    assert fake.built_images[-1]["base"] == "omatype"


def test_manager_build_image_refuses_without_approval(make_manager, user_config) -> None:
    fake = FakeProvisioner()
    mgr = make_manager(provisioner=fake)
    with pytest.raises(ImageBuildNotApproved):
        mgr.build_image("omatype", packages=["wl-clipboard"], approved=False)
    assert fake.built_images == []


def test_manager_build_default_recipe_base(tmp_path: Path, make_manager, user_config) -> None:
    fake = FakeProvisioner()
    mgr = make_manager(provisioner=fake)
    recipe_path = tmp_path / "image.toml"
    recipe_path.write_text('packages = ["wl-clipboard"]\n', encoding="utf-8")
    result = mgr.build_image("omatype", recipe_path=recipe_path, approved=True)
    assert result["base"] == "golden-omarchy"
    assert fake.built_images[0]["requested_base"] == "golden-omarchy"


def test_recipe_base_can_be_a_project_image(make_manager, user_config) -> None:
    cfg = Config.default()
    cfg.images["base-img"] = ImageConfig(golden="base-img.qcow2", seat_type="desktop")
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake)
    mgr.build_image(
        "child",
        base="base-img",
        packages=["wl-clipboard"],
        approved=True,
    )
    assert fake.built_images[0]["requested_base"] == "base-img"


def test_manager_remove_image_refuses_project_binding(
    tmp_path: Path, make_manager, user_config
) -> None:
    cfg = Config.default()
    cfg.images["omatype"] = ImageConfig(golden=str(tmp_path / "omatype.qcow2"), seat_type="desktop")
    cfg.projects["p"] = ProjectConfig(image="omatype")
    mgr = make_manager(cfg, provisioner=FakeProvisioner())
    with pytest.raises(ValueError, match="bound to projects"):
        mgr.remove_image("omatype")


def test_manager_image_list_shows_projects(make_manager) -> None:
    cfg = Config.default()
    cfg.projects["omatype"] = ProjectConfig(image="golden-omarchy")
    mgr = make_manager(cfg, provisioner=FakeProvisioner())
    entries = {entry["name"]: entry for entry in mgr.list_images()}
    assert entries["golden-omarchy"]["projects"] == ["omatype"]


# --------------------------------------------------------------------------
# daemon / client round trips
# --------------------------------------------------------------------------
def test_daemon_image_build_requires_approval(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        try:
            job = client.image_build("omatype", packages=["wl-clipboard"], approved=False)
            with pytest.raises(DaemonRequestError) as excinfo:
                job.result(timeout=5)
        finally:
            client.close()
    assert excinfo.value.code == "build_not_approved"
    assert pool.manager.provisioner.built_images == []


def test_daemon_image_build_list_and_rm_round_trip(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        try:
            job = client.image_build(
                "omatype", base="golden-omarchy", packages=["wl-clipboard"], approved=True
            )
            result = job.result(timeout=10)
            assert result["name"] == "omatype"
            assert any(entry["name"] == "omatype" for entry in client.image_list())
            removed = client.image_rm("omatype")
            assert removed["name"] == "omatype"
            assert not any(entry["name"] == "omatype" for entry in client.image_list())
        finally:
            client.close()
    data = tomllib.loads(user_config.read_text(encoding="utf-8"))
    assert "omatype" not in data.get("images", {})


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
def test_mcp_image_tools_are_registered() -> None:
    assert {"image_list", "image_build", "image_rm"} <= set(MCP_TOOL_NAMES)


def test_mcp_image_build_refuses_without_approval(fake_daemon) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        tools = OmavroomTools(client, autostart_heartbeat=False)
        try:
            with pytest.raises(ValueError, match="approved=True"):
                tools.image_build("omatype", packages=["wl-clipboard"])
        finally:
            tools.stop()
            client.close()
    assert pool.manager.provisioner.built_images == []


def test_mcp_image_build_round_trip(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        tools = OmavroomTools(client, autostart_heartbeat=False)
        try:
            built = tools.image_build(
                "omatype", base="golden-omarchy", packages=["wl-clipboard"], approved=True
            )
            view = tools.job_wait(built["job_id"], timeout_s=10)
            assert view["state"] == "done"
            assert view["result"]["name"] == "omatype"
            assert any(entry["name"] == "omatype" for entry in tools.image_list())
            assert tools.image_rm("omatype")["name"] == "omatype"
        finally:
            tools.stop()
            client.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cli(pool, *args, socket_override=None):
    return cli.main(["--socket", str(socket_override or pool.socket_path), *args])


def test_cli_image_build_list_rm(fake_daemon, user_config, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    recipe_path = tmp_path / ".omavroom" / "image.toml"
    recipe_path.parent.mkdir(parents=True)
    recipe_path.write_text(
        'base = "golden-omarchy"\npackages = ["wl-clipboard"]\n', encoding="utf-8"
    )
    with fake_daemon() as pool:
        assert _cli(pool, "image", "build", "omatype", "--yes") == 0
        out = capsys.readouterr().out
        assert "omatype" in out and "delta=false" in out
        assert _cli(pool, "image", "list", "--json") == 0
        entries = json.loads(capsys.readouterr().out)
        assert any(entry["name"] == "omatype" for entry in entries)
        assert _cli(pool, "image", "rm", "omatype", "--yes") == 0
        assert "removed image omatype" in capsys.readouterr().out


def test_cli_image_build_prompts_and_aborts(
    fake_daemon, user_config, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    recipe_path = tmp_path / ".omavroom" / "image.toml"
    recipe_path.parent.mkdir(parents=True)
    recipe_path.write_text('packages = ["wl-clipboard"]\n', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *_args: "n")
    with fake_daemon() as pool:
        assert cli.main(["--socket", str(pool.socket_path), "image", "build", "omatype"]) == 0
    assert "aborted" in capsys.readouterr().out
    assert pool.manager.provisioner.built_images == []


def test_cli_image_build_prompt_shows_packages(
    fake_daemon, user_config, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    recipe_path = tmp_path / ".omavroom" / "image.toml"
    recipe_path.parent.mkdir(parents=True)
    recipe_path.write_text('packages = ["wl-clipboard", "base-devel"]\n', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *_args: "y")
    with fake_daemon() as pool:
        assert cli.main(["--socket", str(pool.socket_path), "image", "build", "omatype"]) == 0
    out = capsys.readouterr().out
    assert "packages: wl-clipboard, base-devel" in out


def test_cli_image_build_missing_recipe_errors(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["image", "build", "omatype", "--yes"]) == 1
    assert "no recipe" in capsys.readouterr().err
