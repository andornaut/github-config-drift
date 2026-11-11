# Container check contract

`github-config-drift` owns the shared Make/container interface, canonical references,
compatibility, evolution and checker enforcement. Each repository owns its source,
implementation and CI. `ai-maintainer` consumes the contract and coordinates
rollout. This grants no game runtime or delivery control.

## Command direction

Ordinary native verification entrypoints, including npm, Cargo and Go checks, run
directly on the host when invoked there. They must not delegate to Make, Docker or
a container launcher. Make invokes those native commands in isolated containers;
the dependency direction is one-way. Packaging, provenance and multicontainer host
controllers retain the explicit exceptions below.

`make check` is the only required Make target. It provides the repository's full,
deterministic verification gate for AI Maintainer and other automation, failing if
any required check fails. Additional targets and debugging interfaces are
project-owned. No public category list, metadata target or staged adapter is
required. Existing scripts that delegate native commands to Make require alignment;
source-profile acceptance alone does not establish compliance.

## CI and image identity

Choose CI execution from the project's image requirements:

| Image requirement | CI execution |
| --- | --- |
| A suitable prebuilt image is available | Run native commands in that image without building an image. |
| A custom Dockerfile is required | Use Make to build the project image and run checks. |

CI and Make must use matching image pins and dependency locks, derived from a shared
reference or enforced by a checked invariant. Reuse a suitable prebuilt image
rather than adding a custom image build. A project needing a custom image owns its
Dockerfile, explicit context manifest, build arguments and runtime version inputs.
Execute the image ID returned by its builder; do not add an independent CI image
pin or require a registry. The stock launcher resolves cached or pulled image
references to immutable IDs. Each language installs its frozen dependencies and
runs all requested checks in one container. Dependency installation may explicitly
enable bridge networking; other invocations default to no network.

Custom-image CI may use optional category aliases to preserve separate jobs and
status-check names. A private launcher, command
adapter or output interface is project-owned, not an additional standard API.
Docker orchestration runs on the host virtual machine, not inside a check
container. CI may reuse private provenance/isolation functions with literal native
commands; stock CI does not dispatch project phases or Make.

## Isolation, outputs and resources

Custom images copy the selected public source through their Dockerfile and run
through Make. Stock checks copy a filtered public source view into owned scratch,
bind it read-only at `/source`, and use a writable container copy at `/work`.
Git metadata and credential-shaped paths are excluded. Source status, revision
and file fingerprints must stay unchanged throughout verification; discard
results if they change.

Only explicitly declared generated paths may be exported by a project interface.
Outputs must be ignored content inside the checkout, without tracked source,
symlinks or special files. Projects own their native artifact copy and atomic
publication checks. A product archive may be captured from stdout; multiple
outputs may use an explicit writable scratch binding. The aggregate `make check`
exports nothing.

The canonical launcher caps custom image builds and execution at four CPUs, with
4 GiB memory by default and `nofile=256:256`. A validated `CHECK_MEMORY` setting applies
to both phases. Checks run at `/work` as nonroot UID `19001`, with
temporary HOME and scratch storage. Execution uses a read-only root filesystem,
dropped capabilities, no privilege escalation and bounded processes. Checks default
to no network; reviewed project requirements may enable it.

Resource-only admission helpers require exact reviewed source in the canonical
admission catalog. Preserve existing game quiet windows, load guards and delivery
ownership. Do not add clock or load gates to other repositories as part of this
contract.

Explicit packaging, multicontainer and provenance workflows retain their reviewed
host controllers. Controllers orchestrate isolated jobs and preserve source/image
identity; they do not move test execution onto the host. Existing macOS-only checks
retain their native sandbox route. These are bounded implementation exceptions,
not permission to bypass isolation or required checks.

## Implementation and references

1. Provide native commands that execute directly on the host. Wrap the full gate
   with `make check` without changing that native behavior.
2. Select a suitable prebuilt image, or define the necessary custom Dockerfile and
   explicit build context. Pin tools and freeze dependency locks.
3. Run native CI commands without an image build for the prebuilt path. Use Make
   for custom-image CI, with optional category targets when jobs need them. Verify
   CI/Make image and dependency parity; declare only necessary generated exports.
4. Verify the complete gate, native command path, image/source provenance and
   unchanged fingerprints. Reconcile checker support and affected source copies
   together; do not claim repositories are aligned before their changes are checked.

Derive pins and source identities from these files rather than duplicating a
version or hash inventory in prose:

| Reference | Purpose |
| --- | --- |
| [stock](../configs/container/stock) and [run](../configs/container/run) | Stock-image library and custom-image host launcher. |
| [stock-profiles.json](../configs/container/stock-profiles.json) | Inspected stock helpers, frozen inputs, image bindings and native CI jobs. |
| [check-profiles.json](../configs/container/check-profiles.json) | Inspected dispatchers, Dockerfiles and bridges. |
| [admissions.json](../configs/container/admissions.json) | Reviewed resource-only admission sources. |
| [harnesses.json](../configs/container/harnesses.json) | Reviewed custom harness and dependency proofs. |
| [tools.json](../configs/container/tools.json) and [ShellCheck step](../configs/shell/shellcheck-step.yml) | Shared lint tool pins. |
| [tool manifest](../tests/container/package.json) and [lockfile](../tests/container/package-lock.json) | Frozen Markdown dependencies and scoped security overrides. |
| [Docker setup](../configs/container/docker-setup-step.yml) and [Buildx setup](../configs/container/setup-buildx-step.yml) | Canonical CI Docker tooling. |
