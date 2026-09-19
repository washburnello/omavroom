"""Static seat addressing: ``[network]`` config, allocation, guest unit.

The goldens share a machine-id/DUID, so DHCP can hand two concurrent seats
the same lease. These tests cover the contained fix: a validated host
address range, unique per-seat allocation, and the deterministic
systemd-networkd unit the provisioner renders. No VM is booted.
"""

from __future__ import annotations

import pytest

from omavroom.config import Config, NetworkConfig
from omavroom.manager.libvirt_provisioner import build_static_network_config


def test_network_defaults() -> None:
    network = Config.default().network
    assert network.subnet_prefix == "192.168.122"
    assert network.gateway == "192.168.122.1"
    assert network.dns == ("192.168.122.1",)
    assert (network.host_range_start, network.host_range_end) == (200, 250)
    assert network.prefix_len == 24


def test_network_toml_override(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[network]\n"
        'subnet_prefix = "10.44.0"\n'
        'gateway = "10.44.0.1"\n'
        'dns = ["10.44.0.1", "1.1.1.1"]\n'
        "host_range_start = 10\n"
        "host_range_end = 12\n",
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.network.subnet_prefix == "10.44.0"
    assert cfg.network.dns == ("10.44.0.1", "1.1.1.1")
    assert cfg.network.ip_for_host(10) == "10.44.0.10"
    assert cfg.network.ip_for_host(12) == "10.44.0.12"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[network]\nsubnet_prefix = "10.44"\n', "network.subnet_prefix"),
        ('[network]\nsubnet_prefix = "10.44.0.1"\n', "network.subnet_prefix"),
        ('[network]\nsubnet_prefix = "10.44.256"\n', "network.subnet_prefix"),
        ('[network]\ngateway = "10.44.0.999"\n', "network.gateway"),
        ('[network]\ngateway = "192.168.123.1"\n', "network.gateway"),
        ("[network]\ndns = []\n", "network.dns"),
        ('[network]\ndns = ["nope"]\n', "network.dns"),
        ("[network]\nhost_range_start = 0\n", "network.host_range_start"),
        ("[network]\nhost_range_end = 255\n", "network.host_range_end"),
        (
            "[network]\nhost_range_start = 250\nhost_range_end = 200\n",
            "network.host_range_start",
        ),
        ("[network]\nbogus = 1\n", r"unknown keys in \[network\].*bogus"),
    ],
)
def test_bad_network_values_rejected(tmp_path, body: str, match: str) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_network_validates_directly() -> None:
    with pytest.raises(ValueError):
        NetworkConfig(subnet_prefix="192.168.122.1")
    with pytest.raises(ValueError):
        NetworkConfig(gateway="192.168.123.1")
    with pytest.raises(ValueError):
        NetworkConfig(dns=())
    with pytest.raises(ValueError):
        NetworkConfig(host_range_start=201, host_range_end=200)


def test_network_gateway_and_dns_derive_from_prefix() -> None:
    derived = NetworkConfig(subnet_prefix="10.20.30")
    assert derived.gateway == "10.20.30.1"
    assert derived.dns == ("10.20.30.1",)
    explicit = NetworkConfig(subnet_prefix="10.20.30", gateway="10.20.30.254", dns=("8.8.8.8",))
    assert explicit.gateway == "10.20.30.254"
    assert explicit.dns == ("8.8.8.8",)
    with pytest.raises(ValueError):
        NetworkConfig(subnet_prefix="10.20.30", gateway="10.20.31.1")


def test_network_single_key_update_derives_gateway(tmp_path) -> None:
    from omavroom.config import set_config_value

    cfg_path = tmp_path / "config.toml"
    set_config_value("network", "subnet_prefix", "10.9.9", path=cfg_path)
    cfg = Config.from_toml(cfg_path)
    assert cfg.network.gateway == "10.9.9.1"
    assert cfg.network.dns == ("10.9.9.1",)


def test_allocation_is_unique_and_within_range() -> None:
    network = Config.default().network
    used: set[str] = set()
    allocated: list[str] = []
    for i in range(20):
        ip = network.allocate(f"terminal-{i}", used)
        allocated.append(ip)
        used.add(ip)
    for ip in allocated:
        assert ip.startswith(network.subnet_prefix + ".")
        host = int(ip.rsplit(".", 1)[1])
        assert network.host_range_start <= host <= network.host_range_end
    assert len(set(allocated)) == len(allocated)
    assert len(used) == 20


def test_allocation_is_deterministic_per_seat() -> None:
    network = Config.default().network
    # Same pool state -> same address for a given seat name...
    first = network.allocate("terminal-7", set())
    assert network.allocate("terminal-7", set()) == first
    # ...and it moves deterministically to the next free slot on collision.
    second = network.allocate("terminal-7", {first})
    assert second != first
    assert network.allocate("terminal-7", {first}) == second


def test_allocation_exhaustion_raises() -> None:
    network = NetworkConfig(host_range_start=200, host_range_end=201)
    used = {"192.168.122.200", "192.168.122.201"}
    with pytest.raises(ValueError, match="no free address"):
        network.allocate("terminal-1", used)
    # A seat outside the range is still poolable when a slot is free.
    assert network.allocate("terminal-1", {"192.168.122.200"}) == "192.168.122.201"


def test_build_static_network_config_content() -> None:
    content = build_static_network_config(
        mac="52:54:00:4f:91:8e",
        ip="192.168.122.207",
        prefix_len=24,
        gateway="192.168.122.1",
        dns=("192.168.122.1", "1.1.1.1"),
    )
    assert "[Match]" in content
    assert "MACAddress=52:54:00:4f:91:8e" in content
    assert "[Network]" in content
    assert "Address=192.168.122.207/24" in content
    assert "Gateway=192.168.122.1" in content
    assert content.count("DNS=") == 2
    assert "DNS=192.168.122.1" in content
    assert "DNS=1.1.1.1" in content
