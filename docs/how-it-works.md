# How it works

`uv-readiness` reads your `uv.lock`, decides for each package whether it can be
installed on the target Python, and asks uv's resolver how to fix the ones that
cannot. It works in five steps.

```
uv.lock → baseline → reachable packages → classify → fix pass → classifiers
```

## 1. Baseline

`uv.lock` only records wheels that fit the project's `requires-python`.

- **Target inside `requires-python`:** the lock is used as it is. Nothing is
  run and nothing is fetched for this step.
- **Target outside it**, for example a project capped at `<3.13` checked for
  3.13: the lock has no data for that version, so the tool re-locks a temporary
  copy pinned to the target and uses the result. The report tells you to widen
  `requires-python`.

## 2. Reachable packages

The lock covers every Python version and platform the project supports. The
tool walks the dependency graph from your project (and every workspace member)
and keeps only what can be installed on the target Python.

- It follows dependencies, every extra and every dependency group of the
  project.
- An edge with a marker is followed when the marker can be true on the target
  Python on Linux (x86_64 or aarch64), macOS (arm64) or Windows (x86_64).
- A package such as `tomli ; python_version < "3.11"` is dropped when you check
  3.13, so it cannot show up as a false blocker.

The walk also records which packages require each package. The report uses that
to draw the tree.

## 3. Classify

Each reachable package gets a status from the wheel filenames in the lock.

| Status | Meaning |
|---|---|
| `ready` | A wheel installs on the target. |
| `no-wheel` | The package ships wheels, but none for the target. |
| `source-only` | The package has an sdist and no wheels at all. |
| `unchecked` | Git, path, editable or other local source. |

A wheel counts for target 3.M when its tags are one of these. Platform tags are
ignored.

| Tags | Standard build | Free-threaded (`3.Mt`) |
|---|---|---|
| `py3-none`, or `py3N-none` with N ≤ M | yes | yes |
| `cp3M-cp3M` | yes | no |
| `cp3M-cp3Mt` | no | yes |
| `cp3N-abi3` with N ≤ M | yes | no |
| `cp3M-none` | yes | yes |

Every ready package also records its **evidence**:

- `target-wheel`: a wheel was built for this Python (`cp3M`).
- `version-independent`: `py3-none` or `abi3`. It installs, but the wheel says
  nothing about this particular version.

## 4. Fix pass

When packages have status `no-wheel`, the tool asks uv what can be done. It
runs `uv lock` up to three times on the temporary copy:

1. `uv lock --upgrade-package <each package without a wheel>`. Packages that
   now have a wheel become `update`, with the version uv chose.
2. `uv lock --upgrade`, only if some are still stuck. This catches a package
   held back by another package's old locked version.
3. `uv lock --upgrade --no-build-package <still stuck>`, only to capture uv's
   explanation. Whatever is still stuck is `blocked`.

The upgraded lock is classified again in full. A package that the upgrade
would leave without a wheel, including a dependency that is new with the
upgrade, is reported as `blocked`, so the verdict cannot be "ready after
updates" in that case.

The report prints the command that fixed the most: the `--upgrade-package`
form when that was enough, `uv lock --upgrade` otherwise.

### The temporary copy

The copy holds `pyproject.toml` for the project and each workspace member,
`uv.lock`, and `uv.toml` if you have one. Before uv runs:

- `requires-python` is set to `==3.M.*` in each `pyproject.toml`. The copied
  lock keeps its own range, so uv sees it is out of date and resolves again
  with your locked versions as preferences. The lock's top-level fork markers
  are removed, because they describe the old range.
- A project with `dynamic = ["version"]` gets a fixed version, so uv does not
  need your sources to build it.
- uv is called with `--project`, `--directory` and `--python` pointing at the
  copy and the target, so `UV_PROJECT`, `UV_WORKING_DIRECTORY` or `UV_PYTHON`
  in your environment cannot redirect it. `UV_FROZEN` and `UV_LOCKED` are not
  passed on, because they stop uv from resolving.

The copy is deleted afterwards. Your project's files are never written.

## 5. Classifiers

For ready and updated packages whose evidence is `version-independent`, the
tool asks PyPI
which Python versions the release declares
(`https://pypi.org/pypi/<name>/<version>/json`).

- At most 16 requests run at once, each with a 10-second timeout and no retry.
- Only packages locked from `https://pypi.org/simple` are looked up. Packages
  from other indexes are never sent to PyPI.
- The result is a hint. It never changes a status, the verdict or the exit
  code.

`--offline` skips steps 4 and 5.
