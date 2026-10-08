"""Check whether a uv project's locked dependencies are ready for a Python version."""

import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

from packaging.markers import InvalidMarker, Marker
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

Lock = dict[str, Any]
Package = dict[str, Any]


class SetupError(Exception):
    """The tool cannot run as asked; reported as exit code 2."""


class RelockError(Exception):
    """`uv lock` failed on the temporary copy; str() is uv's stderr or a short reason."""


class Target(NamedTuple):
    minor: int
    free_threaded: bool

    def __str__(self) -> str:
        return f"3.{self.minor}{'t' if self.free_threaded else ''}"

    @property
    def tag(self) -> str:
        return f"cp3{self.minor}{'t' if self.free_threaded else ''}"


def parse_target(text: str) -> Target:
    match = re.fullmatch(r"3\.(\d+)(?:\.\d+)?(t?)", text)
    if not match:
        raise SetupError(f"not a Python version: {text!r} (expected something like 3.13 or 3.14t)")
    return Target(int(match[1]), bool(match[2]))


def _tag_supports(interpreter: str, abi: str, target: Target) -> bool:
    match = re.fullmatch(r"(py|cp)3(\d*)", interpreter)
    if not match:
        return False
    minor = int(match[2]) if match[2] else None
    if match[1] == "py":
        return abi == "none" and (minor is None or minor <= target.minor)
    if minor is None:
        return False
    if abi == "abi3":
        # ponytail: abi3t (free-threaded stable ABI) is not recognised; add it when wheels use it
        return minor <= target.minor and not target.free_threaded
    if minor != target.minor:
        return False
    return abi in ("none", target.tag)


def wheel_supports(filename: str, target: Target) -> bool:
    """Whether a wheel installs on the target Python. Platform tags are ignored."""
    try:
        tags = parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return False
    return any(_tag_supports(tag.interpreter, tag.abi, target) for tag in tags)


# ponytail: four mainstream platforms; a dependency gated on any other platform counts as unreachable
ENVIRONMENTS: list[dict[str, str]] = [
    {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
    },
    {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "aarch64",
    },
    {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "os_name": "posix",
        "platform_machine": "arm64",
    },
    {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "platform_machine": "AMD64",
    },
]

Key = tuple[str, str | None]


@dataclass
class Pkg:
    name: str
    version: str | None
    status: str
    required_by: list[str] = field(default_factory=list)
    new_version: str | None = None


def _roots(lock: Lock) -> list[Package]:
    """The project and its workspace members."""
    packages: list[Package] = lock.get("package", [])
    members = set(lock.get("manifest", {}).get("members", []))
    if members:
        return [p for p in packages if p["name"] in members]
    return [
        p
        for p in packages
        if "." in (p.get("source", {}).get("editable"), p.get("source", {}).get("virtual"))
    ]


def _live(marker: str | None, target: Target) -> bool:
    """Whether a dependency edge can apply on the target Python on any mainstream platform."""
    if not marker:
        return True
    python = {
        "python_version": f"3.{target.minor}",
        "python_full_version": f"3.{target.minor}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }
    try:
        parsed = Marker(marker)
    except InvalidMarker:
        return True
    return any(parsed.evaluate({**environment, **python}) for environment in ENVIRONMENTS)


def reachable(lock: Lock, target: Target) -> dict[Key, tuple[Package, set[str]]]:
    """Map (name, version) to (package, names requiring it) for what installs on the target."""
    packages: list[Package] = lock.get("package", [])
    roots = _roots(lock)
    if not roots:
        return {(p["name"], p.get("version")): (p, set()) for p in packages}
    root_names = {p["name"] for p in roots}
    by_name: defaultdict[str, list[Package]] = defaultdict(list)
    for package in packages:
        by_name[package["name"]].append(package)

    found: dict[Key, tuple[Package, set[str]]] = {}
    seen: set[tuple[Key, frozenset[str]]] = set()
    # extras is None for a project root: follow every extra and every dependency group
    todo: list[tuple[Package, frozenset[str] | None]] = [(root, None) for root in roots]
    while todo:
        package, extras = todo.pop()
        optional: dict[str, list[dict[str, Any]]] = package.get("optional-dependencies", {})
        edges: list[dict[str, Any]] = list(package.get("dependencies", []))
        if extras is None:
            groups = [*optional.values(), *package.get("dev-dependencies", {}).values()]
        else:
            groups = [optional.get(extra, []) for extra in extras]
        for group in groups:
            edges += group
        for edge in edges:
            if not _live(edge.get("marker"), target):
                continue
            for child in by_name[edge["name"]]:
                if child["name"] in root_names:
                    continue
                if "version" in edge and child.get("version") != edge["version"]:
                    continue
                key = (child["name"], child.get("version"))
                found.setdefault(key, (child, set()))[1].add(package["name"])
                wanted = frozenset(edge.get("extra", []))
                if (key, wanted) not in seen:
                    seen.add((key, wanted))
                    todo.append((child, wanted))
    return found


def _wheel_names(package: Package) -> list[str]:
    refs = [
        w.get("filename") or w.get("url") or w.get("path") or "" for w in package.get("wheels", [])
    ]
    refs.append(package.get("source", {}).get("url", ""))
    names = [unquote(urlsplit(ref).path).rsplit("/", 1)[-1] for ref in refs]
    return [name for name in names if name.endswith(".whl")]


def classify(lock: Lock, target: Target, original: Lock | None = None) -> list[Pkg]:
    """Status of every reachable package.

    Pass `original` when `lock` is a re-lock narrowed to the target: its wheel lists are
    already filtered, so the original tells a missing wheel from a source-only package.
    """
    had_wheels = {p["name"] for p in (original or {}).get("package", []) if p.get("wheels")}
    result: list[Pkg] = []
    for (name, version), (package, parents) in reachable(lock, target).items():
        names = _wheel_names(package)
        if not {"registry", "url"} & package.get("source", {}).keys():
            status = "unchecked"
        elif any(wheel_supports(wheel, target) for wheel in names):
            status = "ready"
        elif names or name in had_wheels:
            status = "no-wheel"
        else:
            status = "source-only"
        result.append(Pkg(name, version, status, sorted(parents)))
    return sorted(result, key=lambda p: (p.name, p.version or ""))


# ponytail: regex edit of requires-python; a project that sets it another way loses the resolver pass
REQUIRES_PYTHON = re.compile(r"""^(\s*requires-python\s*=\s*)(["']).*?\2""", re.MULTILINE)
VERSION_LINE = re.compile(r"\s+[\w.\-]+\s?(==|>=|<=|>|<)\s?[\w.*+!]+")


def _load(path: Path) -> Lock:
    with path.open("rb") as file:
        return tomllib.load(file)


def _member_dirs(lock: Lock) -> set[Path]:
    dirs = {Path(".")}
    for root in _roots(lock):
        source = root.get("source", {})
        if path := source.get("editable") or source.get("virtual"):
            dirs.add(Path(path))
    return dirs


def relock(project: Path, target: Target, *args: str) -> Lock:
    """Run `uv lock *args` on a copy of the project pinned to the target Python."""
    with tempfile.TemporaryDirectory(prefix="uv-readiness-") as tmp:
        copy = Path(tmp)
        narrowed = 0
        for member in _member_dirs(_load(project / "uv.lock")):
            if member.is_absolute() or ".." in member.parts:
                raise RelockError(f"workspace member {member} is outside the project")
            source = project / member / "pyproject.toml"
            if not source.is_file():
                continue
            text, count = REQUIRES_PYTHON.subn(
                rf'\g<1>"==3.{target.minor}.*"', source.read_text(encoding="utf-8"), count=1
            )
            narrowed += count
            (copy / member).mkdir(parents=True, exist_ok=True)
            (copy / member / "pyproject.toml").write_text(text, encoding="utf-8")
        if not narrowed:
            raise RelockError(f"no requires-python line found in {project / 'pyproject.toml'}")
        for name in ("uv.lock", "uv.toml"):
            if (project / name).is_file():
                shutil.copy(project / name, copy / name)
        try:
            done = subprocess.run(
                ["uv", "lock", *args], cwd=copy, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise SetupError("uv was not found on PATH") from None
        if done.returncode:
            raise RelockError(done.stderr)
        return _load(copy / "uv.lock")


def trim(stderr: str) -> str:
    """Reduce uv's resolver failure output to the derivation."""
    lines = stderr.strip().splitlines()
    start = next((i for i, line in enumerate(lines) if "No solution found" in line), 0)
    kept: list[str] = []
    for line in lines[start:]:
        if line.strip().startswith("hint:"):
            break
        if not VERSION_LINE.fullmatch(line):
            kept.append(line)
        elif not (kept and kept[-1].strip() == "…"):
            kept.append("          …")
    return textwrap.dedent("\n".join(kept)).strip()
