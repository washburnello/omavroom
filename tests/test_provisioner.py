"""Phase 4 provisioner interface + FakeProvisioner behavior."""

from __future__ import annotations

import pytest

from omavroom.manager.provisioner import (
    ExportSpec,
    FakeProvisioner,
    InputEvent,
    ProvisionerError,
    RepoSpec,
    ResourceCaps,
)

CAPS = ResourceCaps(cpu_vcpus=2, memory_mb=2048, overlay_max_gb=10)


def test_fake_records_full_lifecycle() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "omavroom-base", CAPS)
    fake.apply_resource_limits(vm, CAPS)
    fake.start(vm)
    fake.wait_ready(vm, 300)
    fake.prepare_repo(vm, RepoSpec(url="git@example/repo.git", branch="main"))
    fake.reset(vm)
    fake.stop(vm)
    fake.destroy(vm)

    assert vm == "fake://terminal-1"
    assert fake.created == [vm]
    assert fake.destroyed == [vm]
    assert fake.resets == [vm]
    assert fake.applied_limits[vm] is CAPS
    assert fake.call_names() == [
        "create_from_image",
        "apply_resource_limits",
        "start",
        "wait_ready",
        "prepare_repo",
        "reset",
        "stop",
        "destroy",
    ]


def test_destroy_is_idempotent() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    fake.destroy(vm)
    fake.destroy(vm)
    assert fake.destroyed == [vm]


def test_repo_spec_has_no_credentials() -> None:
    spec = RepoSpec(url="git@example/repo.git", branch="main")
    assert not hasattr(spec, "key")


def test_fail_on_raises_provisioner_error() -> None:
    fake = FakeProvisioner(fail_on={"start"})
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    with pytest.raises(ProvisionerError):
        fake.start(vm)


def test_raise_on_injects_arbitrary_exception() -> None:
    fake = FakeProvisioner(raise_on={"start": OSError("libvirt blew up")})
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    with pytest.raises(OSError):
        fake.start(vm)


def test_broken_seat_fails_wait_ready() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    fake.break_seat("terminal-1")
    with pytest.raises(ProvisionerError):
        fake.wait_ready(vm, 300)


def test_fetch_and_push_are_separate_steps() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    spec = ExportSpec(repo="demo", branch="task")
    fetched = fake.fetch_bundle(vm, spec)
    assert fetched.ok is True
    assert fetched.sha and len(fetched.sha) == 40
    assert fetched.changed_paths == ("src/app.py",)
    assert fetched.spec is spec
    pushed = fake.push(spec, fetched)
    assert pushed.ok is True
    assert pushed.sha == fetched.sha
    assert fake.exported == [vm]
    assert fake.call_names() == ["create_from_image", "fetch_bundle", "push"]


def test_export_failure_is_reported_not_raised() -> None:
    fake = FakeProvisioner(fail_exports=True)
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    result = fake.fetch_bundle(vm, ExportSpec(repo="demo"))
    assert result.ok is False
    assert "export" in result.message


def test_sha_mismatch_fails_push() -> None:
    fake = FakeProvisioner(sha_mismatch=True)
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    spec = ExportSpec(repo="demo")
    pushed = fake.push(spec, fake.fetch_bundle(vm, spec))
    assert pushed.ok is False
    assert "SHA" in pushed.message


def test_adopt_seam_lists_and_attaches() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    fake.start(vm)
    infos = fake.list_vms()
    assert [info.ref for info in infos] == [vm]
    assert infos[0].state == "running"
    assert fake.attach(vm) is not None
    assert fake.attach("fake://missing") is None


def test_orphan_injection_and_drop_helpers() -> None:
    fake = FakeProvisioner()
    ref = fake.inject_vm("orphan-1")
    assert fake.attach(ref) is not None
    fake.drop_vm(ref)
    assert fake.attach(ref) is None
    assert fake.destroyed == []


def test_desktop_ops_seam() -> None:
    fake = FakeProvisioner()
    vm = fake.create_from_image("desktop-1", "desktop", "img", CAPS)
    shot = fake.screenshot(vm, max_width=320)
    assert shot.startswith(b"PNG:desktop-1:320x")
    fake.input(vm, [InputEvent("key", "Return"), InputEvent("text", "hi")])
    assert [event.value for event in fake.inputs[vm]] == ["Return", "hi"]
    assert fake.peek_endpoint(vm).startswith("vnc://")
    with pytest.raises(ProvisionerError):
        fake.screenshot("fake://missing")


def test_input_event_validates_kind() -> None:
    with pytest.raises(ValueError):
        InputEvent("teleport", "x")
