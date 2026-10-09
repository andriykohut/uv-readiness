# JSON output

`--json` prints one JSON object on stdout and nothing else. The progress bar is
off, and the exit code is the same as for the text report.

```sh
uvx uv-readiness 3.13 --json
```

An excerpt, with four of the packages and the explanation shortened:

```json
{
  "target": "3.13",
  "verdict": "not ready",
  "packages": [
    {"name": "idna", "version": "3.6", "status": "ready", "required_by": ["requests"], "evidence": "version-independent", "registry": "https://pypi.org/simple", "declared": ["3.5", "3.6", "3.7", "3.8", "3.9", "3.10", "3.11", "3.12"]},
    {"name": "numpy", "version": "1.26.4", "status": "blocked", "required_by": ["pandas", "scipy"], "registry": "https://pypi.org/simple"},
    {"name": "pandas", "version": "2.2.1", "status": "update", "required_by": ["shop"], "new_version": "3.0.6", "evidence": "target-wheel", "registry": "https://pypi.org/simple"},
    {"name": "scipy", "version": "1.11.4", "status": "blocked", "required_by": ["shop"], "registry": "https://pypi.org/simple"}
  ],
  "fix_command": "uv lock --upgrade-package pandas --upgrade-package pyyaml",
  "uv_explanation": "× No solution found when resolving dependencies … your project's requirements are unsatisfiable.",
  "notes": []
}
```

## Top level

| Field | Type | Meaning |
|---|---|---|
| `target` | string | The target as given, for example `"3.13"` or `"3.14t"`. |
| `verdict` | string | `"ready"`, `"ready after updates"` or `"not ready"`. |
| `packages` | array | One object per reachable package, sorted by name. |
| `fix_command` | string or null | The `uv lock` command to run, when one was found. |
| `uv_explanation` | string or null | uv's derivation for the blocked packages. |
| `notes` | array of strings | The notes shown under the verdict in the text report. |

## Package

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Package name as locked. |
| `version` | string | The locked version. Absent for a local package without one. |
| `status` | string | See below. |
| `required_by` | array of strings | Packages that require it directly. Your project appears by its own name. |
| `new_version` | string | For `update`: the version uv resolved. Absent otherwise. |
| `evidence` | string | For ready and updated packages: `"target-wheel"` or `"version-independent"`. |
| `registry` | string | The index the package is locked from. Absent for other sources. |
| `declared` | array of strings | Python versions the release declares on PyPI. Absent when not looked up. |

Fields without a value are left out, not set to null.

## Status

| Status | Meaning |
|---|---|
| `ready` | A wheel installs on the target. |
| `update` | An upgrade gives it a wheel; see `new_version`. |
| `blocked` | No wheel, and no upgrade fixes it. |
| `no-wheel` | No wheel, and the resolver was not asked or was unavailable. |
| `source-only` | An sdist and no wheels at all. |
| `unchecked` | Git, path or other local source. |

## Examples

List what is blocked:

```sh
uvx uv-readiness 3.14 --json | jq -r '.packages[] | select(.status == "blocked") | .name'
```

List version-independent packages that do not declare the target:

```sh
uvx uv-readiness 3.14 --json |
  jq -r '(.target | rtrimstr("t")) as $t | .packages[] | select(.declared and (.declared | index($t) | not)) | .name'
```
