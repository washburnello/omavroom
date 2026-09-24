"""Per-project image recipes: config, provisioner build, daemon/MCP/CLI.

No VMs: the libvirt build path is driven through a recorded host runner that
answers ``virsh``/``qemu-img``/guest-agent calls, so the *exact* install
commands and the delta base are asserted (never fake-masked). The
daemon/client/MCP/CLI layers run in-process against a FakeProvisioner.
"""

from __future__ import annotations

import base64
import json
import re
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
    BUILD_DNS_PROBE,
    BUILD_DOMAIN_PREFIX,
    PACMAN_CLEAN_COMMAND,
    CommandResult,
    LibvirtProvisioner,
    _pacman_nothing_to_do,
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


def test_register_image_persists_recorded_packages(user_config) -> None:
    register_image(
        "omatype",
        "/tmp/omatype.qcow2",
        base="golden-omarchy",
        packages=["rustup", "base-devel"],
        recipe_hash="abc",
    )
    data = tomllib.loads(user_config.read_text(encoding="utf-8"))
    assert data["images"]["omatype"]["packages"] == ["rustup", "base-devel"]
    assert data["images"]["omatype"]["base"] == "golden-omarchy"
    # Round-trips through the config loader so image_status/plan see them.
    assert Config.load().images["omatype"].packages == ("rustup", "base-devel")


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
        self.defined_xml: dict[str, str] = {}
        self.fail_install = fail_install
        # Scripted DNS readiness: when set, the DNS probe command fails this
        # many times before succeeding (models the golden's boot-time race).
        self.dns_failures = 0
        self.dns_probe_attempts = 0
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
            self.defined_xml[name] = xml
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
            return self._agent_result(command)
        return CommandResult(0, json.dumps({"return": {}}), "")

    def _agent_result(self, command: str) -> CommandResult:
        """Exit code + captured stdout for one recorded guest command."""
        if "pacman -S" in command:
            if self.fail_install:
                err = b"error: failed retrieving file 'foo' : Could not resolve host: h"
                return self._status(1, out=b"", err=err)
            return self._status(0, out=b"ok")
        # DNS readiness probe: fail a scripted number of times, then succeed.
        if "getent hosts" in command or "resolvectl query" in command:
            self.dns_probe_attempts += 1
            if self.dns_probe_attempts <= self.dns_failures:
                return self._status(2, out=b"")
            return self._status(0, out=b"1.2.3.4\n")
        if "NET_STATIC_OK" in command or "NET_STATIC_MISSING" in command:
            return self._status(0, out=b"NET_STATIC_OK\n")
        if "NET_CLEANED" in command:
            return self._status(0, out=b"NET_CLEANED\n")
        return self._status(0, out=b"ok")

    @staticmethod
    def _status(exitcode: int, *, out: bytes = b"", err: bytes = b"") -> CommandResult:
        return CommandResult(
            0,
            json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": exitcode,
                        "out-data": base64.b64encode(out).decode("ascii"),
                        "err-data": base64.b64encode(err).decode("ascii"),
                    }
                }
            ),
            "",
        )

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
    # Guest commands: the build network is configured and DNS is proven BEFORE
    # pacman, then the post command, cache clean, and the build-only unit is
    # removed before the overlay is flattened.
    commands = runner.agent_commands
    pacman_at = commands.index("pacman -S --needed --noconfirm wl-clipboard base-devel")
    configure_at = next(i for i, c in enumerate(commands) if "NET_STATIC_OK" in c)
    dns_at = next(i for i, c in enumerate(commands) if "getent hosts" in c)
    assert configure_at < dns_at < pacman_at
    assert "systemctl enable --now foo" in commands
    assert PACMAN_CLEAN_COMMAND in commands
    clean_at = next(i for i, c in enumerate(commands) if "NET_CLEANED" in c)
    assert commands.index(PACMAN_CLEAN_COMMAND) < clean_at
    # The static unit must be MAC-matched and match the build VM's MAC.
    static_cmd = commands[configure_at]
    assert "Address=192.168.122." in static_cmd
    assert "Gateway=192.168.122.1" in static_cmd
    assert "DNS=192.168.122.1" in static_cmd
    assert build_pacman_install_command(()) == "pacman -S --needed --noconfirm"
    # Host order: overlay, define, start, flatten, remove.
    seq = runner.sequence()
    assert seq.index("qemu-img:create") < seq.index("define") < seq.index("start")
    assert seq.index("start") < seq.index("qemu-img:convert") < seq.index("undefine")
    assert any("installing" in line for line in logs)
    assert any("configuring build network" in line for line in logs)
    assert any("waiting for DNS" in line for line in logs)
    # The flattened image must not keep the build-only static unit baked in.
    assert "rm -f /etc/systemd/network/10-omavroom-static.network" in static_cmd or any(
        "rm -f /etc/systemd/network/10-omavroom-static.network" in c for c in commands
    )


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
# FIX 1: build-VM networking before pacman (+ DNS wait, + cleanup)
# --------------------------------------------------------------------------
def test_build_network_configured_before_pacman_with_unique_ip(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    commands = runner.agent_commands
    configure = next(c for c in commands if "NET_STATIC_OK" in c)
    dns = next(c for c in commands if "getent hosts" in c)
    pacman = next(c for c in commands if c.startswith("pacman -S"))
    # Configure -> prove DNS -> install. Never install before the network works.
    assert commands.index(configure) < commands.index(dns) < commands.index(pacman)
    # The unit matches the build VM's MAC and uses the configured network.
    domain_xml = runner.defined_xml[f"{BUILD_DOMAIN_PREFIX}omatype"]
    mac = re.search(r"<mac address=['\"]([^'\"]+)['\"]", domain_xml).group(1)
    assert f"MACAddress={mac}" in configure
    assert "systemctl restart systemd-networkd" in configure
    assert "Address=192.168.122." in configure
    assert "Gateway=192.168.122.1" in configure
    assert "DNS=192.168.122.1" in configure
    # A unique address is allocated (within the configured pool), never one a
    # seat already holds.
    allocated = configure.split("Address=", 1)[1].split("/", 1)[0]
    assert (
        cfg.network.host_range_start
        <= int(allocated.rsplit(".", 1)[1])
        <= (cfg.network.host_range_end)
    )
    assert BUILD_DNS_PROBE in dns


def test_build_network_is_removed_before_flatten(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    commands = runner.agent_commands
    cleanup = next(c for c in commands if "NET_CLEANED" in c)
    assert "rm -f /etc/systemd/network/10-omavroom-static.network" in cleanup
    assert "systemctl restart systemd-networkd" in cleanup
    # Cleanup happens after the cache clean but before the overlay is flattened.
    assert commands.index(PACMAN_CLEAN_COMMAND) < commands.index(cleanup)
    seq = runner.sequence()
    assert seq.index("qemu-img:convert") > seq.index("qemu-img:create")
    # The cleanup guest command is issued before the host flatten (convert).
    convert_at = next(
        i for i, a in enumerate(runner.calls) if a[0] == "qemu-img" and "convert" in a
    )
    cleanup_at = next(
        i for i, a in enumerate(runner.calls) if a[0] == "virsh" and "NET_CLEANED" in str(a)
    )
    assert cleanup_at < convert_at


def test_build_dns_timeout_raises_clear_error(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner()
    runner.dns_failures = 10_000  # never resolves
    prov = _build_prov(tmp_path, runner, cfg)
    prov.build_network_timeout_s = 0  # no waiting: fail on the first probe
    with pytest.raises(ProvisionerError, match="no working network/DNS") as excinfo:
        prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    message = str(excinfo.value)
    assert BUILD_DNS_PROBE in message
    assert "192.168.122.1" in message  # the gateway/dns actually used
    # The scratch VM is still cleaned up on the DNS failure.
    assert f"{BUILD_DOMAIN_PREFIX}omatype" not in runner.defined


def test_build_image_noop_install_is_success(tmp_path: Path) -> None:
    """A delta where every package is already present must not fail."""

    class _NoopRunner(_BuildRunner):
        def _agent_result(self, command: str) -> CommandResult:
            if command.startswith("pacman -S ") and "--needed" in command:
                warning = (
                    b"warning: wl-clipboard-1.0-1 is up to date -- skipping\n"
                    b"warning: there is nothing to do\n"
                )
                return self._status(1, out=warning)
            return super()._agent_result(command)

    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _NoopRunner()
    prov = _build_prov(tmp_path, runner, cfg)
    # Exits cleanly instead of raising on pacman's exit 1 "nothing to do".
    path = prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))
    assert Path(path).exists()


def test_build_image_real_pacman_error_still_fails(tmp_path: Path) -> None:
    cfg = Config.default()
    _golden(tmp_path, cfg)
    runner = _BuildRunner(fail_install=True)
    prov = _build_prov(tmp_path, runner, cfg)
    with pytest.raises(ProvisionerError, match="package install failed"):
        prov.build_image("golden-omarchy", name="omatype", packages=("wl-clipboard",))


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, "", True),
        (1, "warning: git-2.55.0-1 is up to date -- skipping", True),
        (1, "warning: there is nothing to do", True),
        (1, "warning: x is up to date -- skipping\nwarning: there is nothing to do", True),
        (
            1,
            "error: failed retrieving file 'rustup' from m : Could not resolve host: m",
            False,
        ),
        (1, "error: failed to commit transaction (conflicting files)", False),
        (1, "error: target not found: nope", False),
        (2, "warning: something is up to date -- skipping", True),
    ],
)
def test_pacman_nothing_to_do_classification(returncode: int, output: str, expected: bool) -> None:
    assert _pacman_nothing_to_do(returncode, output) is expected


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
