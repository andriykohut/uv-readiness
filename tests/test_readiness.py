import http.client
import io
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from uv_readiness import (
    Pkg,
    RelockError,
    Report,
    SetupError,
    Target,
    analyze,
    check_classifiers,
    classify,
    fetch_classifiers,
    main,
    parse_target,
    relock,
    render_json,
    render_text,
    trim,
    wheel_supports,
)

PY313 = Target(13, False)
PY313T = Target(13, True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.13", (13, False)), ("3.13.2", (13, False)), ("3.14t", (14, True))],
)
def test_parse_target(text: str, expected: tuple[int, bool]) -> None:
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["", "3", "2.7", "py313", "3.x", "313"])
def test_parse_target_rejects_other_text(text: str) -> None:
    with pytest.raises(SetupError):
        parse_target(text)


def test_target_formats() -> None:
    assert (str(PY313), PY313.tag) == ("3.13", "cp313")
    assert (str(PY313T), PY313T.tag) == ("3.13t", "cp313t")


@pytest.mark.parametrize(
    ("filename", "standard", "free_threaded"),
    [
        ("six-1.17.0-py2.py3-none-any.whl", True, True),
        ("idna-3.20-py3-none-any.whl", True, True),
        ("foo-1.0-py313-none-any.whl", True, True),
        ("foo-1.0-py314-none-any.whl", False, False),
        ("foo-1.0-py2-none-any.whl", False, False),
        ("numpy-2.5.3-cp313-cp313-macosx_14_0_arm64.whl", True, False),
        ("numpy-2.5.3-cp313-cp313t-macosx_14_0_arm64.whl", False, True),
        ("numpy-1.26.4-cp312-cp312-macosx_11_0_arm64.whl", False, False),
        ("cryptography-44.0.0-cp39-abi3-manylinux_2_28_x86_64.whl", True, False),
        ("foo-1.0-cp314-abi3-manylinux_2_28_x86_64.whl", False, False),
        ("foo-1.0-cp313-none-win_amd64.whl", True, True),
        ("foo-1.0-pp311-pypy311_pp73-win_amd64.whl", False, False),
        ("garbage.whl", False, False),
    ],
)
def test_wheel_supports(filename: str, standard: bool, free_threaded: bool) -> None:
    assert wheel_supports(filename, PY313) is standard
    assert wheel_supports(filename, PY313T) is free_threaded


REGISTRY = {"registry": "https://pypi.org/simple"}


def dep(name: str, **fields: str | list[str]) -> dict[str, Any]:
    return {"name": name, **fields}


def pkg(
    name: str,
    version: str = "1.0",
    *,
    wheels: Sequence[str] = (),
    deps: Sequence[dict[str, Any]] = (),
    optional: dict[str, list[dict[str, Any]]] | None = None,
    source: dict[str, str] | None = None,
    sdist: bool = True,
) -> dict[str, Any]:
    package: dict[str, Any] = {
        "name": name,
        "version": version,
        "source": source or REGISTRY,
        "dependencies": list(deps),
        "optional-dependencies": optional or {},
        "wheels": [{"url": f"https://files.example/{wheel}"} for wheel in wheels],
    }
    if sdist:
        package["sdist"] = {"url": f"https://files.example/{name}-{version}.tar.gz"}
    return package


def project(
    *deps: dict[str, Any],
    optional: dict[str, list[dict[str, Any]]] | None = None,
    groups: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    return {
        "name": "app",
        "version": "0",
        "source": {"virtual": "."},
        "dependencies": list(deps),
        "optional-dependencies": optional or {},
        "dev-dependencies": groups or {},
    }


def lock(*packages: dict[str, Any], requires_python: str = ">=3.10") -> dict[str, Any]:
    return {"version": 1, "requires-python": requires_python, "package": list(packages)}


def statuses(packages: Sequence[Pkg]) -> dict[str, str]:
    return {p.name: p.status for p in packages}


PURE = "x-1.0-py3-none-any.whl"


def test_classify_statuses() -> None:
    result = classify(
        lock(
            project(dep("a"), dep("b"), dep("c"), dep("d")),
            pkg("a", wheels=["a-1.0-cp313-cp313-win_amd64.whl"]),
            pkg("b", wheels=["b-1.0-cp312-cp312-win_amd64.whl"]),
            pkg("c"),
            pkg("d", source={"git": "https://example.com/d.git"}, sdist=False),
        ),
        PY313,
    )
    assert statuses(result) == {
        "a": "ready",
        "b": "no-wheel",
        "c": "source-only",
        "d": "unchecked",
    }


def test_marker_prunes_dependencies_not_installed_on_target() -> None:
    result = classify(
        lock(
            project(
                dep("tomli", marker="python_full_version < '3.11'"),
                dep("colorama", marker="sys_platform == 'win32'"),
                dep("uvloop", marker="platform_machine == 'aarch64'"),
            ),
            pkg("tomli", wheels=["tomli-1.0-cp310-cp310-win_amd64.whl"]),
            pkg("colorama", wheels=[PURE]),
            pkg("uvloop", wheels=["uvloop-1.0-cp313-cp313-manylinux_2_28_aarch64.whl"]),
        ),
        PY313,
    )
    assert statuses(result) == {"colorama": "ready", "uvloop": "ready"}


def test_fork_is_selected_by_marker() -> None:
    result = classify(
        lock(
            project(
                dep("numpy", version="1.26.4", marker="python_full_version < '3.12'"),
                dep("numpy", version="2.5.3", marker="python_full_version >= '3.12'"),
            ),
            pkg("numpy", "1.26.4", wheels=["numpy-1.26.4-cp311-cp311-win_amd64.whl"]),
            pkg("numpy", "2.5.3", wheels=["numpy-2.5.3-cp313-cp313-win_amd64.whl"]),
        ),
        PY313,
    )
    assert [(p.name, p.version, p.status) for p in result] == [("numpy", "2.5.3", "ready")]


def test_extras_and_groups_are_followed() -> None:
    result = classify(
        lock(
            project(
                dep("httpx", extra=["http2"]),
                optional={"yaml": [dep("pyyaml")]},
                groups={"dev": [dep("pytest")]},
            ),
            pkg("httpx", wheels=[PURE], optional={"http2": [dep("h2")], "brotli": [dep("brotli")]}),
            pkg("h2", wheels=[PURE]),
            pkg("brotli", wheels=[PURE]),
            pkg("pyyaml", wheels=[PURE]),
            pkg("pytest", wheels=[PURE]),
        ),
        PY313,
    )
    assert set(statuses(result)) == {"httpx", "h2", "pyyaml", "pytest"}


def test_required_by_lists_direct_dependents() -> None:
    result = classify(
        lock(
            project(dep("scipy"), dep("pandas")),
            pkg("scipy", wheels=[PURE], deps=[dep("numpy")]),
            pkg("pandas", wheels=[PURE], deps=[dep("numpy")]),
            pkg("numpy", wheels=[PURE]),
        ),
        PY313,
    )
    assert {p.name: p.required_by for p in result} == {
        "numpy": ["pandas", "scipy"],
        "pandas": ["app"],
        "scipy": ["app"],
    }


def test_dependency_cycle_terminates() -> None:
    result = classify(
        lock(
            project(dep("a")),
            pkg("a", wheels=[PURE], deps=[dep("b")]),
            pkg("b", wheels=[PURE], deps=[dep("a")]),
        ),
        PY313,
    )
    assert statuses(result) == {"a": "ready", "b": "ready"}


def test_lock_without_project_entry_reports_everything() -> None:
    result = classify(lock(pkg("a", wheels=[PURE]), pkg("b")), PY313)
    assert statuses(result) == {"a": "ready", "b": "source-only"}
    assert all(p.required_by == [] for p in result)


def test_percent_encoded_wheel_url_with_fragment() -> None:
    torch = pkg("torch", "2.5.0+cpu")
    torch["wheels"] = [
        {
            "url": "https://download.pytorch.org/whl/cpu/torch-2.5.0%2Bcpu-cp313-cp313-linux_x86_64.whl#sha256=abc"
        }
    ]
    assert statuses(classify(lock(project(dep("torch")), torch), PY313)) == {"torch": "ready"}


def test_direct_url_wheel_source() -> None:
    local = pkg(
        "local", source={"url": "https://example.com/local-1.0-py3-none-any.whl"}, sdist=False
    )
    assert statuses(classify(lock(project(dep("local")), local), PY313)) == {"local": "ready"}


def test_narrowed_lock_uses_original_to_tell_no_wheel_from_source_only() -> None:
    original = lock(
        project(dep("numpy"), dep("legacy")),
        pkg("numpy", "1.26.4", wheels=["numpy-1.26.4-cp312-cp312-win_amd64.whl"]),
        pkg("legacy"),
    )
    narrowed = lock(
        project(dep("numpy"), dep("legacy")),
        pkg("numpy", "1.26.4"),
        pkg("legacy"),
        requires_python="==3.13.*",
    )
    assert statuses(classify(narrowed, PY313, original)) == {
        "numpy": "no-wheel",
        "legacy": "source-only",
    }


PYPROJECT = """\
[project]
name = "app"
version = "0"
requires-python = ">=3.10"
dependencies = ["idna", "numpy"]
"""

LOCK_TOML = """\
version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "app"
version = "0"
source = { virtual = "." }
dependencies = [
    { name = "idna" },
    { name = "numpy" },
]

[[package]]
name = "idna"
version = "3.20"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/idna-3.20.tar.gz" }
wheels = [
    { url = "https://files.example/idna-3.20-py3-none-any.whl" },
]

[[package]]
name = "numpy"
version = "1.26.4"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/numpy-1.26.4.tar.gz" }
wheels = [
    { url = "https://files.example/numpy-1.26.4-cp312-cp312-win_amd64.whl" },
]
"""

NO_SOLUTION = """\
Using CPython 3.13.7
Resolving despite existing lockfile due to removal of global exclude newer
  × No solution found when resolving dependencies for split (markers:
  │ python_full_version >= '3.12' and sys_platform == 'win32'):
  ╰─▶ Because only the following versions of scipy are available:
          scipy==0.8.0
          scipy==0.9.0
          scipy>=1.12
      and scipy<=1.11.4 has no usable wheels, we can conclude that
      scipy<=1.11.4 cannot be used.
      And because your project depends on scipy<1.12, we can conclude that
      your project's requirements are unsatisfiable.

      hint: Wheels are required for `scipy` because building from source is
      disabled for `scipy` (i.e., with `--no-build-package scipy`)
"""


def make_project(root: Path, pyproject: str = PYPROJECT, lock_text: str = LOCK_TOML) -> Path:
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "uv.lock").write_text(lock_text, encoding="utf-8")
    return root


FORKED_LOCK = LOCK_TOML.replace(
    'requires-python = ">=3.10"\n',
    'requires-python = ">=3.10"\nresolution-markers = [\n'
    "    \"python_full_version >= '3.12'\",\n    \"python_full_version < '3.12'\",\n]\n",
)
NARROWED_LOCK = LOCK_TOML.replace('requires-python = ">=3.10"', 'requires-python = "==3.13.*"')


class FakeRun:
    """Stands in for subprocess.run and records what uv would have seen."""

    def __init__(
        self, returncode: int = 0, stderr: str = "", lock_text: str = NARROWED_LOCK
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.lock_text = lock_text
        self.commands: list[list[str]] = []
        self.cwd = Path()
        self.pyproject = ""
        self.lock = ""
        self.env: dict[str, str] = {}

    def __call__(
        self, command: list[str], *, cwd: Path, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.env = env or {}
        self.cwd = Path(cwd)
        self.pyproject = (self.cwd / "pyproject.toml").read_text(encoding="utf-8")
        self.lock = (self.cwd / "uv.lock").read_text(encoding="utf-8")
        (self.cwd / "uv.lock").write_text(self.lock_text, encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def test_relock_runs_uv_on_a_narrowed_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = make_project(tmp_path, lock_text=FORKED_LOCK)
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)

    result = relock(project_dir, PY313, "--upgrade")

    copy = str(run.cwd)
    assert run.commands == [
        ["uv", "lock", "--project", copy, "--directory", copy, "--python", "3.13", "--upgrade"]
    ]
    assert 'requires-python = "==3.13.*"' in run.pyproject
    assert result["requires-python"] == "==3.13.*"
    assert run.cwd != project_dir
    assert not run.cwd.exists()
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT
    assert (project_dir / "uv.lock").read_text(encoding="utf-8") == FORKED_LOCK


def test_relock_makes_uv_resolve_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    relock(make_project(tmp_path, lock_text=FORKED_LOCK), PY313)
    # the copied lock keeps its own Python range, so uv sees it is stale and resolves again,
    # and loses its fork markers, which belong to that old range
    assert 'requires-python = ">=3.10"' in run.lock
    assert "resolution-markers" not in run.lock
    assert tomllib.loads(run.lock)["package"] == tomllib.loads(LOCK_TOML)["package"]


def test_relock_hides_uv_frozen_and_locked_from_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_FROZEN", "1")
    monkeypatch.setenv("UV_LOCKED", "1")
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    relock(make_project(tmp_path), PY313)
    assert "PATH" in run.env
    assert "UV_FROZEN" not in run.env
    assert "UV_LOCKED" not in run.env


def test_relock_rejects_a_lock_that_lost_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = FakeRun(lock_text='version = 1\nrequires-python = "==3.13.*"\n')
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    with pytest.raises(RelockError, match="without the project"):
        relock(make_project(tmp_path), PY313)


def test_relock_gives_a_dynamically_versioned_project_a_static_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = (
        '[project]\nname = "app"\ndynamic = ["version", "readme"]\n'
        'requires-python = ">=3.10"\ndependencies = ["idna", "numpy"]\n\n'
        '[tool.hatch.version]\npath = "src/app/__init__.py"\n'
    )
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    relock(make_project(tmp_path, pyproject=pyproject), PY313)
    copied = tomllib.loads(run.pyproject)["project"]
    assert copied["version"] == "0"
    assert copied["dynamic"] == ["readme"]
    assert copied["dependencies"] == ["idna", "numpy"]


def test_relock_raises_with_uv_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uv_readiness.subprocess.run", FakeRun(1, NO_SOLUTION))
    with pytest.raises(RelockError, match="No solution found"):
        relock(make_project(tmp_path), PY313)


def test_relock_refuses_member_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_text = (
        'version = 1\nrequires-python = ">=3.10"\n\n[manifest]\nmembers = ["shared"]\n\n'
        '[[package]]\nname = "shared"\nversion = "0"\nsource = { editable = "../shared" }\n'
    )
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    with pytest.raises(RelockError, match="outside the project"):
        relock(make_project(tmp_path, lock_text=lock_text), PY313)
    assert run.commands == []


def test_relock_needs_a_requires_python_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)
    with pytest.raises(RelockError, match="requires-python"):
        relock(make_project(tmp_path, pyproject='[project]\nname = "app"\n'), PY313)
    assert run.commands == []


def test_relock_reports_missing_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: object, **__: object) -> None:
        raise FileNotFoundError("uv")

    monkeypatch.setattr("uv_readiness.subprocess.run", missing)
    with pytest.raises(SetupError, match="not found on PATH"):
        relock(make_project(tmp_path), PY313)


def test_trim_keeps_only_the_derivation() -> None:
    result = trim(NO_SOLUTION)
    assert result.splitlines()[0].startswith("× No solution found")
    assert result.count("…") == 1
    assert "scipy==0.8.0" not in result
    assert "scipy>=1.12" not in result
    assert "your project depends on scipy<1.12" in result
    assert "hint:" not in result
    assert "Using CPython" not in result


ALL = frozenset({"scipy", "pandas", "numpy", "pyyaml"})


def stack(
    *,
    scipy: str = "1.11.4",
    pandas: str = "2.2.1",
    numpy: str = "1.26.4",
    pyyaml: str = "6.0.1",
    ready: frozenset[str] = frozenset(),
    requires_python: str = ">=3.10",
) -> dict[str, Any]:
    """A lock whose packages have a cp312 wheel only, except those named in `ready` (cp313)."""

    def entry(name: str, version: str, *deps: dict[str, Any]) -> dict[str, Any]:
        tag = "cp313" if name in ready else "cp312"
        return pkg(name, version, wheels=[f"{name}-{version}-{tag}-{tag}-win_amd64.whl"], deps=deps)

    return lock(
        project(dep("scipy"), dep("pandas"), dep("pyyaml")),
        entry("scipy", scipy, dep("numpy")),
        entry("pandas", pandas, dep("numpy")),
        entry("numpy", numpy),
        entry("pyyaml", pyyaml),
        requires_python=requires_python,
    )


class FakeUv:
    """A relocker that answers each kind of `uv lock` run with a canned lock or error."""

    def __init__(self, **results: dict[str, Any] | Exception) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> dict[str, Any]:
        self.calls.append(args)
        if "--no-build-package" in args:
            kind = "why"
        elif args == ("--upgrade",):
            kind = "full"
        else:
            kind = "targeted" if args else "plain"
        result = self.results[kind]
        if isinstance(result, Exception):
            raise result
        return result


def test_ready_lock_never_runs_uv() -> None:
    uv = FakeUv()
    report = analyze(stack(ready=ALL), PY313, uv)
    assert report.verdict == "ready"
    assert set(statuses(report.packages).values()) == {"ready"}
    assert uv.calls == []


def test_offline_leaves_packages_without_a_wheel() -> None:
    uv = FakeUv()
    report = analyze(stack(), PY313, uv, offline=True)
    assert set(statuses(report.packages).values()) == {"no-wheel"}
    assert report.verdict == "not ready"
    assert report.notes == ["run without --offline to check for fixes"]
    assert uv.calls == []


def test_fixable_and_blocked_packages_are_separated() -> None:
    partly = stack(pandas="2.3.3", pyyaml="6.0.3", ready=frozenset({"pandas", "pyyaml"}))
    uv = FakeUv(targeted=partly, full=partly, why=RelockError(NO_SOLUTION))
    steps: list[str] = []

    report = analyze(stack(), PY313, uv, step=steps.append)

    assert statuses(report.packages) == {
        "numpy": "blocked",
        "pandas": "update",
        "pyyaml": "update",
        "scipy": "blocked",
    }
    assert {p.name: p.new_version for p in report.packages if p.status == "update"} == {
        "pandas": "2.3.3",
        "pyyaml": "6.0.3",
    }
    assert report.fix_command == "uv lock --upgrade-package pandas --upgrade-package pyyaml"
    assert "your project depends on scipy<1.12" in (report.uv_explanation or "")
    assert report.verdict == "not ready"
    assert uv.calls == [
        (
            "--upgrade-package",
            "numpy",
            "--upgrade-package",
            "pandas",
            "--upgrade-package",
            "pyyaml",
            "--upgrade-package",
            "scipy",
        ),
        ("--upgrade",),
        ("--upgrade", "--no-build-package", "numpy", "--no-build-package", "scipy"),
    ]
    assert steps == [
        "Upgrading packages without a wheel",
        "Trying a full upgrade",
        "Asking uv why",
    ]


def test_targeted_upgrade_that_fixes_everything_runs_uv_once() -> None:
    fixed = stack(scipy="1.18.1", pandas="3.0.6", numpy="2.5.3", pyyaml="6.0.3", ready=ALL)
    uv = FakeUv(targeted=fixed)
    report = analyze(stack(), PY313, uv)
    assert set(statuses(report.packages).values()) == {"update"}
    assert report.verdict == "ready after updates"
    assert report.fix_command == (
        "uv lock --upgrade-package numpy --upgrade-package pandas"
        " --upgrade-package pyyaml --upgrade-package scipy"
    )
    assert len(uv.calls) == 1


def test_full_upgrade_is_suggested_when_targeted_is_not_enough() -> None:
    fixed = stack(scipy="1.18.1", pandas="3.0.6", numpy="2.5.3", pyyaml="6.0.3", ready=ALL)
    uv = FakeUv(targeted=stack(), full=fixed)
    report = analyze(stack(), PY313, uv)
    assert set(statuses(report.packages).values()) == {"update"}
    assert {p.name: p.new_version for p in report.packages}["numpy"] == "2.5.3"
    assert report.fix_command == "uv lock --upgrade"
    assert report.verdict == "ready after updates"
    assert len(uv.calls) == 2


def test_target_outside_requires_python_cannot_run_offline() -> None:
    with pytest.raises(SetupError, match="outside requires-python"):
        analyze(stack(requires_python=">=3.10,<3.13"), PY313, FakeUv(), offline=True)


def test_target_outside_requires_python_uses_a_narrowed_baseline() -> None:
    narrowed = stack(pyyaml="6.0.3", ready=ALL, requires_python="==3.13.*")
    uv = FakeUv(plain=narrowed)
    report = analyze(stack(requires_python=">=3.10,<3.13"), PY313, uv)
    by_name = {p.name: p for p in report.packages}
    assert (by_name["pyyaml"].status, by_name["pyyaml"].version, by_name["pyyaml"].new_version) == (
        "update",
        "6.0.1",
        "6.0.3",
    )
    assert by_name["numpy"].status == "ready"
    assert report.verdict == "ready after updates"
    assert report.notes == ["requires-python is >=3.10,<3.13; widen it to include 3.13"]
    assert report.fix_command == "uv lock"
    assert uv.calls == [()]


def test_current_versions_that_do_not_resolve_fall_back_to_a_full_upgrade() -> None:
    upgraded = stack(scipy="1.18.1", ready=ALL, requires_python="==3.13.*")
    uv = FakeUv(plain=RelockError(NO_SOLUTION), full=upgraded)
    report = analyze(stack(requires_python=">=3.10,<3.13"), PY313, uv)
    assert report.resolved
    assert report.fix_command == "uv lock --upgrade"
    assert report.verdict == "ready after updates"
    assert set(statuses(report.packages).values()) == {"ready"}
    assert "No solution found" in (report.uv_explanation or "")


def test_nothing_resolves_for_the_target() -> None:
    uv = FakeUv(plain=RelockError(NO_SOLUTION), full=RelockError(NO_SOLUTION))
    report = analyze(stack(requires_python=">=3.10,<3.13"), PY313, uv)
    assert not report.resolved
    assert report.packages == []
    assert report.verdict == "not ready"
    assert "uv lock --upgrade does not resolve either" in report.notes


def test_resolver_failure_unrelated_to_readiness_keeps_the_offline_result() -> None:
    uv = FakeUv(targeted=RelockError("error: Failed to build `app`"))
    report = analyze(stack(), PY313, uv)
    assert set(statuses(report.packages).values()) == {"no-wheel"}
    assert report.notes == ["resolver pass unavailable:\nerror: Failed to build `app`"]
    assert report.verdict == "not ready"


def test_an_upgrade_that_costs_another_package_its_wheel_is_not_ready() -> None:
    before = stack(ready=frozenset({"scipy", "pandas", "pyyaml"}))
    after = stack(numpy="2.5.3", pandas="3.0.6", ready=frozenset({"scipy", "numpy", "pyyaml"}))
    report = analyze(before, PY313, FakeUv(targeted=after))
    found = {p.name: (p.status, p.new_version) for p in report.packages}
    assert found["numpy"] == ("update", "2.5.3")
    assert found["pandas"] == ("blocked", "3.0.6")
    assert {p.name: p.evidence for p in report.packages}["pandas"] is None
    assert report.verdict == "not ready"


def test_an_upgrade_that_adds_a_dependency_without_a_wheel_is_not_ready() -> None:
    before = lock(
        project(dep("numpy")),
        pkg("numpy", "1.26.4", wheels=["numpy-1.26.4-cp312-cp312-win_amd64.whl"]),
    )
    after = lock(
        project(dep("numpy")),
        pkg(
            "numpy", "2.5.3", wheels=["numpy-2.5.3-cp313-cp313-win_amd64.whl"], deps=[dep("newdep")]
        ),
        pkg("newdep", "1.0", wheels=["newdep-1.0-cp312-cp312-win_amd64.whl"]),
    )
    report = analyze(before, PY313, FakeUv(targeted=after))
    assert [(p.name, p.version, p.status, p.required_by) for p in report.packages] == [
        ("newdep", "1.0", "blocked", ["numpy"]),
        ("numpy", "1.26.4", "update", ["app"]),
    ]
    assert report.verdict == "not ready"


def test_conflict_extra_markers_do_not_prune_dependencies() -> None:
    result = classify(
        lock(
            project(dep("numba", marker="extra == 'extra-5-probe-old'")),
            pkg("numba", wheels=[PURE]),
        ),
        PY313,
    )
    assert statuses(result) == {"numba": "ready"}


def test_render_skips_a_workspace_member_with_nothing_left_to_show() -> None:
    out = render(Report("3.13", [Pkg("numpy", "1.26.4", "blocked", ["alpha", "beta"])]))
    assert out.splitlines()[-2:] == ["alpha", "└── numpy 1.26.4  ✗ no cp313 wheel  (also via beta)"]


def test_render_does_not_say_a_package_is_required_by_itself() -> None:
    out = render(Report("3.13", [Pkg("celery", "5.3", "blocked", ["app", "celery"])]))
    assert "celery 5.3  ✗ no cp313 wheel\n" in out
    assert "also via" not in out


def two_numpys(old_tag: str, new_tag: str) -> dict[str, Any]:
    """A lock where numpy is locked at two versions for the same Python, split by platform."""
    return lock(
        project(
            dep("numpy", version="1.26.4", marker="sys_platform == 'win32'"),
            dep("numpy", version="2.0.2", marker="sys_platform != 'win32'"),
        ),
        pkg("numpy", "1.26.4", wheels=[f"numpy-1.26.4-{old_tag}-{old_tag}-win_amd64.whl"]),
        pkg("numpy", "2.0.2", wheels=[f"numpy-2.0.2-{new_tag}-{new_tag}-macosx_14_0_arm64.whl"]),
    )


def test_both_versions_of_a_package_locked_twice_are_fixed() -> None:
    fixed = lock(
        project(dep("numpy")),
        pkg("numpy", "2.5.3", wheels=["numpy-2.5.3-cp313-cp313-win_amd64.whl"]),
    )
    uv = FakeUv(targeted=fixed)
    report = analyze(two_numpys("cp312", "cp312"), PY313, uv)
    assert [(p.version, p.status, p.new_version) for p in report.packages] == [
        ("1.26.4", "update", "2.5.3"),
        ("2.0.2", "update", "2.5.3"),
    ]
    assert report.verdict == "ready after updates"
    assert uv.calls == [("--upgrade-package", "numpy")]


def test_a_version_without_a_wheel_is_not_hidden_by_a_ready_one() -> None:
    unchanged = two_numpys("cp312", "cp313")
    uv = FakeUv(targeted=unchanged, full=unchanged, why=RelockError(NO_SOLUTION))
    report = analyze(two_numpys("cp312", "cp313"), PY313, uv)
    assert [(p.version, p.status) for p in report.packages] == [
        ("1.26.4", "blocked"),
        ("2.0.2", "ready"),
    ]
    assert uv.calls[0] == ("--upgrade-package", "numpy")
    out = render(report)
    assert "numpy 1.26.4  ✗ no cp313 wheel" in out
    assert "numpy 2.0.2" not in out


def test_render_lists_every_version_without_a_wheel() -> None:
    out = render(
        Report(
            "3.13",
            [
                Pkg("numpy", "1.26.4", "no-wheel", ["app"]),
                Pkg("numpy", "2.0.2", "no-wheel", ["app"]),
            ],
        )
    )
    assert "numpy 1.26.4, 2.0.2  ✗ no cp313 wheel" in out


def render(report: Report, *, verbose: bool = False) -> str:
    buffer = io.StringIO()
    render_text(report, Console(file=buffer, width=100), verbose=verbose)
    return buffer.getvalue()


MIXED = Report(
    "3.13",
    [
        Pkg("idna", "3.20", "ready", ["app"]),
        Pkg("legacy", "0.3", "source-only", ["app"]),
        Pkg("mylib", None, "unchecked", ["app"]),
        Pkg("numpy", "1.26.4", "blocked", ["pandas", "scipy"]),
        Pkg("pandas", "2.2.1", "update", ["app"], "2.3.3"),
        Pkg("scipy", "1.11.4", "blocked", ["app"]),
    ],
    fix_command="uv lock --upgrade-package pandas",
    uv_explanation="httpx[http2] and scipy<=1.11.4 have no usable wheels",
)


def test_render_puts_problems_first() -> None:
    out = render(MIXED)
    lines = out.splitlines()
    assert "Python 3.13 · not ready" in lines[0]
    assert "2 blocked · 1 to update · 1 ready" in lines[0]
    assert out.index("Blocked") < out.index("Update") < out.index("Source only")
    assert "scipy 1.11.4  ✗ no cp313 wheel" in out
    assert "numpy 1.26.4  ✗ no cp313 wheel  (also via pandas)" in out
    assert "uv lock --upgrade-package pandas" in out
    assert "2.2.1" in out and "2.3.3" in out
    assert "legacy 0.3" in out and "mylib" in out
    assert "idna" not in out


def test_render_keeps_square_brackets_literal() -> None:
    assert "httpx[http2] and scipy<=1.11.4 have no usable wheels" in render(MIXED)


def test_render_verbose_lists_ready_packages() -> None:
    assert "idna 3.20" in render(MIXED, verbose=True)


def test_render_lists_blocked_packages_flat_without_a_project() -> None:
    out = render(Report("3.13", [Pkg("numpy", "1.26.4", "no-wheel")]))
    assert "No wheel for 3.13" in out
    assert "numpy 1.26.4  ✗ no cp313 wheel" in out


def test_render_json_shape() -> None:
    data = json.loads(render_json(MIXED))
    assert data["target"] == "3.13"
    assert data["verdict"] == "not ready"
    assert data["fix_command"] == "uv lock --upgrade-package pandas"
    assert data["notes"] == []
    assert {
        "name": "pandas",
        "version": "2.2.1",
        "status": "update",
        "required_by": ["app"],
        "new_version": "2.3.3",
    } in data["packages"]
    assert {"name": "idna", "version": "3.20", "status": "ready", "required_by": ["app"]} in data[
        "packages"
    ]


def test_main_ready_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["3.12", "--project", str(make_project(tmp_path))]) == 0
    assert "Python 3.12 · ready" in capsys.readouterr().out


def test_main_offline_reports_missing_wheel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["3.13", "--project", str(make_project(tmp_path)), "--offline"]) == 1
    out = capsys.readouterr().out
    assert "not ready" in out
    assert "numpy 1.26.4  ✗ no cp313 wheel" in out
    assert "run without --offline" in out


def test_main_json_prints_only_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["3.13", "--project", str(make_project(tmp_path)), "--offline", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert {p["name"]: p["status"] for p in data["packages"]} == {
        "idna": "ready",
        "numpy": "no-wheel",
    }


def test_main_survives_a_stdout_that_cannot_encode_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["3.13", "--project", str(make_project(tmp_path)), "--offline"]) == 1
    stream.flush()
    assert b"numpy 1.26.4" in raw.getvalue()


def test_main_rejects_a_bad_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["python3", "--project", str(make_project(tmp_path))]) == 2
    assert "not a Python version" in capsys.readouterr().err


def test_main_needs_a_lockfile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["3.13", "--project", str(tmp_path)]) == 2
    assert "uv.lock" in capsys.readouterr().err


def locked_project(root: Path, pyproject: str) -> Path:
    """A real project locked as of 2024-03-01, before any package had a cp313 wheel."""
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    subprocess.run(["uv", "lock", "--exclude-newer", "2024-03-01"], cwd=root, check=True)
    return root


def run_json(project_dir: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Run the tool for 3.13 on a real project and check it left the lock alone."""
    before = (project_dir / "uv.lock").read_text(encoding="utf-8")
    code = main(["3.13", "--project", str(project_dir), "--json"])
    assert (project_dir / "uv.lock").read_text(encoding="utf-8") == before
    return code, json.loads(capsys.readouterr().out)


PYYAML_ONLY = (
    '[project]\nname = "probe"\nversion = "0"\nrequires-python = ">=3.10"\n'
    'dependencies = ["pyyaml"]\n'
)


@pytest.mark.network
def test_real_uv_finds_the_update(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(locked_project(tmp_path, PYYAML_ONLY), capsys)
    assert code == 1
    assert data["verdict"] == "ready after updates"
    assert data["fix_command"] == "uv lock --upgrade-package pyyaml"
    (pyyaml,) = data["packages"]
    assert (pyyaml["status"], pyyaml["version"]) == ("update", "6.0.1")


@pytest.mark.network
def test_real_uv_is_pinned_to_the_copy_despite_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = locked_project(tmp_path, PYYAML_ONLY)
    monkeypatch.setenv("UV_PROJECT", str(project_dir))
    monkeypatch.setenv("UV_WORKING_DIRECTORY", str(project_dir))
    monkeypatch.setenv("UV_PYTHON", "3.12")
    monkeypatch.setenv("UV_FROZEN", "1")
    code, data = run_json(project_dir, capsys)
    assert code == 1
    assert data["fix_command"] == "uv lock --upgrade-package pyyaml"


@pytest.mark.network
def test_real_uv_capped_project_with_forks_is_not_reported_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = locked_project(
        tmp_path,
        '[project]\nname = "probe"\nversion = "0"\nrequires-python = ">=3.10,<3.13"\n'
        'dependencies = ["scipy<1.12", "pandas", "pyyaml", "idna", "httpx[http2]"]\n',
    )
    assert "resolution-markers" in (project_dir / "uv.lock").read_text(encoding="utf-8")
    code, data = run_json(project_dir, capsys)
    assert code == 1
    assert data["verdict"] == "not ready"
    found = {p["name"]: p["status"] for p in data["packages"]}
    assert found["scipy"] == "blocked"
    assert found["idna"] == "ready"


def current_project(root: Path, requires_python: str, dependencies: str, cutoff: str) -> Path:
    """A real project locked as of `cutoff`, with the cutoff in pyproject.toml.

    Unlike `locked_project`, its lock is up to date, so uv has no reason of its own to
    resolve again when the tool re-locks a copy.
    """
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "probe"\nversion = "0"\nrequires-python = "{requires_python}"\n'
        f'dependencies = {dependencies}\n\n[tool.uv]\nexclude-newer = "{cutoff}"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock"], cwd=root, check=True)
    return root


@pytest.mark.network
@pytest.mark.parametrize(
    ("requires_python", "dependencies"),
    [
        (">=3.11,<3.13", '["pandas", "pyyaml", "requests"]'),
        (">=3.10,<3.13", '["scipy", "pandas", "pyyaml", "idna", "httpx[http2]"]'),
    ],
    ids=["plain", "forked"],
)
def test_real_uv_capped_project_that_is_ready_needs_no_updates(
    requires_python: str, dependencies: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = current_project(tmp_path, requires_python, dependencies, "2025-09-01T00:00:00Z")
    code, data = run_json(project_dir, capsys)
    assert data["verdict"] == "ready"
    assert code == 0
    assert {p["status"] for p in data["packages"]} == {"ready"}
    assert "numpy" in {p["name"] for p in data["packages"]}
    assert data["fix_command"] is None


@pytest.mark.network
def test_real_uv_capped_project_reports_dependencies_new_to_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = current_project(
        tmp_path, ">=3.11,<3.13", '["discord.py"]', "2025-09-01T00:00:00Z"
    )
    _, data = run_json(project_dir, capsys)
    assert "audioop-lts" in {p["name"] for p in data["packages"]}


@pytest.mark.network
def test_real_uv_handles_a_dynamically_versioned_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "probe").mkdir()
    (tmp_path / "probe" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    project_dir = locked_project(
        tmp_path,
        '[project]\nname = "probe"\ndynamic = ["version"]\nrequires-python = ">=3.10"\n'
        'dependencies = ["pyyaml"]\n\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[tool.hatch.version]\npath = "probe/__init__.py"\n',
    )
    code, data = run_json(project_dir, capsys)
    assert code == 1
    assert data["fix_command"] == "uv lock --upgrade-package pyyaml"
    assert data["notes"] == []


PYPI = "https://pypi.org/simple"


@pytest.fixture(autouse=True)
def no_pypi(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Keep every test off the network unless it is marked `network`."""
    if "network" not in request.keywords:
        monkeypatch.setattr("uv_readiness.fetch_classifiers", lambda name, version: None)


def classifiers(*minors: int) -> list[str]:
    return [
        "Programming Language :: Python :: 3",
        *(f"Programming Language :: Python :: 3.{minor}" for minor in minors),
        "Typing :: Typed",
    ]


def independent(
    name: str,
    version: str,
    status: str = "ready",
    new_version: str | None = None,
    declared: list[str] | None = None,
) -> Pkg:
    """A package from PyPI whose wheels are not tied to a Python version."""
    return Pkg(
        name,
        version,
        status,
        ["app"],
        new_version,
        evidence="version-independent",
        registry=PYPI,
        declared=declared,
    )


def test_classify_records_evidence_and_registry() -> None:
    result = classify(
        lock(
            project(dep("built"), dep("pure"), dep("stable"), dep("both"), dep("private")),
            pkg("built", wheels=["built-1.0-cp313-cp313-win_amd64.whl"]),
            pkg("pure", wheels=[PURE]),
            pkg("stable", wheels=["stable-1.0-cp39-abi3-win_amd64.whl"]),
            pkg(
                "both",
                wheels=["both-1.0-cp39-abi3-win_amd64.whl", "both-1.0-cp313-cp313-win_amd64.whl"],
            ),
            pkg("private", wheels=[PURE], source={"registry": "https://pkgs.example/simple"}),
        ),
        PY313,
    )
    assert {p.name: p.evidence for p in result} == {
        "built": "target-wheel",
        "pure": "version-independent",
        "stable": "version-independent",
        "both": "target-wheel",
        "private": "version-independent",
    }
    registries = {p.name: p.registry for p in result}
    assert registries["pure"] == PYPI
    assert registries["private"] == "https://pkgs.example/simple"


def test_packages_without_a_wheel_have_no_evidence() -> None:
    report = analyze(stack(), PY313, FakeUv(), offline=True)
    assert {p.evidence for p in report.packages} == {None}


def test_updated_packages_carry_the_evidence_of_the_new_version() -> None:
    fixed = stack(scipy="1.18.1", pandas="3.0.6", numpy="2.5.3", pyyaml="6.0.3", ready=ALL)
    report = analyze(stack(), PY313, FakeUv(targeted=fixed))
    assert {p.evidence for p in report.packages} == {"target-wheel"}


def test_check_classifiers_asks_pypi_about_version_independent_packages_only() -> None:
    report = Report(
        "3.13",
        [
            independent("good", "1.0"),
            independent("lags", "2.0"),
            independent("updated", "1.0", "update", "1.5"),
            independent("unreachable", "1.0"),
            Pkg("built", "1.0", "ready", ["app"], evidence="target-wheel", registry=PYPI),
            Pkg(
                "private",
                "1.0",
                "ready",
                ["app"],
                evidence="version-independent",
                registry="https://pkgs.example/simple",
            ),
            Pkg("blocked", "1.0", "blocked", ["app"], registry=PYPI),
        ],
    )
    answers = {
        ("good", "1.0"): classifiers(12, 13),
        ("lags", "2.0"): classifiers(12, 11),
        ("updated", "1.5"): classifiers(13),
    }
    asked: list[tuple[str, str]] = []
    ticks: list[int] = []

    def fetch(name: str, version: str) -> list[str] | None:
        asked.append((name, version))
        return answers.get((name, version))

    check_classifiers(report, fetch, lambda: ticks.append(1))

    assert sorted(asked) == [
        ("good", "1.0"),
        ("lags", "2.0"),
        ("unreachable", "1.0"),
        ("updated", "1.5"),
    ]
    assert {p.name: p.declared for p in report.packages} == {
        "good": ["3.12", "3.13"],
        "lags": ["3.11", "3.12"],
        "updated": ["3.13"],
        "unreachable": None,
        "built": None,
        "private": None,
        "blocked": None,
    }
    assert len(ticks) == 4
    assert report.classifiers_checked


def test_summary_splits_ready_packages_by_evidence() -> None:
    out = render(
        Report(
            "3.13",
            [
                Pkg("a", "1", "ready", evidence="target-wheel"),
                Pkg("b", "1", "ready", evidence="version-independent"),
                Pkg("c", "1", "ready", evidence="version-independent"),
            ],
        )
    )
    assert "3 ready (1 with a cp313 wheel, 2 version-independent)" in out
    assert "Classifiers" not in out


def test_summary_says_when_no_ready_package_is_tied_to_the_target() -> None:
    out = render(Report("3.99", [Pkg("a", "1", "ready", evidence="version-independent")]))
    assert "1 ready (all version-independent)" in out


def test_render_classifiers_lists_packages_that_do_not_declare_the_target() -> None:
    out = render(
        Report(
            "3.13",
            [
                independent("bare", "1.0", declared=[]),
                independent("good", "1.0", declared=["3.12", "3.13"]),
                independent("lags", "2.0", declared=["3.11", "3.12"]),
                independent("unreachable", "1.0"),
            ],
            classifiers_checked=True,
        )
    )
    assert "1 declare 3.13 · 2 don't · 1 unknown" in out
    assert re.search(r"lags\s+2\.0\s+up to 3\.12", out)
    assert re.search(r"bare\s+1\.0\s+no Python versions", out)
    assert not re.search(r"good\s+1\.0", out)


def test_render_json_includes_evidence_and_declared_versions() -> None:
    report = Report(
        "3.13",
        [
            Pkg(
                "good",
                "1.0",
                "ready",
                ["app"],
                evidence="version-independent",
                registry=PYPI,
                declared=["3.13"],
            )
        ],
    )
    assert json.loads(render_json(report))["packages"] == [
        {
            "name": "good",
            "version": "1.0",
            "status": "ready",
            "required_by": ["app"],
            "evidence": "version-independent",
            "registry": PYPI,
            "declared": ["3.13"],
        }
    ]


def test_main_checks_classifiers_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []

    def fetch(name: str, version: str) -> list[str]:
        asked.append((name, version))
        return classifiers(11, 12)

    monkeypatch.setattr("uv_readiness.fetch_classifiers", fetch)
    assert main(["3.12", "--project", str(make_project(tmp_path))]) == 0
    assert asked == [("idna", "3.20")]
    assert "1 declare 3.12" in capsys.readouterr().out


def test_main_offline_never_asks_pypi(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(name: str, version: str) -> list[str]:
        raise AssertionError("asked PyPI while offline")

    monkeypatch.setattr("uv_readiness.fetch_classifiers", fetch)
    assert main(["3.12", "--project", str(make_project(tmp_path)), "--offline"]) == 0
    assert "Classifiers" not in capsys.readouterr().out


def test_fetch_classifiers_reads_one_release(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def answer(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        asked.append(request.full_url)
        return io.BytesIO(json.dumps({"info": {"classifiers": classifiers(12)}}).encode())

    monkeypatch.setattr("uv_readiness.urllib.request.urlopen", answer)
    assert fetch_classifiers("idna", "3.10") == classifiers(12)
    assert asked == ["https://pypi.org/pypi/idna/3.10/json"]


@pytest.mark.parametrize("body", [b"[]", b'{"info": null}', b'{"info": {"classifiers": 5}}'])
def test_fetch_classifiers_treats_a_malformed_answer_as_unknown(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("uv_readiness.urllib.request.urlopen", lambda *_, **__: io.BytesIO(body))
    assert fetch_classifiers("idna", "3.10") is None


def test_fetch_classifiers_treats_a_broken_connection_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object, **__: object) -> io.BytesIO:
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr("uv_readiness.urllib.request.urlopen", broken)
    assert fetch_classifiers("idna", "3.10") is None


@pytest.mark.network
def test_fetch_classifiers_from_pypi() -> None:
    found = fetch_classifiers("idna", "3.10")
    assert found is not None
    assert "Programming Language :: Python :: 3.12" in found
    assert fetch_classifiers("idna", "0.0.0.0.1") is None
