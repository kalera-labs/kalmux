"""The version is written in two places (pyproject.toml for the build, tmcore.VERSION for --version and the UI).
The release workflow compares the tag against pyproject only, so this is what keeps the two from drifting."""
import tomllib

from conftest import ROOT

from kalmux import tmcore


def test_pyproject_and_tmcore_agree_on_the_version():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        assert tomllib.load(fh)["project"]["version"] == tmcore.VERSION
