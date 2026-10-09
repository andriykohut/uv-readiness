# uv-readiness

Find out whether a uv project can move to a new Python version, and what is in
the way.

![uv-readiness checking a project for Python 3.13: two packages are blocked, two need an update, and uv explains why](https://raw.githubusercontent.com/andriykohut/uv-readiness/main/docs/demo.gif)

```sh
uvx uv-readiness 3.14
```

Run it in a directory that has a `uv.lock`. It checks every locked package,
direct or transitive, and never modifies your project.

## What it tells you

- **What blocks the upgrade.** Packages that ship compiled wheels but none for
  the target Python, drawn as a tree from your project down, so you can see
  which dependency pulls each one in.
- **What fixes it.** It re-locks a temporary copy of the project for the target
  Python and prints the `uv lock` command that works. When no upgrade helps, it
  shows uv's own explanation of which requirement holds things back.
- **What "ready" rests on.** Most packages ship `py3-none` wheels that install
  on any Python 3, which says nothing about one particular version. The summary
  counts those apart from packages with a wheel built for the target, and a
  classifier check lists the ones that do not declare the target yet.

## Two signals, used differently

The tool reads both wheels and classifiers. Which one applies depends on the
package.

| | Wheels | Classifiers |
|---|---|---|
| What it is | A published file tagged for a Python version. | A line the author adds to the package metadata. |
| Which packages | Compiled ones: few in most projects, but the ones that hold up an upgrade. | Everything else: `py3-none` packages, usually the large majority. |
| How strong | A fact. A `cp314` binary exists or it does not. | A hint. Often added months late, or never. |
| Effect | Decides the verdict and the exit code. | Shown in the report. Never changes the verdict. |

So for most of your packages the classifier is the only version-specific
information there is, and the report shows it. It does not fail a run, because a
missing classifier usually means the author has not updated the metadata, not
that the package is broken.

This is the uv counterpart to
[pdm-readiness](https://github.com/andriykohut/pdm-readiness), which reads
classifiers for direct dependencies only.

## Usage

```
uv-readiness <python> [--project DIR] [--offline] [--json] [--verbose]
```

| Argument | Meaning |
|---|---|
| `<python>` | Target version: `3.14`, or `3.14t` for the free-threaded build. |
| `--project DIR` | Project root containing `uv.lock`. Default: current directory. |
| `--offline` | Only read `uv.lock`: no `uv lock` runs, no PyPI lookups. |
| `--json` | Print the report as JSON. |
| `--verbose` | Also list ready packages. |

| Exit code | Meaning |
|---|---|
| `0` | Ready: no package is missing a wheel for the target. |
| `1` | Updates are needed, or packages are blocked. |
| `2` | The tool could not run: no `uv.lock`, a bad version, `uv` missing. |

uv has no plugin system, so this is a standalone command, not `uv readiness`.
It needs `uv` on your PATH whenever it has to ask the resolver.

## In CI

```yaml
- uses: actions/checkout@v7
- uses: astral-sh/setup-uv@v10.2.0
- run: uvx uv-readiness 3.14
```

The step fails until the project is ready. See [Running in CI](https://github.com/andriykohut/uv-readiness/blob/main/docs/ci.md) for
a scheduled workflow and for using the JSON output.

## Documentation

- [How it works](https://github.com/andriykohut/uv-readiness/blob/main/docs/how-it-works.md): what is read, what is run, and where.
- [Reading the report](https://github.com/andriykohut/uv-readiness/blob/main/docs/report.md): every section, status and note.
- [JSON output](https://github.com/andriykohut/uv-readiness/blob/main/docs/json.md): the fields behind `--json`.
- [Running in CI](https://github.com/andriykohut/uv-readiness/blob/main/docs/ci.md): workflows and exit codes.
- [Limits](https://github.com/andriykohut/uv-readiness/blob/main/docs/limits.md): what the tool cannot tell you.

## Limits worth knowing first

- A wheel shows that a package installs, not that it was tested. A `py3-none`
  wheel counts as ready even if the code uses something the target Python
  removed.
- Platform tags are ignored: a wheel for the target Python on any platform
  counts.
- The suggested command is resolved for the target Python only. In a project
  that spans several Python versions, uv may pick other versions for the older
  ones.

The full list is in [Limits](https://github.com/andriykohut/uv-readiness/blob/main/docs/limits.md).

## Development

```sh
uv sync
uv run pytest                 # unit tests
uv run pytest -m network      # runs real `uv lock` and queries PyPI
uv run ruff check && uv run ruff format --check && uv run ty check
vhs docs/demo.tape            # re-record the demo GIF
```

## License

MIT. See [LICENSE](https://github.com/andriykohut/uv-readiness/blob/main/LICENSE).
