# Release checker

Read-only release readiness checks for EAL/EAS components: release-note
headings on a branch, Docker Hub image tags, and open PR scans.
Requires **Python 3.11+**; no third-party packages.

## Quick start

```powershell
# Full report: release notes + Docker images + open PR scan
python release_check.py --version 2026.3.0-rc1

# Readiness only (no PR scan): release notes + image availability
python release_check.py --version 2026.3.0-rc1 --skip-prs

# Different notes and image-tag versions
python release_check.py --version 2026.3.0 --image-version 2026.3.0-rc1

# Limit scope
python release_check.py --version 2026.3.0-rc1 --category EAL
python release_check.py --version 2026.3.0-rc1 --project vector-retriever --project smart-nvr
```

Other flags: `--branch` (default `main`), `--catalog`, `--output`,
`--discover` (suggest untracked release-note paths),
`--validate-catalog` (offline catalog check, no network).

## Checking only release notes and images

Use `--skip-prs` to skip open PR scanning entirely. The report then verifies,
per project, the exact version heading on the target branch and every required
Docker Hub tag. PR columns are explicitly labeled as skipped, not zero.
This needs roughly one GitHub API request per project, so it usually fits the
unauthenticated limit; a full PR scan does not.

## Authentication

Set `GH_TOKEN` or `GITHUB_TOKEN` through your normal secret-management process,
or sign in with `gh auth login`. The token must be a GitHub **REST API** token
(classic or fine-grained PAT) with read access to the catalog repositories;
git-transport-only credentials stored by Git Credential Manager are rejected by
`api.github.com`. Unauthenticated access is limited to ~60 requests/hour —
enough for `--skip-prs`, not for a full PR scan. The token is sent only to
`api.github.com`, never to Docker Hub. Never put tokens in the catalog, source,
or chat.

## Results

Each run writes timestamped **HTML, CSV, and JSON** to `reports/` (previous
runs are kept) and prints separate EAL/EAS Markdown tables with six columns:
**Component | Release notes on `<branch>` | Docker images |
Open release-note PR | Other PRs – age | Dependency PRs – age**.
Components link to their GitHub directories; images link to exact-tag-filtered
Docker Hub pages, including missing tags.

| Status | Meaning |
|---|---|
| Done | Exact version heading on branch / image tag published |
| Not done | Heading or file absent / tag not publicly found |
| Partial | Some required images published, others missing |
| Check failed | API, auth, rate-limit, network, or format error |

Notes require an exact version **heading** on the target branch; an open PR
does not count. If the requested version is absent, the first heading actually
on the branch is shown. Every open PR in the selected repositories is scanned
and categorized with mutually exclusive precedence
**release-note → dependency (heuristic) → other**; PRs touching only paths
outside the selected projects go to the out-of-scope inventory.
Reconciliation deduplicates by repository/PR number:
release-note + dependency + other = mapped;
mapped + out-of-scope + unclassified = enumerated open PRs.
Incomplete scans are labeled in every PR cell and never reported as zero.
Owners stay Pending until explicitly verified; they are never derived from PR
or commit authors.

Exit codes: `0` all selected projects ready; `1` incomplete release;
`2` check/configuration/report error.

## Catalog

`projects.json` holds the project list in display order. Per project:

- `name`, `category` (`EAL`/`EAS`), `repo` (`owner/repository`), `path`
  (repository-relative; empty for repo root)
- `owner`: verified owner, `null` = Pending
- `images`: required first-party images; short names expand to
  `intel/<name>:{version}`, objects support custom tags, e.g.
  `{"repository": "intel/chatqna", "tag": "core_{version}"}`
- optional: `release_notes` (overrides `<path>/docs/user-guide/release-notes.md`),
  `pr_paths` (broader directories for PR mapping), `mapping_note`

Validate edits offline:

```powershell
python release_check.py --version 2026.3.0 --validate-catalog
```

## Agent skill

Open this repository as the workspace root and invoke `/release-readiness`
(or ask "run the release readiness report"). The skill
([SKILL.md](.github/skills/release-readiness/SKILL.md)) asks for the exact
notes/image versions, scope, and any catalog changes; confirms and persists
catalog edits; validates the catalog; runs the CLI; and presents EAL/EAS tables
plus deduplicated PR reconciliation with links to the generated reports.
It is read-only towards GitHub and Docker Hub: no image pushes, no PR
creation or merging.

## Tests

```powershell
python -m unittest -v
```
