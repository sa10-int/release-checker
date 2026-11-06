---
name: release-readiness
description: 'Run the local EAL/EAS release readiness report. Use when checking release notes, Docker image publication, open PR ages, dependency PRs, release-note PRs, reconciliation totals, new release projects, or project owner updates.'
argument-hint: 'Release-notes version and image-tag version'
---

# Release readiness

## Collect inputs

Use the question/form tool before running the report. Do not infer versions from
previous reports or silently reuse an earlier snapshot.

1. Ask for the exact release-notes version and the exact image-tag version as
   separate fields. Explicit versions supplied in the current request may be
   prefilled. Ask which scope to report: all EAL/EAS (default), EAL, EAS, or
   specific catalog projects.
2. Read `projects.json` at the workspace root. Show current project names,
   categories, and owners in original order; `null` means **Pending**, not a PR
   author. Ask whether any new projects should be added and whether any existing
   project needs an owner update.
3. For each new project collect its name, GitHub repository URL, EAL/EAS category,
   verified owner (or explicitly Pending), repository-relative project path,
   release-note path if nonstandard, and required Docker Hub image repositories
   and tag templates. Collect optional broader `pr_paths` for nested components.
   The repo must be stored as `owner/repository`, not a GitHub URL. A repository
   root project may use an empty path. Resolve unknown paths/images from the
   repository and ask the user to confirm; never invent them.
4. For owner changes ask for the existing project name and new verified owner.
   Do not derive ownership from commit authors or PR authors.
5. Confirm a concise summary of catalog changes before persisting them. Append
   new entries without reordering existing projects, preserve image variants and
   tag prefixes, and apply owner changes to the selected entries only. Use file
   edit tools to update the existing catalog, not a temporary replacement.

## Validate and execute

Run commands from the workspace root using its platform's path syntax.

```powershell
python release_check.py --version <notes-version> --image-version <image-version> --validate-catalog
python release_check.py --version <notes-version> --image-version <image-version>
```

Append `--category EAL`, `--category EAS`, or repeat `--project <name>` when requested.
Validate before network checks, including after any catalog edits.

Public repositories do not require authentication. Check whether `GH_TOKEN`,
`GITHUB_TOKEN`, or an authenticated `gh` is available without displaying any
token. If authentication is missing, explain the public API limit of 60 requests
per hour per outgoing IP and offer an unauthenticated attempt or authentication
through the user's normal secret-management process; never ask
them to paste a token into chat or save it in the catalog. An unauthenticated
attempt is allowed with the user's agreement, but incomplete scans must remain
explicit. Do not use `--skip-prs` for a requested full PR report unless the user
accepts a readiness-only report.

The script is read-only towards GitHub and Docker Hub. Do not push images,
create PRs, or merge PRs as part of this skill.

## Display results

Read the newly generated JSON/HTML report, not a previous run. Display separate
**EAL** and **EAS** tables, preserving catalog order within each group:

| Component - GitHub link | Release notes on main | Docker images | Open release-note PR | Other PRs - age | Dependency PRs - age |
| --- | --- | --- | --- | --- | --- |

- Use exactly these six columns, with separate EAL and EAS sections. Link each
  component name to its GitHub project directory. If a different branch was
  requested, replace "main" in the release-note column header with that branch.
- Put the requested notes/image versions and check times above the tables.
  Show the actual first version heading on the checked branch when the requested
  version is absent, e.g. `2026.3.0 (requested 2026.3.0-rc1 absent)`, rather than
  only "Not done". Show the requested version when found. Explicitly distinguish
  a missing release-note file, no version headings, and a failed check.
  Link the release-note text to the checked branch's release-note file.
- Link every required Docker image to its exact-tag-filtered Docker Hub page,
  including missing tags, shared images and prefixed Chat Q&A variants. Include
  each image's status and the aggregate status. A missing tag link is not proof
  of publication. Never relabel `2026.3.0` as RC1.
- Link PR numbers and include titles. Show age in days for other/dependency PRs.
  Release-note PR ages are retained in JSON but omitted from the table column.
- Show the actual head release-note versions; flag mismatches with the requested
  version. An open PR does not make main-branch release notes Done.
- Identify missing image variants, not just an aggregate Partial status.
- Categorization is mutually exclusive: release-note file changes first, then
  any detected dependency changes, then other. Mixed PRs follow that precedence;
  explain that dependency detection is heuristic.
- Report unique repository/PR-number counts: release-note + dependency + other
  = mapped; mapped + out-of-scope + unclassified = enumerated open PRs.
  Shared PRs may appear against multiple projects but count only once.
- Include the out-of-scope PR inventory, or link the HTML report containing it.
  Out of scope is relative to the selected catalog projects, not necessarily
  unrelated to the release. Do not claim every repository component is tracked.
- Include scan status/errors and snapshot start/completion times. Skipped or
  incomplete scans never mean zero open PRs; counts may be lower bounds.
  For incomplete scans, mark every PR cell as not fully verified, even if it
  contains some confirmed matches. Keep verified owners, scan status, changes
  since the prior run, and unclassified PRs in metadata outside the six-column
  tables. Do not remove catalog owner collection or reconciliation.
- Use the main CLI output as the report source. Do not silently substitute
  hardcoded session scripts or stale PR lists for failed live checks.
- Link the generated HTML/CSV/JSON deliverables. Return code 1 means incomplete
  release readiness; 2 means a check/configuration/report error, not success.

See the workspace `README.md` for CLI behavior and catalog fields.
