"""A Homebrew install, faked in tmp: every path Kalmux leaves behind must survive `brew upgrade`.

The formula runs `libexec.install Dir["*"]` and a python wrapper does `sys.path.insert(0, "<opt_libexec>/src")`,
so the modules are imported through /<prefix>/opt/kalmux, a symlink into /<prefix>/Cellar/kalmux/<version>.
An upgrade replaces the versioned directory, and a cleanup deletes the old one: anything that names it dies.
"""
import os
from pathlib import Path

import pytest

from kalmux import tmcore, tmsetup


@pytest.fixture
def brew(tmp_path):
    """<tmp>/Cellar/kalmux/0.5.1/libexec/{pyproject.toml,bin/kalmux,src/kalmux/assets/kalmux-hook} + <tmp>/opt/kalmux."""
    keg = tmp_path / "Cellar" / "kalmux" / "0.5.1"
    pkg = keg / "libexec" / "src" / "kalmux"
    (pkg / "assets").mkdir(parents=True)
    (pkg / "tmcore.py").write_text("")
    (pkg / "assets" / "kalmux-hook").write_text("#!/bin/bash\n")
    (keg / "libexec" / "bin").mkdir()
    (keg / "libexec" / "bin" / "kalmux").write_text("#!/usr/bin/env python3\n")
    (keg / "libexec" / "pyproject.toml").write_text("[project]\n")      # copied in by Dir["*"]: not a checkout
    (keg / "bin").mkdir()
    (keg / "bin" / "kalmux").write_text("#!/opt/python3\n")
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / "kalmux").symlink_to(keg)
    return {"prefix": tmp_path, "keg": keg, "pkg": pkg, "opt": tmp_path / "opt" / "kalmux"}


def test_homebrew_keg_splits_a_cellar_path(brew):
    inside = brew["keg"] / "libexec" / "src" / "kalmux"
    assert tmcore.homebrew_keg(inside) == (brew["prefix"], "kalmux", Path("libexec/src/kalmux"))
    assert tmcore.homebrew_keg(Path("/Users/k/Dev/kalmux/src/kalmux")) is None
    assert tmcore.homebrew_keg(Path("/x/Cellar/kalmux")) is None                # no version directory: not a keg path


def test_stable_path_maps_the_versioned_keg_to_opt(brew):
    hook = brew["pkg"] / "assets" / "kalmux-hook"
    assert tmcore.stable_path(hook) == brew["opt"] / "libexec/src/kalmux/assets/kalmux-hook"
    assert tmcore.stable_path(brew["opt"] / "bin" / "kalmux") == brew["opt"] / "bin" / "kalmux"
    elsewhere = Path("/Users/k/Dev/kalmux/src/kalmux/assets/kalmux-hook")
    assert tmcore.stable_path(elsewhere) == elsewhere


def test_stable_path_keeps_the_keg_path_when_opt_does_not_exist(brew):
    brew["opt"].unlink()
    hook = brew["pkg"] / "assets" / "kalmux-hook"
    assert tmcore.stable_path(hook) == hook


def test_package_dir_imported_through_opt_stays_on_opt(brew):
    """__file__ is <opt>/libexec/src/kalmux/tmcore.py; resolve() alone would walk into the Cellar."""
    via_opt = brew["opt"] / "libexec" / "src" / "kalmux" / "tmcore.py"
    assert tmcore.package_dir(via_opt) == brew["opt"] / "libexec" / "src" / "kalmux"
    assert tmcore.package_dir(brew["pkg"] / "tmcore.py") == brew["opt"] / "libexec" / "src" / "kalmux"


def test_a_homebrew_keg_is_not_a_source_checkout(brew):
    """pyproject.toml and bin/kalmux are copied into libexec, so the old file test said `checkout`."""
    assert tmcore.source_checkout(brew["opt"] / "libexec" / "src" / "kalmux") is None
    assert tmcore.source_checkout(brew["pkg"]) is None


def test_a_clone_is_a_source_checkout(tmp_path):
    clone = tmp_path / "kalmux"
    (clone / "src" / "kalmux").mkdir(parents=True)
    (clone / "bin").mkdir()
    (clone / "bin" / "kalmux").write_text("")
    (clone / "pyproject.toml").write_text("")
    assert tmcore.source_checkout(clone / "src" / "kalmux") == clone
    (clone / "bin" / "kalmux").unlink()
    assert tmcore.source_checkout(clone / "src" / "kalmux") is None


def test_this_checkout_is_detected_and_paths_are_stable():
    assert tmcore.SOURCE_CHECKOUT is not None and tmcore.ROOT == tmcore.SOURCE_CHECKOUT
    assert tmcore.homebrew_keg(tmcore.PKG_DIR) is None


def test_cli_path_from_path_lookup_lands_on_opt_not_the_cellar(brew, monkeypatch):
    """Invoked as something else (python -c), not in a checkout: `which kalmux` resolves the bin link into the
    Cellar. The opt form is what may be baked into anything that outlives this version."""
    shim = brew["prefix"] / "bin" / "kalmux"
    shim.parent.mkdir()
    shim.symlink_to(brew["keg"] / "bin" / "kalmux")
    monkeypatch.setattr(tmsetup, "SOURCE_CHECKOUT", None)
    monkeypatch.setattr(tmsetup.sys, "argv", ["/usr/bin/python3"])
    monkeypatch.setattr(tmsetup.sys, "executable", str(brew["prefix"] / "python" / "bin" / "python3"))
    monkeypatch.setattr(tmsetup.shutil, "which", lambda name: str(shim))
    assert tmsetup.cli_path() == brew["opt"] / "bin" / "kalmux"


def test_cli_path_invoked_through_a_cellar_path_is_mapped_to_opt(brew, monkeypatch):
    monkeypatch.setattr(tmsetup.sys, "argv", [str(brew["keg"] / "bin" / "kalmux"), "setup"])
    assert tmsetup.cli_path() == brew["opt"] / "bin" / "kalmux"


def test_links_into_any_kalmux_keg_are_ours(brew, tmp_path):
    """A link baked by an older version points into Cellar/kalmux/<old>: dangling after cleanup, still ours."""
    old = tmp_path / "links" / "old"
    old.parent.mkdir()
    old.symlink_to(brew["prefix"] / "Cellar" / "kalmux" / "0.4.0" / "bin" / "kalmux")
    assert tmsetup.link_is_ours(old)
    live = tmp_path / "links" / "live"
    live.symlink_to(brew["opt"] / "bin" / "kalmux")
    assert tmsetup.link_is_ours(live)
    other = tmp_path / "links" / "other"
    other.symlink_to(brew["prefix"] / "Cellar" / "tmux" / "3.7c" / "bin" / "tmux")
    assert not tmsetup.link_is_ours(other)


def test_autolaunch_bakes_the_stable_interpreter(brew, monkeypatch, tmp_path):
    """sys.executable can name a versioned python keg; AutoLaunch must keep working after `brew upgrade python`."""
    keg_python = brew["prefix"] / "Cellar" / "python@3.13" / "3.13.9" / "bin" / "python3.13"
    keg_python.parent.mkdir(parents=True)
    keg_python.write_text("")
    (brew["prefix"] / "opt" / "python@3.13").symlink_to(keg_python.parents[1])
    seen = {}
    monkeypatch.setattr(tmsetup, "autolaunch_state", lambda *a: "missing")

    def fake_run(cmd, timeout=0, env=None):
        seen["source"] = cmd[-1]
        Path(cmd[2]).write_text("compiled")
        return 0, ""
    monkeypatch.setattr(tmsetup, "_run", fake_run)
    tmsetup.install_autolaunch(tmp_path / "kalmux", tmp_path / "AutoLaunch.scpt", tmp_path / "stamp", python=str(keg_python))
    assert f"'{brew['prefix'] / 'opt' / 'python@3.13' / 'bin' / 'python3.13'}'" in seen["source"]
    assert "Cellar" not in seen["source"]
    assert os.path.exists(tmp_path / "stamp")
