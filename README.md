# github-config-drift

[![CI](https://github.com/andornaut/github-config-drift/actions/workflows/release.yml/badge.svg)](https://github.com/andornaut/github-config-drift/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

The canonical configuration every repository here is meant to carry, and the sweep
that reports where a copy has drifted from it.

Every repository carries its own copy, because none of these tools can inherit from
somewhere else: `golangci-lint` has no include mechanism at all, and `ruff`'s
`extend` takes a local path only. Copies drift, so this reports the drift rather
than pretending it cannot happen.

## What it covers

`check-drift.py` lists every repository owned by `andornaut` that is neither
archived nor a fork, reads each one's files through `gh api`, and compares them
with [`configs/`](./configs). It clones nothing.

The listing is the public endpoint rather than the authenticated one: a workflow's
`GITHUB_TOKEN` is not a user, so `user/repos` returns nothing for it. A repository
created tomorrow is swept the following Monday with no edit here.

## Reconciling

A report is not a verdict. Drift runs both ways: a repository can fall behind, and
it can also be ahead, having reached a stricter position on its own. Read the diff
before deciding which side moves.

Where the copies are meant to differ, they differ in named places:

| Language | Per-repository, by design |
| --- | --- |
| Go | the `gci` import prefix, the `gosec` suppression list, exclusion rules a repository states are local |
| Python | `per-file-ignores`, `extend-include`, `extend-exclude`, and `target-version` where a repository states a floor |
| JavaScript | the eslint entries in `.lintstagedrc`, which name the script types a repository holds; `eslint.config.base.mjs` is byte-identical everywhere, and each repository's own `eslint.config.mjs` applies it to its paths |
| Shell | `scandir` and `ignore_paths`, which name a repository's own vendored trees |
| Markdown | `ignores`, where a repository adds its own test data to the shared entries. Additions only: an entry canon names and a copy does not is reported |

The sweep skips those and compares the rest.

## Absence is reported, not skipped

A gate nobody added reads exactly like one that passed, so presence is checked
separately from content.

Language presence comes from `repos/{r}/languages` rather than from a file glob:
most of the shell scripts here carry no extension, and linguist reads the shebang.
A repository GitHub reports as holding Shell is expected to carry the ShellCheck
step at all. `SHELL_EXEMPT` in `check-drift.py` names the repositories that lint
shell some other way, each with the reason. Go and Python are checked the same way,
from the same languages endpoint: the config is skipped where the language is
absent and reported where the language is present and the config is not.

Markdown has no exemption list. Every repository here carries at least a
`README.md`, so both `.markdownlint-cli2.yaml` and the step are expected everywhere.

A query that failed is neither presence nor absence, and the two are easy to
confuse: `gh` exits non-zero for a file a repository does not carry and for a call
that never reached an answer. Read the second as absence and a rate limited run
goes quiet exactly where it should speak, since the language lookup that decides
whether a config is expected would report the language missing. Only a 404 counts
as an answer here. Anything else is retried, and a call that still fails ends the
run with exit 2 rather than letting it report on the repositories it did reach.

A repository with a `package.json` is expected to carry a `.lintstagedrc`, and its
prettier entry to be `*` with `--ignore-unknown`. Only that entry is compared. The
hook and CI have to cover one set of files: a hand-written list of types is how the
hook came to check less than `prettier --check .` does, missing the
`.markdownlint-cli2.yaml` every repository carries. The same repository is expected
to carry `.husky/pre-commit`, compared byte for byte: it is one line,
`npx lint-staged`, and every difference is a difference in what the hook runs.

`.github/workflows/ai-attributions.yml` is compared byte for byte in every
repository, rather than by step as the ShellCheck and markdownlint gates are. It is
the attribution gate's own configuration, so a copy that quietly lost
`agents-files`, `emdashes` or `fetch-depth` still runs, still reports success, and
checks less than the others do.

## Related

[ai-attributions](https://github.com/andornaut/ai-attributions) is the other
estate-wide checker, arranged the opposite way: it is a published GitHub Action
that each repository runs on itself, over its own git history. This is one
workflow, in one repository, reading all the others through the API. A local
checkout is never consulted, so a stale one cannot skew a result.

## Running it

```bash
python3 check-drift.py            # report, exit 1 on drift, 2 if it could not finish
python3 check-drift.py --repo gog # one repository
```

It reads each file through `gh api`, so it needs `gh` authenticated. CI runs it
weekly on Mondays and on any change to `configs/` or the script.

## Developing

- See [requirements-dev.txt](./requirements-dev.txt) for the pinned tooling.

```bash
pip install -r requirements-dev.txt
python -m pytest -v      # unit tests
ruff check .             # lint
ruff format --check .    # formatting
```
