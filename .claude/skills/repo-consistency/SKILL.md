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
them. It also reads each workflow for the shape every one of them holds, which
costs no further calls: a job with no `timeout-minutes`, a file whose rationale
comment is missing or names a cap the file no longer declares, a workflow with no
top-level `permissions:`, and an action followed at a loose ref or at two versions
across the estate. It reports absence as well as difference.

This audit covers what that script does not read: repository settings, rulesets,
releases, and the parts of a workflow that are about how it is wired rather than
how it is shaped, meaning triggers, `needs:` gating and the test matrix. Do not
re-derive file drift, workflow shape or the action tally by hand.

Read its exit status, not only its last line: 0 is a clean sweep, 1 is drift, and 2
is a sweep that could not be completed and is reporting nothing. Treat 2 as unknown
rather than clean, and re-run it before relying on any of the file-drift layer.

## What this file may record

Record the lookup, not its answer. A version number, a count, or a repository's
current state written down here is wrong by the next release, and a stale audit
guide reports drift that is not there. Name the command that derives the fact
instead. Illustrating a shape is fine (`3.12` is a minor, `v24.13.1` is a patch);
asserting current state is not.

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

A valid-shaped short result read as the whole answer is what produces a false
finding here: an empty one taken for a real zero, or a truncated one taken for a
real count. Before reporting that something is missing or that a set is smaller
than canon says, establish that the query could have found the rest.

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
- A per-repository check that prints only when it finds something says exactly the
  same thing about a clean repository and about one whose query failed: nothing.
  Silence across the estate is then indistinguishable from a fan-out that never
  ran. Write a result file and an exit status per repository, assert every expected
  one is present and zero, and derive the finding from those rather than from what
  reached the terminal.

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

`check-drift.py` tallies this, so read its report rather than counting `uses:` by
hand. What the report cannot decide is whether a ref it names is deliberate. The
exemptions it already knows are the operator's own actions at a major tag (`@v1`),
a commit SHA, and `dtolnay/rust-toolchain` at a channel or a declared Rust floor,
one of each. A new exemption belongs in the script beside those, not in a note
here: an exemption nobody encoded is one the next run reports again.

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

A release workflow that runs no jobs on a push to the default branch is not drift
by itself: a rolling `dev` release needs an asset name that can drop its version,
which not every package format allows. Read the comment beside the job.

### Runtime pins against upstream support

A repository names its runtime in a file (`.nvmrc`, `.ruby-version`,
`.python-version`, `go.mod`) and nothing moves it: Dependabot does not read any of
them, so a pinned line goes on being installed after upstream stops patching it.
Read each pin against the release calendar and report any past its `eol` date.

```bash
curl -sS https://endoflife.date/api/ruby.json \
  | jq -r '.[]|"\(.cycle) latest=\(.latest) eol=\(.eol)"'
```

This reads a support window, so it applies to a runtime that has one: Ruby, Node,
Python and Go all keep several lines alive at once and retire the oldest on a date.
A rapid-release language does not, and endoflife.date marks every version but the
newest as past `eol` for those, which is not a finding. Rust is the one to expect:
the `rust-version` in `Cargo.toml` and the toolchain a job pins to that same number
are a minimum a consumer must have, not the runtime CI installs, and sitting a
release or two behind stable is what a floor is for. Read a declared floor against
the refs beside it rather than against whatever stable is that week:

```bash
gh api "repos/$r/contents/Cargo.toml" --jq .content | base64 -d | grep rust-version
gh api "repos/$r/contents/.github/workflows" --jq '.[].name' | while read -r f; do
  gh api "repos/$r/contents/.github/workflows/$f" --jq .content | base64 -d \
    | grep -n 'rust-toolchain@' | sed "s|^|$f: |"
done
```

A pin naming a minor (`3.12`) takes the newest patch on every run; one naming a
patch (`v24.13.1`, `3.2.2`) freezes until someone edits it, so those are the ones
that go stale. Moving one is more than the version file. The lockfile records the
runtime as well (`RUBY VERSION` in `Gemfile.lock`), and CI installing with frozen
set refuses to rewrite it, so regenerate the lockfile under the new runtime rather
than editing that stanza. A linter may declare the version separately
(rubocop's `TargetRubyVersion`), and rubocop rejects a gemspec whose
`required_ruby_version` disagrees with it. A package's own floor
(`required_ruby_version`, `engines.node`, `rust-version`) becomes a claim nothing
checks the moment CI stops running at it, so move the floor with the pin or add a
job that runs there.

### Every path a workflow names resolves

A path in a workflow is not checked until the run reaches it, and a `uses: ./` that
names nothing fails the whole workflow rather than one step. Resolve each against
the repository's tree: `uses: ./...`, `{go,node,python,ruby}-version-file:`,
`working-directory:`, and a trigger's `paths:` filter. Normalise a leading `./`
with `removeprefix`, never `lstrip('./')`, which strips every leading dot and makes
each dotfile read as missing. Prove the checker can fail, by pointing one reference
at a name that is not there, before believing it found none.

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

Drop a path the tree no longer carries before taking the latest. Its runs are a
deleted workflow's history, so a workflow removed while red reports as a failing
gate that nothing runs any more.

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
`bundler in /. - Update #1519000683`, one name per job, so a per-workflow-name sweep
never sees a repeat. Select them by event rather than by name, and paginate: on a
repository with heavy push traffic the first page holds nothing but push runs, so a
single-page fetch returns zero update jobs for a repository that has dozens.

```bash
gh api --paginate "repos/$r/actions/runs?event=dynamic&per_page=100" \
  --jq '.workflow_runs[]|select(.conclusion=="failure")|.name'
```

A failure here means updates for that ecosystem silently stop while CI stays green.
Read the log for the cause. `dependency_file_not_resolvable` against a language
version means the manifest never declared a runtime, so Dependabot picked its own
default. Declare it in the manifest: Dependabot does not read sidecar version files
such as `.ruby-version`, and a `required_ruby_version` in a gemspec does not select
a runtime. Adding the directive makes the resolver write a version section into the
lockfile, and CI that installs with frozen set refuses to add one itself, so commit
the lockfile in the same change, carrying the version CI installs.

### Links that resolve only through a redirect

A link that works today because the site rewrites it is one upstream decision from
breaking, and nothing in CI looks at links at all. Request each one **without**
following redirects and read the status: 200 is the page, a 3xx is the site
correcting you.

Sort what comes back before changing anything. A redirect to the identical page over
https, or one GitHub issues for a repository that was renamed, is the site naming its
own canonical address and is worth adopting. A redirect to a login page, a Stack
Exchange `/a/NNN` short permalink expanding to its question, or `discord.gg` becoming
`discord.com/invite` is normal behaviour and not drift. A deep page that lands on a
docs root has lost the content, so following it makes the link worse.

Two ways this check lies:

- A status is about the client as much as the page. Stack Exchange, Fandom, O'Reilly
  and GitLab answer a scripted request with 403, freedesktop.org with 418, and a host
  hit in parallel answers 429; none of that means the page is gone. The VS Code
  Marketplace and crates.io return 404 without an `Accept: text/html` header, and
  crates.io returns 200 for a crate that does not exist. A `000` is a dead domain, a
  TLS failure or a timeout, and only `getent hosts` separates the first from the rest.
  Take a non-200 as a question, never as a verdict, and recheck it serially.
- Replacing a URL that is a prefix of another URL in the same file corrupts the longer
  one, whichever order the replacements run in: rewriting `https://example.com/` to
  `https://example.com/ca` turns an untouched `https://example.com/ca/downloads` into
  `/caca/downloads`. Anchor the match on the full markdown link, or check afterwards
  that every link the edit introduced still resolves. Verify the result, not the plan:
  a batch of link edits is exactly the kind of change whose damage is invisible until
  someone clicks.

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

Land a deviant file as close to the canonical version as its comparison allows.
`eslint.config.base.mjs`, `.husky/pre-commit` and the attributions workflow are compared
byte for byte, so `diff` against `configs/` in this repository is the check.
`.golangci.yml`, `ruff.toml` and `.markdownlint-cli2.yaml` are compared with the
declared-local keys stripped, so `diff` reports differences that are allowed: run
`check-drift.py --repo` against the repository instead of reading that diff as
drift.

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
