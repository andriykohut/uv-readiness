"""Check whether a uv project's locked dependencies are ready for a Python version."""

import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import SpecifierSet
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


Relocker = Callable[..., Lock]
Step = Callable[[str], None]


@dataclass
class Report:
    target: str
    packages: list[Pkg] = field(default_factory=list)
    fix_command: str | None = None
    uv_explanation: str | None = None
    notes: list[str] = field(default_factory=list)
    resolved: bool = True

    @property
    def verdict(self) -> str:
        statuses = {p.status for p in self.packages}
        if not self.resolved or statuses & {"blocked", "no-wheel"}:
            return "not ready"
        if self.fix_command or "update" in statuses:
            return "ready after updates"
        return "ready"


def in_range(lock: Lock, target: Target) -> bool:
    spec = lock.get("requires-python")
    if not spec:
        return True
    return any(SpecifierSet(spec).contains(f"3.{target.minor}.{patch}") for patch in (0, 99))


def _unsolvable(error: RelockError) -> bool:
    return "No solution found" in str(error)


def _tail(error: RelockError) -> str:
    return "\n".join(str(error).strip().splitlines()[-5:])


def _flags(flag: str, names: list[str]) -> list[str]:
    return [part for name in names for part in (flag, name)]


def _fix(report: Report, target: Target, original: Lock, relocker: Relocker, step: Step) -> None:
    """Re-lock for the target and turn each `no-wheel` into `update` or `blocked`."""

    def attempt(*args: str) -> dict[str, Pkg]:
        # ponytail: keyed by name; a package locked at two versions for one Python keeps the last
        return {p.name: p for p in classify(relocker(*args), target, original)}

    def stuck(result: dict[str, Pkg], names: list[str]) -> list[str]:
        return [n for n in names if n in result and result[n].status == "no-wheel"]

    if not report.resolved:  # the current versions do not resolve for the target at all
        step("Trying a full upgrade")
        try:
            upgraded = attempt("--upgrade")
        except RelockError as error:
            if not _unsolvable(error):
                raise
            report.notes.append("uv lock --upgrade does not resolve either")
            return
        report.resolved = True
        report.fix_command = "uv lock --upgrade"
        report.packages = sorted(upgraded.values(), key=lambda p: p.name)
        for package in report.packages:
            if package.status == "no-wheel":
                package.status = "blocked"
        return

    current = {p.name: p for p in report.packages}
    blockers = sorted(n for n, p in current.items() if p.status == "no-wheel")
    step("Upgrading packages without a wheel")
    result = attempt(*_flags("--upgrade-package", blockers))
    still = stuck(result, blockers)
    fixed = [n for n in blockers if n not in still]
    command = "uv lock " + " ".join(_flags("--upgrade-package", fixed)) if fixed else None
    pulled = [
        n
        for n, p in result.items()
        if n in current
        and n not in blockers
        and current[n].status == "ready"
        and p.version != current[n].version
    ]
    if still:
        step("Trying a full upgrade")
        full = attempt("--upgrade")
        if len(stuck(full, still)) < len(still):
            result, command, pulled = full, "uv lock --upgrade", []
            still = stuck(full, still)

    for name in blockers:
        if name in still:
            current[name].status = "blocked"
        else:
            current[name].status = "update"
            current[name].new_version = result[name].version if name in result else None
    for name in pulled:
        current[name].status = "update"
        current[name].new_version = result[name].version
    report.fix_command = command

    if still:
        step("Asking uv why")
        try:
            relocker("--upgrade", *_flags("--no-build-package", still))
        except RelockError as error:
            if _unsolvable(error):
                report.uv_explanation = trim(str(error))


def _mark_forced(packages: list[Pkg], original: Lock) -> None:
    """Show versions the narrowed re-lock had to change as updates from the locked version."""
    locked: defaultdict[str, list[str]] = defaultdict(list)
    for package in original.get("package", []):
        if "version" in package:
            locked[package["name"]].append(package["version"])
    for p in packages:
        versions = locked.get(p.name)
        if not versions or p.version in versions:
            continue
        p.new_version = p.new_version or p.version
        p.version = versions[0]
        if p.status == "ready":
            p.status = "update"


def analyze(
    original: Lock,
    target: Target,
    relocker: Relocker,
    *,
    offline: bool = False,
    step: Step = lambda _: None,
) -> Report:
    report = Report(str(target))
    baseline: Lock | None = original
    if not in_range(original, target):
        spec = original["requires-python"]
        if offline:
            raise SetupError(
                f"Python {target} is outside requires-python ({spec}); "
                "answering that needs the resolver, so --offline cannot be used"
            )
        report.notes.append(f"requires-python is {spec}; widen it to include 3.{target.minor}")
        step(f"Resolving current versions for Python {target}")
        try:
            baseline = relocker()
        except RelockError as error:
            if not _unsolvable(error):
                raise SetupError(
                    f"could not resolve a copy of the project:\n{_tail(error)}"
                ) from None
            baseline, report.resolved, report.uv_explanation = None, False, trim(str(error))
    if baseline is not None:
        report.packages = classify(baseline, target, original)

    waiting = baseline is None or any(p.status == "no-wheel" for p in report.packages)
    if waiting and offline:
        report.notes.append("run without --offline to check for fixes")
    elif waiting:
        try:
            _fix(report, target, original, relocker, step)
        except RelockError as error:
            report.notes.append(f"resolver pass unavailable:\n{_tail(error)}")
    if baseline is not None and baseline is not original:
        _mark_forced(report.packages, original)
    return report
