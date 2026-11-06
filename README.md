# Local release checker

Read-only checks for EAL/EAS release notes, all open PRs, and Docker Hub
image tags. Requires **Python 3.11+**; no third-party packages are needed.

## Run from PowerShell

```powershell
Set-Location C:\Users\sa10\Projects\release-checker
python release_check.py --version 2026.3.0-rc1
python release_check.py --version 2026.3.0-rc1 --category EAL
python release_check.py --version 2026.3.0-rc1 --project vector-retriever
```

GitHub authentication is recommended, especially for open PR scans. Use an existing
`gh auth login`, or set `GH_TOKEN` / `GITHUB_TOKEN` in the current shell using your
normal secret-management process. Do not put tokens in the catalog or source code.
The token needs read access to the repositories. Without authentication, GitHub's
public API limit is normally 60 requests per hour. This tool sends the token only
to `api.github.com`, never to Docker Hub.

Additional options:

```powershell
# Different documentation and image versions
python release_check.py --version 2026.3.0 --image-version 2026.3.0-rc1
# Fast check: omit potentially expensive open PR scanning
python release_check.py --version 2026.3.0-rc1 --skip-prs
# Suggest untracked project release-note paths (never edits the catalog)
python release_check.py --version 2026.3.0-rc1 --discover
```

`--branch` defaults to `main`. `--project` can be repeated.
`--catalog` and `--output` accept custom paths. Default catalog/report paths are
relative to the script directory, regardless of your current working directory.

## Results

Each run saves timestamped **HTML, CSV, and JSON** under `reports` and prints
separate EAL/EAS Markdown tables in the terminal.
Both Markdown and HTML use exactly six primary columns:
**Component - GitHub link | Release notes on main | Docker images |
Open release-note PR | Other PRs - age | Dependency PRs - age**.
The branch name in the notes header follows `--branch`.
Component names link to their GitHub directories; all required images link to
their exact-tag-filtered Docker Hub pages, including missing tags and variants.
The notes cell links to the checked file and shows the requested version when
present, otherwise the first version heading actually on that branch plus a
requested-version-absent warning. Missing files, missing headings, and check
failures are distinct. The exact readiness status remains unchanged in JSON.
Owner, scan status, and change history appear in a separate metadata section.
Incomplete PR scans are flagged in every PR cell, including cells with partial
matches. CSV retains its detailed columns and adds `notes_on_branch`; JSON
also records `notes_versions`, `notes_file_found`, and `notes_on_branch`.
Open HTML in a browser; CSV is Excel-compatible. JSON preserves detailed results.
Reports compare against the most recent report with the same versions, branch,
and ordered project selection. They do not overwrite previous runs.

| Status | Meaning |
|---|---|
| Done | Exact version heading on target branch / active image tag with manifests |
| Not done | Heading absent / file or exact image tag not publicly found |
| Partial | Some required images are published, others missing |
| Check failed | API, authentication, rate-limit, network, or response-format error |

Release notes require an exact version **heading**, not a mention in prose.
A merged change is considered done once that heading is on the target branch.
PR authors are displayed separately from verified owners.

Every open PR in each selected repository is scanned, across all base branches,
even when a project's release notes are already Done. Changed file paths (including
rename origins) map PRs to selected projects. Prefix matching respects directory
boundaries. PRs with changes outside those paths appear in the out-of-scope
inventory. An open release-note PR is displayed even if it proposes a different
version; its actual head headings are listed. The legacy JSON `prs` field retains
exact requested-version matches targeting `--branch`.

PR categories are mutually exclusive, with this precedence:

1. **Release-note**: changes a release-note file, even in a mixed-purpose PR.
2. **Dependency**: any detected dependency changes, including mixed-purpose PRs.
3. **Other**: all remaining fully scanned PRs.

Dependency detection is heuristic: Dependabot/Renovate author, dependency labels,
title keywords, or common package/lock/requirements files. It does not prove a
change is dependency-only, or detect every package version embedded in Dockerfiles.
Release-note categorization similarly identifies file changes, not only version bumps.
All categories retain titles so mixed-purpose changes remain visible.

The report includes separate release-note / other / dependency PR columns.
Age is elapsed time since PR creation, in days rounded to two decimals, measured
at the report's `checked_at` completion time; `started_at` records the start.
Release-note ages are retained in JSON but omitted from that display column.

HTML and JSON contain repository-wide reconciliation and individual out-of-scope
PRs; project CSV contains all three PR categories and ages. Counts deduplicate
by repository/PR number, although shared PRs appear in multiple project rows.
Release-note + dependency + other = unique mapped PRs.
Mapped + out-of-scope + unclassified = enumerated open PRs.
On rate limits, partial pagination, or truncated file lists, scans are **Incomplete**
and unclassifiable PRs remain explicit. Counts cover enumerated PRs only, not a
guaranteed full inventory. Skipped scans show counts as unavailable, not zero.

Exit codes: `0` all selected projects ready; `1` incomplete release;
`2` check/configuration/report error. Reports still include partial results when
individual network checks fail. A skipped PR scan is explicitly labeled.

## Maintain the catalog

`projects.json` contains the 21 initial projects in the supplied order.
Short image names expand to `intel/<name>:{version}`. Explicit image objects
handle prefixed tags, e.g.:

```json
{"repository": "intel/chatqna", "tag": "core_{version}"}
```

Each project supports:

- `name`, `category` (`EAL` / `EAS`), `repo`, `path`
- `owner`: verified team/person, initially `null` because owners are not confirmed
- `release_notes`: optional override of `<path>/docs/user-guide/release-notes.md`
- `pr_paths`: optional nonempty list of broader repository-relative directories
  for PR mapping; defaults to `path`. Use an empty path for a repository-root
  project. Do not include leading/trailing slashes or `..` components.
- `images`: nonempty list of required first-party images (not third-party databases)
- `mapping_note`: scope/mapping uncertainty shown in reports

Image lists are release-scope decisions. Review them when introducing/removing
variants. Vector Retriever includes all four backends; Live Video Search and VSS
include shared first-party dependencies for the default VDMS search deployment.
Alternative deployment stacks can require additional images: add them explicitly.
The security-guard mapping is provisional, based on the previously identified PR.

Discovery lists untracked canonical release-note paths in the two catalog
repositories, including components outside your release scope. Review suggestions
manually; projects with no release-note file cannot be discovered this way.

Docker checks cover publicly visible Docker Hub tags, not private repositories,
image contents, platform validation, signatures, or runtime correctness.
No images are pulled, pushed, or retagged; no PRs are created or merged.

## Interactive agent skill

The skill lives in [SKILL.md](.github/skills/release-readiness/SKILL.md) and is
discovered automatically by agents that support workspace skills (for example
GitHub Copilot in VS Code) when this folder is open as the workspace root.

### How to run it

1. **Open this repository as the workspace root** so the agent can find
   `.github/skills/release-readiness/SKILL.md`, `projects.json`, and
   `release_check.py`.
2. **Make GitHub authentication available before starting** (recommended):
   set `GH_TOKEN` or `GITHUB_TOKEN` through your normal secret-management
   process, or sign in with `gh auth login`. The token needs read access to the
   catalog repositories and must be a REST API token (classic PAT or
   fine-grained token). Git-transport-only OAuth credentials stored by Git
   Credential Manager are rejected by `api.github.com`. Never paste tokens into
   chat or store them in the catalog. Without authentication the public API
   allows ~60 requests/hour, which is not enough for a full PR scan.
3. **Invoke the skill** in agent chat, e.g. `/release-readiness`, or ask in
   natural language: "run the release readiness report".
4. **Answer the input form.** The skill always asks before running; it never
   reuses versions from a previous report. It collects:
   - the exact **release-notes version** (e.g. `2026.3.0-rc1`),
   - the exact **image-tag version** (may differ from the notes version),
   - the **scope**: all EAL/EAS (default), EAL only, EAS only, or specific
     projects,
   - any **catalog changes**: new projects (name, `owner/repository`, category,
     verified owner or Pending, paths, images/tag templates) or owner updates.
5. **Review catalog edits** if you requested any; the skill confirms a summary
   before persisting them to `projects.json`, then validates the catalog
   (`--validate-catalog`) before making any network requests.
6. **Read the results.** The skill runs the CLI and presents separate EAL/EAS
   six-column tables in catalog order, the deduplicated open-PR reconciliation,
   scan status and errors, and links to the timestamped HTML/CSV/JSON reports
   under `reports/`. Owners remain Pending until explicitly confirmed; the
   skill never derives ownership from PR or commit authors.

The skill is read-only towards GitHub and Docker Hub: it never pushes images,
creates PRs, or merges PRs. If authentication is missing it offers an explicit
choice between an unauthenticated (possibly incomplete) attempt and waiting for
a token; incomplete PR scans are always labeled as such, never reported as zero
open PRs. The Python CLI itself remains noninteractive for repeatable
local/scheduled use.

To validate catalog edits without making API requests:

```powershell
python release_check.py --version 2026.3.0 --validate-catalog
```

## Tests

```powershell
python -m unittest -v
```
