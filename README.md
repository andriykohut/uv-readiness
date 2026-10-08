# uv-readiness

Check whether a uv project's locked dependencies are ready for a Python version.

```sh
uvx uv-readiness 3.13
```

It reads `uv.lock`, so every locked package is checked, direct or transitive.
A package is ready when a wheel for the target Python exists: `cp313`, a
compatible `abi3`, or a pure-Python wheel. When packages lack one, the tool
re-locks a temporary copy of the project pinned to the target Python and
reports which upgrades fix them, with the `uv lock` command to run. Your
project is never modified.

```
Python 3.13 · not ready    2 blocked · 2 to update · 4 ready

Blocked
probe
├── numpy 1.26.4  ✗ no cp313 wheel  (also via pandas, scipy)
└── scipy 1.11.4  ✗ no cp313 wheel
╭─ why (uv) ───────────────────────────────────────────────────────────────────╮
│ × No solution found when resolving dependencies for split (markers:          │
│ │ python_full_version >= '3.12' and sys_platform == 'win32'):                │
│ ╰─▶ Because only the following versions of scipy are available:              │
│         …                                                                    │
│     and scipy<=1.11.4 has no usable wheels, we can conclude that             │
│     scipy<=1.11.4 cannot be used.                                            │
│     And because your project depends on scipy<1.12, we can conclude that     │
│     your project's requirements are unsatisfiable.                           │
╰──────────────────────────────────────────────────────────────────────────────╯

Update
package    locked    resolves to
pandas     2.2.1     3.0.6
pyyaml     6.0.1     6.0.3

Resolved for Python 3.13:
  uv lock --upgrade-package pandas --upgrade-package pyyaml
```

## Usage

```
uv-readiness <python> [--project DIR] [--offline] [--json] [--verbose]
```

| Argument | Meaning |
|---|---|
| `<python>` | Target version: `3.13`, or `3.14t` for the free-threaded build. |
| `--project DIR` | Project root containing `uv.lock`. Default: current directory. |
| `--offline` | Only read `uv.lock`; do not run `uv lock`. |
| `--json` | Print the report as JSON. |
| `--verbose` | Also list ready packages. |

Exit code `0` means ready, `1` means updates are needed or packages are
blocked, `2` means the tool could not run.

## Limits

- A wheel shows that a package installs, not that it was tested. A pure-Python
  wheel counts as ready even if the code uses something the target Python
  removed.
- Platform tags are ignored: a wheel for the target Python on any platform
  counts.
- The suggested command is resolved against a copy pinned to the target Python.
  In a project that spans several Python versions uv may choose other versions
  for the older ones.
- The resolver pass needs an interpreter of the target version; uv downloads
  one if none is installed.
