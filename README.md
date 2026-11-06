# Release checker

Read-only release readiness checks for EAL/EAS components: release-note
headings on a branch, Docker Hub image tags, and open PR scans.
Requires **Python 3.11+**; no third-party packages.

## Run with PR scan (full report)

Checks release notes, Docker images, and every open PR in the catalog
repositories.

1. Set `GH_TOKEN` or `GITHUB_TOKEN` (see [Authentication](#authentication)) —
   required, a full PR scan exceeds the unauthenticated rate limit.
2. From the repository root, run:

   ```powershell
   python release_check.py --version 2026.3.0-rc1
   ```

3. Read the EAL/EAS tables in the terminal and the timestamped HTML/CSV/JSON
   under `reports/`.

## Run without PR scan (release notes + images only)

Checks only the release-note heading on the branch and Docker Hub tag
availability. Needs ~1 GitHub request per project, so it usually works even
unauthenticated.

1. From the repository root, run:

   ```powershell
   python release_check.py --version 2026.3.0-rc1 --skip-prs
   ```

2. PR columns in the report are labeled as skipped, not zero.

### Common variations (both modes)

```powershell
# Different notes and image-tag versions
python release_check.py --version 2026.3.0 --image-version 2026.3.0-rc1 --skip-prs

# Limit scope
python release_check.py --version 2026.3.0-rc1 --category EAL
python release_check.py --version 2026.3.0-rc1 --project vector-retriever --project smart-nvr
```

Other flags: `--branch` (default `main`), `--catalog`, `--output`,
`--discover` (suggest untracked release-note paths),
`--validate-catalog` (offline catalog check, no network).

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

The skill ([SKILL.md](.github/skills/release-readiness/SKILL.md)) is discovered
automatically when this repository is open as the workspace root. It asks for
versions/scope, confirms any catalog edits, validates the catalog, runs the
CLI, and presents EAL/EAS tables, PR reconciliation, and report links.
Read-only towards GitHub and Docker Hub. For a full PR scan, set a REST API
token first (see [Authentication](#authentication)).

### Sample prompts

**With PR scan (full report):**

> /release-readiness
>
> run the release readiness report for 2026.3.0-rc1
>
> check release readiness for EAL only, notes and images at 2026.3.0-rc1

**Without PR scan (release notes + images only):**

> run release readiness without the PR scan — only release notes and image availability
>
> readiness-only check for 2026.3.0-rc1, skip open PRs
>
> are the 2026.3.0-rc1 images published and release notes updated? no PR report needed

The skill uses `--skip-prs` only after you accept a readiness-only report;
skipped PR columns are labeled as such, never shown as zero.

## Tests

```powershell
python -m unittest -v
```
