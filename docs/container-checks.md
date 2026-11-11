# Container checks

`github-config-drift` owns the shared Make/container convention and canonical
configuration. Each repository owns its implementation and CI. `ai-maintainer`
runs the repository's checks. This does not transfer runtime or delivery control.

## Commands

`make check` is the only required Make target. It runs the complete verification
gate in containers and fails when a check fails. Additional targets are
project-owned.

Native npm, Cargo, Go and other verification commands run directly on the host
when invoked there. They do not delegate to Make or Docker. Make invokes those
native commands in containers. Packaging and multicontainer tasks may retain
ordinary host orchestration; test execution remains isolated.

## CI

| Project requirements | CI execution |
| --- | --- |
| A suitable prebuilt image exists | Run native commands in that image without building an image. |
| A custom image is necessary | Use Make to build the Dockerfile with `COPY` source and run checks. |

Make and CI use the same image references and frozen dependency locks. An aggregate
gate installs each image/lock combination once and runs its checks in that
container. Separate CI jobs may repeat setup. Distinct tool dependency trees may
use separate containers.

## Isolation

The [shared stock helper](../configs/container/stock) supplies ordinary Docker
execution and cleanup. It copies selected public source into a read-only bind
view, with a writable container copy at `/work`. Git metadata and
credential-shaped files are excluded. Custom images copy their source through
their Dockerfile.

Checks run as nonroot UID `19001`, with temporary HOME and scratch storage, a
read-only root filesystem, dropped capabilities and no privilege escalation.
Defaults are four CPUs, 4 GiB memory, bounded processes and `nofile=256:256`.
Project resource settings may adjust memory and scratch sizes. Dependency setup
may explicitly enable bridge networking; other invocations default to no network.

Projects write ordinary build outputs. The shared helper also permits an explicit
output-directory bind when a project needs files from a container. Existing
macOS-only checks retain their native sandbox route.

## References

- [Makefile](../Makefile) and [root helper](../tests/container/check-stock)
- [Docker setup](../configs/container/docker-setup-step.yml)
- [ShellCheck pin](../configs/shell/shellcheck-step.yml)
- [Markdown tool manifest](../tests/container/package.json) and [lockfile](../tests/container/package-lock.json)
