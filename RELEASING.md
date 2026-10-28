# Release Checklist

Run this checklist before pushing any release to the public repo.
All development happens in `picard-dev`; only squash-merge releases go to `picard`.

---

## Pre-release

- [ ] All tests pass: `python -m pytest --tb=short -q -p no:timeout`
- [ ] Version bumped in `pyproject.toml`
- [ ] `CHANGELOG.md` entry written for the release (no unreleased note)
- [ ] No unintended files staged (`git status`)

## SBOM

Generate a fresh CycloneDX 1.6 SBOM from the release environment:

```bash
pip install cyclonedx-bom
cyclonedx-py environment --output-format json --output-file sbom.cdx.json
```

- [ ] `sbom.cdx.json` regenerated from a clean install of the release version
- [ ] `sbom.cdx.json` committed and included in the release tag

## Build and verify

```bash
pip install build
python -m build
twine check dist/*
```

- [ ] Wheel and sdist build without warnings
- [ ] `twine check` passes

## Final checks

- [ ] No AI attribution in git history: `git log --grep="Co-Authored-By" --grep="Generated with"`
- [ ] README reflects current adapter count and feature set
- [ ] MCP tool descriptions match the actual tool signatures in `mcp_server.py`

## Publish

- [ ] Tag: `git tag -s v<version> -m "v<version>"`
- [ ] Squash-merge to `picard` (public repo) after explicit review agreement
- [ ] Push tag to public: `git push public v<version>`
- [ ] GitHub release created from tag with CHANGELOG entry as body and `sbom.cdx.json` attached
- [ ] PyPI: `twine upload dist/*`
