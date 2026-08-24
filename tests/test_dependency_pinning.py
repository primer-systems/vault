"""The three places a dependency version is written must agree.

Vault states its dependencies three times, for three audiences:

  pyproject.toml     what `pip install primer-vault` resolves against
  requirements.txt   the direct dependencies, pinned, for a release build
  requirements.lock  the whole tree with hashes, which is what CI installs

Only the third protects a release binary. The first is what an ordinary user
gets, so it must not carry open ranges: a floor below anything tested claims
support for combinations nobody has run, and no ceiling lets a major released
that morning be pulled into a wallet.

The floors are now the tested versions and every dependency is capped below its
next breaking release. That is only worth anything while the files agree, and
three copies of a version number drift silently. These tests are the thing that
notices.

They read the files rather than the installed environment on purpose: a
developer's virtualenv falls behind the lock routinely, and that is not a defect
in the repository.
"""

import re
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


def normalise(name):
    """PEP 503 name normalisation - `argon2-cffi` and `Argon2_CFFI` are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def declared():
    """{name: Requirement} from pyproject."""
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    return {normalise(Requirement(d).name): Requirement(d) for d in deps}


def pinned():
    """{name: version} from requirements.txt - direct dependencies only."""
    out = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line.strip())
        if m:
            out[normalise(m.group(1))] = m.group(2)
    return out


def locked():
    """{name: version} from requirements.lock - the whole resolved tree."""
    out = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line)
        if m:
            out[normalise(m.group(1))] = m.group(2)
    return out


class TestTheFilesAgree:

    def test_every_declared_dependency_is_pinned(self):
        missing = sorted(set(declared()) - set(pinned()))
        assert not missing, (
            f"in pyproject.toml but not pinned in requirements.txt: {missing}")

    def test_every_pinned_dependency_is_declared(self):
        """A direct dependency the wheel does not declare is one a pip install
        never gets."""
        missing = sorted(set(pinned()) - set(declared()))
        assert not missing, (
            f"pinned in requirements.txt but not declared in pyproject.toml: {missing}")

    def test_every_pin_is_present_in_the_lock(self):
        gaps = {n: v for n, v in pinned().items()
                if locked().get(n) != v}
        assert not gaps, (
            "requirements.txt and requirements.lock disagree, so a release build "
            f"installs something other than what was pinned: {gaps}. Regenerate "
            "the lock with uv and commit both together.")


class TestTheRangesAreHonest:

    @pytest.mark.parametrize("name", sorted(declared()))
    def test_the_tested_version_satisfies_what_is_declared(self, name):
        """The version CI runs against must be one a pip install can resolve to."""
        requirement, version = declared()[name], pinned()[name]
        assert requirement.specifier.contains(version, prereleases=True), (
            f"{name}=={version} is what the suite runs against, but pyproject "
            f"declares {requirement.specifier}")

    @pytest.mark.parametrize("name", sorted(declared()))
    def test_the_floor_is_not_below_the_tested_version(self, name):
        """A floor under the tested version claims support for combinations that
        have never been run. That claim is the thing being removed."""
        requirement, version = declared()[name], Version(pinned()[name])
        floors = [Version(s.version) for s in requirement.specifier
                  if s.operator in (">=", "==", "~=")]
        assert floors, f"{name} declares no lower bound"
        assert max(floors) <= version
        assert max(floors).release[:2] == version.release[:2], (
            f"{name} floors at {max(floors)} but is tested at {version}; the "
            f"floor should track the tested minor")

    @pytest.mark.parametrize("name", sorted(declared()))
    def test_every_dependency_is_capped(self, name):
        """No ceiling means a major published this morning lands in a wallet."""
        requirement = declared()[name]
        assert any(s.operator in ("<", "<=", "==", "~=") for s in requirement.specifier), (
            f"{name} has no upper bound, so `pip install primer-vault` will "
            f"accept its next breaking release")

    @pytest.mark.parametrize("name", sorted(declared()))
    def test_the_cap_is_the_next_breaking_release(self, name):
        """Where a project puts its breaking changes depends on whether it has
        reached 1.0: before that, the minor is the breaking one."""
        requirement, version = declared()[name], Version(pinned()[name])
        caps = [Version(s.version) for s in requirement.specifier if s.operator == "<"]
        if not caps:
            pytest.skip(f"{name} is capped by something other than '<'")

        cap = min(caps)
        if version.major == 0:
            expected = Version(f"0.{version.minor + 1}")
        else:
            expected = Version(f"{version.major + 1}")
        assert cap == expected, (
            f"{name} is tested at {version} and capped at <{cap}; expected "
            f"<{expected}. Raise it deliberately once the suite passes against "
            f"the newer release.")
