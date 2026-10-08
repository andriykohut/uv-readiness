"""Check whether a uv project's locked dependencies are ready for a Python version."""

import re
from typing import Any, NamedTuple

from packaging.utils import InvalidWheelFilename, parse_wheel_filename

Lock = dict[str, Any]
Package = dict[str, Any]


class SetupError(Exception):
    """The tool cannot run as asked; reported as exit code 2."""


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
