---
name: repo-consistency
description: Audit @andornaut's GitHub repositories for consistency and correctness across repo settings, rulesets, CI/CD workflows and releases. Invoke when asked to review repos for consistency, check CI/CD across repositories, find configuration drift, or verify that global patterns hold. Reports deviations so each can be confirmed as deliberate or repaired.
---

# Repository consistency audit

Establishes what the global pattern is by majority across repositories, then reports
every deviation from it. A deviation is not automatically a defect: the point is to
surface each one so the operator confirms it as deliberate or repairs it.

Run `python3 check-drift.py` in this repository first. It compares the shared
configuration files (`.golangci.yml`, `ruff.toml`, `.markdownlint-cli2.yaml`,
`eslint.config.base.mjs`, `.husky/pre-commit` and
`.github/workflows/ai-attributions.yml`), the prettier entry in `.lintstagedrc`,
and the ShellCheck and markdownlint steps pulled out of whichever workflow carries
them. It reports absence as well as difference. This audit covers what that script does not read: repository
settings, rulesets, workflow structure, and releases. Do not re-derive file drift
by hand.

## Scope

Audit repositories owned by the operator, excluding:

- **Archived repos**: read-only, never modify.
- **Forks**: `.github/` and settings are upstream's. Exclude them from pattern
  derivation as well, or they skew the majority.

```bash
gh api --paginate 'user/repos?per_page=100&affiliation=owner' > repos.json
```

`user/repos` omits merge settings (`allow_squash_merge`, `delete_branch_on_merge`,
`allow_auto_merge`) and `security_and_analysis`. Those need a per-repo
`gh api repos/OWNER/NAME`. Fetch them in parallel; treat a list-endpoint `false` for
those fields as "not reported", not as the real value.

## Gather

Run these concurrently, one output file per repo, then analyze offline. Re-reading
cached JSON beats re-querying while iterating on the analysis.

| Dimension | Endpoint |
|---|---|
| Settings, features, security | `repos/{r}` |
| File inventory | `repos/{r}/git/trees/{default_branch}?recursive=1` |
| Workflow bodies | `repos/{r}/git/blobs/{sha}` (from the tree, base64) |
| Rulesets | `repos/{r}/rulesets`, then `repos/{r}/rulesets/{id}` for rules |
| Legacy branch protection | `repos/{r}/branches/{b}/protection` |
| Default token scope | `repos/{r}/actions/permissions/workflow` |
| Run history | `repos/{r}/actions/runs?event=push` with `--paginate` |
| Tags and releases | `repos/{r}/tags`, then `repos/{r}/releases/tags/{tag}` |

The rulesets **list** endpoint returns only id/name; rules require the per-id fetch.

### A zero is not an answer

Every false finding this audit has produced came from a valid-shaped short result
read as the whole answer: an empty one taken for a real zero, or a truncated one
taken for a real count. Before reporting that something is missing or that a set is
smaller than canon says, establish that the query could have found the rest.

- Backgrounded `gh` calls fail silently under load. After fetching, assert every
  expected output file exists and is non-empty, and refetch the gaps. Do the
  assertion by diffing the set of files you expected against the set on disk, not by
  re-walking the same work list: a list written with `'\n'.join(...)` has no
  trailing newline, so `while read` drops its final entry and the gap check inherits
  the same blind spot. Terminate generated work lists with a newline.
- Brace a variable an underscore follows when a fan-out script names its output
  file: `$r__runs.json` parses as `${r__}`, which is unset, so every repository
  writes to the same path. That file exists and is non-empty, so the gap check
  above passes while one repository's data stands in for all of them. Write
  `${r}__runs.json`.
- `gh api --slurp` needs gh 2.55 or newer. Where it is missing, the unknown-flag
  error goes to stderr and the output file is left zero length. Fetch run history
  as JSONL with `--paginate --jq '.workflow_runs[]'`, which works either way.
- `gh api --jq` takes exactly one argument, the filter. Passing `--arg name value`
  alongside it fails on stderr and writes nothing to stdout, which a pipe into
  `wc -l` reads as zero findings. Interpolate the shell variable into the filter
  string instead.
- `actions/runs?branch={ref}` returns an empty array for some tag refs whose run
  does exist. Fetch `runs?event=push --paginate` and filter `.head_branch`
  client-side.
- `actions/runs` also serves full, well-formed pages whose newest run is days old.
  Compare `max(.workflow_runs[].created_at)` against that repo's `.pushed_at` from
  `repos/{r}` and refetch until they agree.
- `gh api --jq` writes the filter's output even when the request failed: a 404
  body has no `.tag_name`, so `--jq '.tag_name'` writes `null` and the file is
  non-empty. A presence test keyed on file size therefore reads every missing
  object as present. Key it on the exit status or on a non-empty stderr instead.
- Never pipe a search whose result you intend to count through `head`. A truncated
  list reads as a smaller estate, which turns a correct canon count into an apparent
  drift. Count the full result, and cap the output only after the count is taken.
- `git ls-tree -r` lists a submodule as one `commit` entry rather than as the files
  inside it, so every path under a submodule reads as absent to anything built on
  that listing. Resolve the submodule's pinned commit before calling such a path
  missing.
- In any loop that counts, default an empty capture to a non-zero sentinel, so a
  failed query cannot read as success.

## Checks that find real problems

Ordered by how often they surface something.

### Required status checks vs jobs that actually run

The highest-value check. Parse each workflow's `jobs.*.name` (falling back to the
job id) and its triggers, then compare against the ruleset's required contexts.

- **Ruleset requires a context no job produces**: nothing ever reports it, so every
  PR blocks forever. A required check that stops reporting is indistinguishable from
  one that never ran.
- **A ruleset named for requiring CI requires nothing**: the branch is unguarded
  despite looking protected.
- **A job runs on the default branch but is not required**: a gate that is advisory.

Only count jobs reachable from `pull_request` or a **branch-scoped** `push`. A
`push:` block with only `tags:` is release-only and must not be counted. Matrix jobs
report as `Name (leg)`, so a stable aggregator job is what a ruleset can require.

A check name is its job's name, and contains spaces and matrix legs: `AI
attributions`, `Test (node 20)`. Never split a check name on whitespace to parse a
status list. Select on `conclusion` and compare whole names.

### Default `GITHUB_TOKEN` permissions

`default_workflow_permissions` should be `read` and `can_approve_pull_request_reviews`
`false`. Before tightening, confirm every workflow in that repo declares its own
`permissions:`, so nothing depends on the loose default.

### Dependabot ecosystem coverage

Cross-check declared `package-ecosystem` entries against manifests actually present:
`package.json`→npm, `go.mod`→gomod, `Cargo.toml`→cargo, `requirements.txt`/
`pyproject.toml`→pip, `Gemfile`→bundler, `Dockerfile`→docker. Check nested manifests
too: a `directory: "/"` entry does not cover a subdirectory. Flag both missing
ecosystems and declared ones with no manifest.

### Action version pinning

Tally `uses:` across all workflows. Expect one version per action. Legitimate
floating refs: the operator's own actions at a major tag (`@v1`), and actions whose
documented usage is a branch (`dtolnay/rust-toolchain@stable`). Anything else
floating, or two versions of one action across repos, is drift.

### Checks that reach the network to decide

A gate that fetches something at run time reports a red check when that host is
slow, and the failure looks like the change under test. Look for the shape in any
action input defaulting to a remote lookup.

`golangci-lint-action`'s `verify: true` is the known one: it runs `golangci-lint
config verify`, which fetches a schema from `golangci-lint.run`. It stays on, and a
slow host is a re-run rather than a reason to remove it. `golangci-lint run` accepts
an unknown key inside `linters.settings` and exits 0, so a misspelled one disables
that setting while CI stays green, and only `config verify` rejects it. Settled: do
not re-propose turning it off.

### Release gated on its checks

Workflows do not wait on each other. A green scan running *beside* a release says
nothing about whether the release waited for it. A release job must reach its checks
through `uses: ./.github/workflows/x.yml` plus `needs:`, not run alongside them.

### Test matrix vs shipped platforms

Compare the test matrix against release targets (`.goreleaser.yaml` `goos`, or
equivalent). Shipping a platform that is never tested is a real gap; testing only
what is shipped is correct and should not be flagged.

### CI health

Group runs by workflow **path** and take the latest per path. A run's `name` is
whatever the file's `name:` key held at the time, falling back to the raw path, so
grouping by name splits one workflow's history the moment that key is added or
changed and surfaces a superseded failure as current. The `path` is stable across
both. Exclude `event: dynamic` runs here: those are Dependabot's, covered below.

This reports the health of the default branch and nothing else. Tags need their own
pass.

### Releases exist for every version tag

Latest-run-per-path reports current health, not release health: a green push run on
main supersedes the tag run for the same path, so a release that failed at its tag
reads as success, leaving the tag in place with no release behind it. Nothing else
surfaces that, and a registry publish done by hand still succeeds, so the package
exists while the GitHub release does not.

For every `v*.*.*` tag, confirm a release:

```bash
gh api "repos/$r/releases/tags/$TAG"
```

Tags predating the repository's `release.yml` legitimately have none: compare the
tag date against when that file was added before calling one a gap. Compare full
timestamps rather than dates: a repository whose first release workflow landed in
the same push as its first tag has the two minutes apart, and a day-granularity
comparison reports that tag as a gap. Zero runs for the tag confirms it predates
the workflow. Confirm a suspected gap both ways, by the client-side run scan and by
this endpoint. A tag with a release but no run found is a filter artifact. A tag
with a run and no release is the real finding.

### Concurrency groups

A workflow reachable through `workflow_call` carries a literal prefix in its
`concurrency.group` (`test-`, `attributions-`), so the run a caller starts and the
run a push starts do not cancel each other. A workflow that is not callable begins
its group with `${{` and is correct as it is. The split between the two shapes is
the convention, not drift.

### Dependabot update jobs

Dependabot's own runs appear in the Actions list under per-job names like
`bundler in /. - Update #1519000683`, one name per job, so a per-workflow-name
sweep never sees a repeat and a page-limited fetch drops them entirely. Query them
by pattern instead:

```bash
gh api "repos/$r/actions/runs?per_page=100" \
  --jq '[.workflow_runs[]|select(.name|test("Update #"))|select(.conclusion=="failure")]|length'
```

A failure here means updates for that ecosystem silently stop while CI stays green.
Read the log for the cause. `dependency_file_not_resolvable` against a language
version means the manifest never declared a runtime, so Dependabot picked its own
default. Declare it in the manifest: Dependabot does not read sidecar version files
such as `.ruby-version`, and a `required_ruby_version` in a gemspec does not select
a runtime. Adding the directive makes the resolver write a version section into the
lockfile, and CI that installs with frozen set refuses to add one itself, so commit
the lockfile in the same change, carrying the version CI installs.

### Comments that no longer describe the code

Where a workflow comment states behavior, verify it against the implementation.
Copied-and-drifted comments outlive the behavior they described, and a wrong comment
about what a check rejects is a correctness defect.

## Fix vs ask

**Fix without asking** when the change matches an existing majority and has no
behavioral effect: correcting a factually wrong comment, tightening an unused token
default, restoring canonical wording.

**Ask** when the change alters enforcement or requires a commit to a repo:
adding required status checks, enabling scan flags, renaming a repo. Present the
peer groups that already exist so the choice is "match this group" rather than an
open design question.

Prefer landing a deviant file byte-identical to the canonical version, and verify
with `diff` against `configs/` in this repository.

## Editing rulesets

`PUT repos/{r}/rulesets/{id}` **replaces** the ruleset. Read the current one, modify
the rules array, and send back `name`, `target`, `enforcement`, `conditions`, the
full `rules` array, and `bypass_actors` reduced to `actor_id`/`actor_type`/
`bypass_mode`. Omitting `rules` entries silently drops them, so re-read after
writing and confirm the untouched rules survived.

`GET repos/{r}/rulesets/{id}` omits `bypass_actors` inconsistently. Never conclude
"no bypass" from it alone: read it twice, or cross-check `current_user_can_bypass`,
which reads `always` or `never` and is reliable.

The bypass split is deliberate. **Branch** rulesets (`require-ci`, `protect-main`)
carry `actor_id 5 / RepositoryRole / always`, because `required_status_checks` gates
any ref update rather than only merges, and without the bypass a direct push to main
is refused. **Tag** rulesets (`protect-release-tags`) carry none, because no
automation touches `v*.*.*`. Do not "fix" a branch ruleset by stripping its bypass,
and do not report a tag ruleset's empty list as a gap.

Deleting or force-moving a `v*.*.*` tag therefore needs the ruleset amended first,
which is the operator's call. When asked for it: PUT the full object with
`bypass_actors` set to the same `actor_id 5 / RepositoryRole / always`, delete the
tag, then PUT it back with `bypass_actors: []`. Save the GET output first and diff
the restored object against it, because the read-back is the only proof the window
closed. Confirm the rule is live again by pushing a delete of a tag that does not
exist and matches the pattern (`git push origin :refs/tags/v9.9.9`): the ruleset
refuses before it checks whether the ref exists, so the probe cannot lose a tag.
Deleting a tag also breaks the `compare/<prev>...<this>` link in the next release's
generated notes; repair it with `POST releases/generate-notes` naming the surviving
previous tag, then PATCH the body.

## Committing

Repos may have unrelated uncommitted work, and other agent sessions may hold a
working tree. Check `git status` first, stage only the specific file changed, and
never sweep up unrelated modifications. Push after committing.

The `dotfiles` repo is not under the projects directory: it is the gog-managed clone
at `~/.local/share/gog/andornaut/`.

## Verify

Do not report a settings change as done without reading it back, and do not report a
committed workflow change as done without checking the run it triggered. When a run
fails, read the log before attributing the failure: an unrelated network timeout in a
linter fetching a remote schema is not a regression in the change that triggered it.
