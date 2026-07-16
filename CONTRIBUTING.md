# Contributing to Citefold

Thanks for helping make agent memory more inspectable and trustworthy.

## Before opening a change

- Search existing issues and pull requests before starting substantial work.
- Open an issue first for public API changes, storage-format changes, new
  dependencies, or changes to benchmark methodology.
- Keep changes focused. Do not mix an unrelated refactor into a bug fix or
  feature pull request.
- Never commit API keys, private memory stores, raw user media, or benchmark
  datasets that are not licensed for redistribution.

## Local setup

Citefold supports Python 3.9 through 3.13 on Linux and macOS. Windows is not a
supported target yet because cross-process locking semantics have not been
validated there.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,docs]"
```

Run the test suite:

```bash
python -m pytest
```

Build and validate the distributions:

```bash
python -m build
python -m twine check --strict dist/*
```

Build the documentation locally:

```bash
mkdocs build --strict
```

The documentation workflow validates every relevant push and pull request but
does not deploy by default. After an administrator selects **GitHub Actions**
as the Pages source and creates the repository variable `ENABLE_PAGES=true`,
pushes to `main` also deploy the site. A maintainer can request deployment from
a manual run with the `deploy` input.

## Pull requests

A pull request should:

1. Explain the user-visible problem and the chosen approach.
2. Add or update behavior-focused tests when behavior changes.
3. Update documentation and `CHANGELOG.md` when the public interface changes.
4. Preserve evidence provenance and scope boundaries.
5. Pass CI on every supported Python and operating-system combination.

Use descriptive commit messages and keep generated files out of commits.

## Benchmark and research claims

Retrieval diagnostics, end-to-end question answering, synthetic product tests,
and real-media model quality are different forms of evidence. Do not compare
them as if they were the same metric. Every new reported result must record:

- the Citefold commit when available; otherwise, the labeled source snapshot
  and an explicit statement that the result is not commit-bound;
- dataset name, revision, and checksum;
- exact command and configuration;
- model and provider, when applicable;
- sample count, exclusions, and limitations.

Do not call an unofficial run a leaderboard result or claim real-world
multimodal quality from deterministic fixtures.

## Licensing

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE). Contributions derived from third-party material
must retain the relevant notices and update `THIRD_PARTY_NOTICES.md`.

## Maintainer release checklist

PyPI publication is performed only by `.github/workflows/release.yml` through
Trusted Publishing; do not upload distributions from a maintainer laptop.

Before the first release, configure a PyPI Trusted Publisher for owner
`jappre`, repository `citefold`, workflow `release.yml`, and environment
`pypi`. Protect that GitHub environment so only release tags can publish.
Also enable GitHub private vulnerability reporting so the channel documented
in `SECURITY.md` is available before the first tagged release.

For each release:

1. Update the version in both `pyproject.toml` and `citefold/__init__.py`.
2. Move the relevant changelog entries from `Unreleased` into the versioned
   section and update `CITATION.cff`.
3. Run tests, `mkdocs build --strict`, `python -m build`, and
   `python -m twine check --strict dist/*`.
4. Create a `v<version>` tag from a green `main` commit.
5. Publish the matching GitHub Release. The release workflow checks the tag
   and both version declarations before requesting a short-lived PyPI token.
