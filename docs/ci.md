# Running in CI

The exit code makes `uv-readiness` usable as a check: `0` when the project is
ready, `1` when it is not, `2` when the tool could not run.

## Fail until ready

```yaml
name: Python 3.14 readiness

on:
  pull_request:
  workflow_dispatch:

jobs:
  readiness:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v9
      - run: uvx uv-readiness 3.14
```

## Watch for a blocker to clear

A weekly run tells you when the packages you are waiting on publish their
wheels. `continue-on-error` keeps the workflow green; the report is in the
step's log.

```yaml
name: Python 3.14 readiness

on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:

jobs:
  readiness:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v9
      - run: uvx uv-readiness 3.14
        continue-on-error: true
```

## What the run needs

- **`uv` on PATH.** `astral-sh/setup-uv` provides it.
- **Network access,** unless you pass `--offline`: the resolver pass talks to
  your package index, and the classifier check talks to PyPI.
- **An interpreter of the target version** for the resolver pass. uv downloads
  one when the runner does not have it.
- **Your index configuration.** The resolver pass runs `uv lock` on a copy of
  your `pyproject.toml` and `uv.toml`, so private indexes and their
  credentials work the way they do for a normal `uv lock`.

## Fast, lock-only check

`--offline` reads `uv.lock` and nothing else. It is instant and needs no
network, but it cannot tell an upgradable package from a blocked one, so both
are reported as "no wheel".

```yaml
- run: uvx uv-readiness 3.14 --offline
```

It exits with code 2 when the target is outside your `requires-python`, because
the lock has no data for that version.

## Using the JSON

```yaml
- run: uvx uv-readiness 3.14 --json > readiness.json || true
- run: jq -r '.verdict' readiness.json
```

The fields are described in [JSON output](json.md).
