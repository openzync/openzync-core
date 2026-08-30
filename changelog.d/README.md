# changelog.d

News fragments for [towncrier](https://towncrier.readthedocs.io/).

## Naming

```
<issue>.<type>.md
```

- `<issue>` — issue/MR number or `+` prefix for orphan (e.g. `123`, `+trivial`)
- `<type>` — one of the configured fragment types:

| type       | heading          | notes |
|------------|------------------|-------|
| `breaking` | Breaking Changes | breaking API/contract change |
| `feature`  | Added            | new feature |
| `bugfix`   | Fixed            | bug fix |
| `change`   | Changed          | behaviour change (non-breaking) |
| `removal`  | Removed          | removed feature/API |
| `doc`      | Docs             | documentation only |
| `internal` | Internal         | internal/tooling (shown in changelog) |
| `misc`     | Misc             | hidden (`showcontent = false`) — used only to satisfy `towncrier check` |

Orphan fragments use the `trivial` prefix: `trivial.<type>.md` or `+something.<type>.md`. Use for changes that don't need an issue number.

## Examples

```bash
# create fragments (requires towncrier)
towncrier create 123.feature.md --content "Add unified org permission model."
towncrier create 124.bugfix.md --content "Fix duplicate idealization on concurrent webhook deliveries."
towncrier create trivial.doc.md --content "Fix typo in README."

# check that a fragment exists for this branch
towncrier check --compare-with origin/main

# build draft (does not modify CHANGELOG.md)
towncrier build --draft --version 1.0.0

# build release (writes CHANGELOG.md, deletes fragments)
towncrier build --version 1.0.0 --yes
```

Fragments are deleted on `towncrier build --version X --yes` and their content is appended under the new `## [X] - YYYY-MM-DD` heading in `CHANGELOG.md`.

## CI

- MRs must include a fragment or label `trivial` / `skip-changelog` to bypass the check.
- On tag `v*` / `*.*.*`, CI runs `towncrier build --version $CI_COMMIT_TAG --yes` and pushes the updated `CHANGELOG.md`.
