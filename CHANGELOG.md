# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-05

### Added
- uv-managed Python 3.12 package `opspilot` with settings, JSON logging and the
  `opspilot` CLI.
- Makefile with setup, quality, infrastructure, demo, fault and housekeeping targets.
- Docker Compose stack with Weaviate (all modules disabled) and an opt-in Phoenix
  tracing profile.
- k3d cluster configuration for a lean single-node cluster.
- Shopfront demo app: one env-driven service image plus redis, deployed with kustomize.
- RBAC for the read-only `opspilot-reader` and the narrowly scoped
  `opspilot-operator`, with a script that writes short-lived kubeconfigs.
- Fault-injection framework: scenario schema, injector, CLI and five scenarios
  with ground-truth root causes.
- Unit and integration tests, pre-commit hooks and GitHub Actions CI.
- ADRs 0001–0003, README and progress log.

[Unreleased]: https://github.com/singha105/opspilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/singha105/opspilot/releases/tag/v0.1.0
