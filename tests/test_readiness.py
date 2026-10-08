import io
import json
import subprocess
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
    classify,
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


def test_main_rejects_a_bad_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["python3", "--project", str(make_project(tmp_path))]) == 2
    assert "not a Python version" in capsys.readouterr().err


def test_main_needs_a_lockfile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["3.13", "--project", str(tmp_path)]) == 2
    assert "uv.lock" in capsys.readouterr().err


@pytest.mark.network
def test_real_uv_finds_the_update(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["pyyaml"]\n',
        encoding="utf-8",
    )
    # pyyaml 6.0.1 was the newest release on this date and has no cp313 wheel
    subprocess.run(["uv", "lock", "--exclude-newer", "2024-03-01"], cwd=tmp_path, check=True)
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")

    assert main(["3.13", "--project", str(tmp_path), "--json"]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "ready after updates"
    assert data["fix_command"] == "uv lock --upgrade-package pyyaml"
    (pyyaml,) = data["packages"]
    assert (pyyaml["status"], pyyaml["version"]) == ("update", "6.0.1")
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == before
