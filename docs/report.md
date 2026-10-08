# Reading the report

This is the report for a small project locked in March 2024, checked for
Python 3.13:

```
Python 3.13 · not ready    2 blocked · 2 to update · 9 ready (all version-independent)

Blocked
shop
└── scipy 1.11.4  ✗ no cp313 wheel
    └── numpy 1.26.4  ✗ no cp313 wheel  (also via pandas)
╭─ why (uv) ───────────────────────────────────────────────────────────────────────────╮
│ × No solution found when resolving dependencies for split (markers:                  │
│ │ python_full_version == '3.13.*' and sys_platform == 'win32'):                      │
│ ╰─▶ Because only the following versions of scipy are available:                      │
│         …                                                                            │
│     and scipy<=1.11.4 has no usable wheels, we can conclude that                     │
│     scipy<=1.11.4 cannot be used.                                                    │
│     And because your project depends on scipy<1.12, we can conclude that             │
│     your project's requirements are unsatisfiable.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────╯

Update
package    locked    resolves to
pandas     2.2.1     3.0.6
pyyaml     6.0.1     6.0.3

Resolved for Python 3.13:
  uv lock --upgrade-package pandas --upgrade-package pyyaml

Classifiers  0 declare 3.13 · 9 don't
package               version        declares
certifi               2024.2.2       up to 3.11
charset-normalizer    3.3.2          up to 3.12
idna                  3.6            up to 3.12
python-dateutil       2.9.0.post0    up to 3.12
pytz                  2024.1         up to 3.12
requests              2.31.0         up to 3.11
six                   1.16.0         no Python versions
tzdata                2024.1         no Python versions
urllib3               2.2.1          up to 3.12
Declared by package authors, often late. This never changes the verdict.
```

## The verdict line

`Python 3.13 · not ready` is the answer. The verdict decides the exit code.

| Verdict | Meaning | Exit code |
|---|---|---|
| `ready` | No package is missing a wheel for the target. | 0 |
| `ready after updates` | It will be, once you run the command shown. | 1 |
| `not ready` | At least one package has no wheel for the target, and no fix was found or the resolver was not asked. | 1 |

The counts follow. The ready count is split by evidence, for example
`9 ready (2 with a cp313 wheel, 7 version-independent)`. A project where every
ready package is version-independent, as above, has no package that was built
for the target, so "ready" means "nothing stops installation".

## Blocked

A tree from your project down to each package without a wheel. Only the paths
that lead to such a package are drawn.

- `✗ no cp313 wheel` marks the packages that are in the way.
- `(also via pandas)` means another package requires it too.
- A package locked at two versions for the target shows both:
  `numpy 1.26.4, 2.0.2`.

The heading reads **No wheel for 3.13** when the tool did not ask the resolver,
which happens with `--offline` or when the resolver pass was unavailable. Those
packages may still be fixable.

## Why (uv)

uv's own derivation of why the blocked packages cannot get a wheel, with long
version lists shortened to `…`. Read it from the bottom: the last sentence
usually names the requirement to change, here `scipy<1.12` in your project.

## Update

Packages that get a wheel for the target once upgraded, with the version uv
resolved. The command below the table is the one to run in your project. It was
resolved for the target Python on a copy of the project.

For a target outside your `requires-python`, the command can be a plain
`uv lock`: widening the range is the change, and uv does the rest.

## Classifiers

For version-independent packages, how many declare the target in their trove
classifiers on PyPI, and a table of the ones that do not.

- `up to 3.12` is the newest version the release declares.
- `no Python versions` means the release lists no minor versions at all.
- `unknown` in the tally counts packages whose lookup failed.

This section never changes the verdict. Many packages work on a new Python long
before their authors add the classifier.

## Other sections

- **Source only:** the package has no wheels at all, so wheels cannot say
  anything. It is usually pure Python built from the sdist.
- **Not checked:** git, path and other local sources.
- **Ready:** listed in full with `--verbose`.

## Notes

Lines under the verdict explain anything unusual about the run.

| Note | Meaning |
|---|---|
| `requires-python is …; widen it to include 3.13` | The target is outside your project's range. Widen it before running the command. |
| `run without --offline to check for fixes` | Packages lack a wheel and the resolver was not asked. |
| `resolver pass unavailable: …` | uv could not lock the copy for a reason unrelated to readiness. The offline result stands. |
| `uv lock --upgrade does not resolve either` | No set of versions resolves for the target at all. |
