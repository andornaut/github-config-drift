---
name: repo-consistency
description: Audit @andornaut's GitHub repositories for consistency and correctness across repo settings, rulesets, and CI/CD workflows. Invoke when asked to review repos for consistency, check CI/CD across repositories, find configuration drift, or verify that global patterns hold. Reports deviations so each can be confirmed as deliberate or repaired.
---

# Repository consistency audit

Establishes what the global pattern is by majority across repositories, then reports
every deviation from it. A deviation is not automatically a defect: the point is to
surface each one so the operator confirms it as deliberate or repairs it.

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
| CI health | `repos/{r}/actions/runs?branch={b}&per_page=20` |

The rulesets **list** endpoint returns only id/name; rules require the per-id fetch.

Backgrounded `gh` calls fail silently under load. After fetching, assert every
expected output file exists and is non-empty, and refetch the gaps. Do the assertion
by diffing the set of files you expected against the set on disk, not by re-walking
the same work list: a list written with `'\n'.join(...)` has no trailing newline, so
`while read` drops its final entry and the gap check inherits the same blind spot.
Terminate generated work lists with a newline.

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

Weigh it against what the lookup buys before removing it. `golangci-lint-action`
defaults to `verify: true`, which runs `golangci-lint config verify`; that command
builds a `https://golangci-lint.run/jsonschema/golangci.vN.M.jsonschema.json` URL
and fetches it, so every run depends on that host. Keep it on anyway: `run` accepts
an unknown *settings* key silently and exits 0, so a key misspelled inside
`linters.settings` disables that setting while CI stays green, and only `config
verify` rejects it. An unknown *linter name* is caught either way, by `run` itself.
Trading a loud, retryable network failure for a silent one is the worse deal.

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
The log names the cause; `dependency_file_not_resolvable` against a language version
usually means Dependabot picked its own default runtime because the manifest never
declared one. Declaring it in the manifest is what fixes it: Dependabot does not read
sidecar version files such as `.ruby-version`, and a `required_ruby_version` in a
gemspec does not select a runtime.

Adding a language directive makes the resolver write a version section into the
lockfile. CI that installs with frozen set refuses to add one itself, so commit the
lockfile in the same change, carrying the version CI installs rather than the one on
the machine that generated it.

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
with `diff` against a repo known to carry it.

## Editing rulesets

`PUT repos/{r}/rulesets/{id}` **replaces** the ruleset. Read the current one, modify
the rules array, and send back `name`, `target`, `enforcement`, `conditions`, the
full `rules` array, and `bypass_actors` reduced to `actor_id`/`actor_type`/
`bypass_mode`. Omitting `rules` entries silently drops them, so re-read after
writing and confirm the untouched rules survived.

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
