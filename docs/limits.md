# Limits

What `uv-readiness` cannot tell you, and where its answers are approximate.

## About the verdict

- **Installable is not tested.** A wheel shows that a package installs on the
  target Python. It does not show that the code works there. A `py3-none` wheel
  counts as ready even if the package imports something the target Python
  removed.
- **Classifiers are a weak hint.** They are declared by package authors and
  often added late or never. The tool shows them and never acts on them.
- **Platform tags are ignored.** A wheel for the target Python on any platform
  counts. A package that has a Linux wheel and no Windows wheel is reported
  ready.
- **`abi3t` wheels are not recognised.** For a free-threaded target only
  `cp3Mt`, `cp3M-none` and `py3-none` wheels count.
- **Source-only packages are not judged.** A package with an sdist and no
  wheels is listed separately and does not affect the verdict.
- **Git, path and editable sources are not checked.**

## About the dependency graph

- **Markers are evaluated for four platforms:** Linux x86_64, Linux aarch64,
  macOS arm64 and Windows x86_64. A dependency that only applies elsewhere is
  treated as not installed.
- **A lock with no project entry** is reported in full: every package counts as
  reachable, and markers are not applied.
- **A package locked at several versions for the target** is treated by name in
  the fix pass. All of its versions without a wheel become `update` or
  `blocked` together.

## About the suggested command

- **It is resolved for the target Python only.** The copy of your project is
  pinned to the target. In a project that spans several Python versions, uv may
  choose different versions for the older ones when you run the command.
- **It takes the newest versions uv picks.** It is not the smallest possible
  upgrade.

## About the resolver pass

- **It needs an interpreter of the target version.** uv downloads one if none
  is installed.
- **It cannot run with `--offline`,** so a target outside your
  `requires-python` is an error in that mode.
- **Some projects cannot be locked from a copy.** The copy has your
  `pyproject.toml` files, `uv.lock` and `uv.toml`, not your sources. The report
  says "resolver pass unavailable" and keeps the lock-only result when:
  - a path dependency is not a workspace member,
  - metadata that uv needs to resolve, such as `dependencies`, is dynamic,
  - `pyproject.toml` has no `requires-python` line.

## About PyPI lookups

- **Only PyPI is asked.** Packages locked from another index are not looked up,
  and their names are never sent to PyPI.
- **Nothing is cached between runs.** Each run without `--offline` repeats the
  lookups.
