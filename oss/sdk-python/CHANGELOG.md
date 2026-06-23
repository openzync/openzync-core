# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-06-23

### Added
- First PyPI release as `openzync`.
- Migrated to `src` layout (PEP 517) with Hatchling build backend.
- Dynamic versioning via Git tags (`hatch-vcs`).
- `py.typed` marker for PEP 561 type hint support.
- CHANGELOG.md, `.gitignore`.
- GitHub Actions CI/CD for publishing to PyPI and TestPyPI (trusted publishing).

### Changed
- Renamed PyPI package from `openzep-py` to `openzync`.
- Renamed import package from `openzep` to `openzync`.
- Moved package from flat layout to `src/openzync/`.
- Upgraded build system from setuptools to Hatchling.
- Version now derived from Git tags (via `hatch-vcs`), no longer hardcoded.
- Updated `User-Agent` header to use runtime version detection.
- Classifier updated to "Development Status :: 4 - Beta".
- Dependency bounds relaxed to minimum-version ranges.

### Fixed
- User-Agent version string now tracks package version automatically.
