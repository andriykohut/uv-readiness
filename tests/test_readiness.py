import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from uv_readiness import (
    Pkg,
    RelockError,
    SetupError,
    Target,
    classify,
    parse_target,
    relock,
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


class FakeRun:
    """Stands in for subprocess.run and records what uv would have seen."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list[list[str]] = []
        self.cwd = Path()
        self.pyproject = ""

    def __call__(
        self, command: list[str], *, cwd: Path, **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.cwd = Path(cwd)
        self.pyproject = (self.cwd / "pyproject.toml").read_text(encoding="utf-8")
        (self.cwd / "uv.lock").write_text(
            'version = 1\nrequires-python = "==3.13.*"\n', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def test_relock_runs_uv_on_a_narrowed_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = make_project(tmp_path)
    run = FakeRun()
    monkeypatch.setattr("uv_readiness.subprocess.run", run)

    result = relock(project_dir, PY313, "--upgrade")

    assert run.commands == [["uv", "lock", "--upgrade"]]
    assert 'requires-python = "==3.13.*"' in run.pyproject
    assert result["requires-python"] == "==3.13.*"
    assert run.cwd != project_dir
    assert not run.cwd.exists()
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT
    assert (project_dir / "uv.lock").read_text(encoding="utf-8") == LOCK_TOML


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
