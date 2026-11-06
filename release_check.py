"""Read-only local release readiness checks. Requires Python 3.11+."""

import argparse
import base64
import csv
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent


class CheckError(Exception):
    pass


class Client:
    def __init__(self, token=None):
        self.token = token
        self.cache = {}

    def get(self, url, missing_ok=False):
        if url in self.cache:
            return self.cache[url]
        headers = {"User-Agent": "local-release-checker", "Accept": "application/json"}
        if self.token and urllib.parse.urlparse(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=30
                ) as response:
                    result = json.load(response)
                self.cache[url] = result
                return result
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and missing_ok:
                    self.cache[url] = None
                    return None
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = exc.headers.get("Retry-After", "")
                    # Do not hammer an endpoint with a long requested cooldown.
                    if retry.isdigit() and int(retry) > 30:
                        raise CheckError(f"HTTP {exc.code}: cooldown {retry}s: {url}") from exc
                    time.sleep(int(retry) if retry.isdigit() else 2 ** (attempt + 1))
                    continue
                raise CheckError(f"HTTP {exc.code}: {url}") from exc
            except (OSError, ValueError) as exc:
                raise CheckError(f"{url}: {exc}") from exc
        raise CheckError(f"Retries exhausted: {url}")

    def pages(self, url):
        page = 1
        while True:
            items = self.get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}")
            if not isinstance(items, list):
                raise CheckError(f"Expected a paginated list: {url}")
            yield from items
            if len(items) < 100:
                return
            page += 1


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
        )
        if result.returncode:
            raise CheckError("gh authentication failed. Run 'gh auth login' or set GH_TOKEN.")
        return result.stdout.strip()
    return None


def notes_path(project):
    prefix = project["path"] + "/" if project["path"] else ""
    return project.get("release_notes", prefix + "docs/user-guide/release-notes.md")


def contains_version(text, version):
    return re.search(
        r"^#{1,6}\s+(?:Version\s+)?" + re.escape(version) + r"\s*#*\s*$",
        text, re.MULTILINE,
    ) is not None


def read_notes(client, project, branch):
    url = (
        f"https://api.github.com/repos/{project['repo']}/contents/"
        f"{urllib.parse.quote(notes_path(project), safe='/')}?ref="
        f"{urllib.parse.quote(branch, safe='')}"
    )
    data = client.get(url, missing_ok=True)
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        raise CheckError(f"Expected base64 file contents: {url}")
    try:
        return base64.b64decode(data["content"], validate=False).decode("utf-8")
    except (KeyError, ValueError, UnicodeError) as exc:
        raise CheckError(f"Invalid release notes response: {url}") from exc


def image_specs(project, version):
    for spec in project["images"]:
        if isinstance(spec, str):
            spec = {"repository": "intel/" + spec, "tag": "{version}"}
        if (not isinstance(spec, dict) or not isinstance(spec.get("repository"), str)
                or not isinstance(spec.get("tag", "{version}"), str)):
            raise ValueError(f"Invalid image specification: {spec}")
        yield spec["repository"], spec.get("tag", "{version}").replace("{version}", version)


def check_image(client, repository, tag):
    result = {
        "image": f"{repository}:{tag}",
        "url": f"https://hub.docker.com/r/{repository}/tags?name={urllib.parse.quote(tag)}",
    }
    try:
        data = client.get(
            f"https://hub.docker.com/v2/repositories/{repository}/tags/"
            f"{urllib.parse.quote(tag, safe='')}/", missing_ok=True,
        )
        if data is None:
            result.update(status="Not done", detail="Exact tag not publicly found")
        elif data.get("name") != tag or "images" not in data:
            raise CheckError("Unexpected Docker Hub tag response")
        elif data.get("tag_status") not in (None, "active") or not data["images"]:
            result.update(status="Not done", detail="Tag inactive or has no image manifests")
        else:
            result.update(status="Done", detail=data.get("last_updated", ""))
    except CheckError as exc:
        result.update(status="Check failed", detail=str(exc))
    return result


def image_status(images):
    if any(i["status"] == "Check failed" for i in images):
        return "Check failed"
    done = sum(i["status"] == "Done" for i in images)
    return "Done" if done == len(images) else "Partial" if done else "Not done"


PR_CATEGORIES = ("release-note", "dependency", "other")


def version_headings(text):
    headings = re.findall(
        r"^#{1,6}\s+(?:Version\s+([\w][\w.-]*)|([vV]?\d[\w.-]*))\s*#*\s*$",
        text or "", re.MULTILINE,
    )
    return [explicit or numeric for explicit, numeric in headings]


def dependency_change(pr, files):
    title = pr["title"].lower()
    labels = {label["name"].lower() for label in pr.get("labels", [])}
    author = pr.get("user") or {}
    if (author.get("login", "").lower().startswith(("dependabot", "renovate"))
            or labels & {"dependencies", "dependency", "dependabot"}
            or re.search(r"\b(bump|dependabot|dependency|dependencies|vuln fix)\b", title)):
        return True
    for file in files:
        name = file["filename"].rsplit("/", 1)[-1].lower()
        if (name in {"package.json", "package-lock.json", "npm-shrinkwrap.json",
                     "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "poetry.lock",
                     "uv.lock", "pipfile", "pipfile.lock", "go.mod", "go.sum",
                     "cargo.toml", "cargo.lock"}
                or re.fullmatch(r"requirements(?:[-_.][\w.-]+)?\.txt", name)):
            return True
    return False


def project_touches(project, files):
    paths = project.get("pr_paths", [project["path"]])
    return any(
        filename == notes_path(project)
        or any(not path or filename == path or filename.startswith(path + "/")
               for path in paths)
        for file in files
        for filename in (file["filename"], file.get("previous_filename", file["filename"]))
    )


def pr_age_days(created_at, checked_at):
    created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created.tzinfo is None or created > checked_at:
        raise ValueError("creation time must be timezone-aware and not in the future")
    return round((checked_at - created).total_seconds() / 86400, 2)


def scan_inventory(client, repo, projects, version, checked_at=None):
    """Inventory every open PR, including PRs outside the selected project scope."""
    entries, errors, pulls = [], [], []
    list_complete = True
    try:
        for pr in client.pages(f"https://api.github.com/repos/{repo}/pulls?state=open"):
            pulls.append(pr)
    except CheckError as exc:
        list_complete = False
        errors.append(str(exc))
    checked_at = checked_at or dt.datetime.now(dt.timezone.utc)
    cooldown = None
    seen = set()
    for pr in pulls:
        if pr["number"] in seen:
            continue
        seen.add(pr["number"])
        entry = {
            "repo": repo, "number": pr["number"], "url": pr["html_url"],
            "title": pr["title"], "author": (pr.get("user") or {}).get("login", "Unknown"),
            "created_at": pr.get("created_at"), "age_days": None,
            "base_branch": pr.get("base", {}).get("ref"),
            "projects": [], "notes_projects": [], "notes_versions": {},
            "requested_version": version,
            "target_version_projects": [], "category": "unknown",
            "mapping_status": "Incomplete", "files": [], "errors": [],
        }
        try:
            entry["age_days"] = pr_age_days(entry["created_at"], checked_at)
        except (AttributeError, TypeError, ValueError) as exc:
            entry["errors"].append(f"Invalid PR creation time: {exc}")
        files = []
        try:
            if cooldown:
                raise CheckError(f"File scan not attempted after rate limit: {cooldown}")
            for file in client.pages(
                f"https://api.github.com/repos/{repo}/pulls/{pr['number']}/files"
            ):
                files.append(file)
            if len(files) >= 3000 or len(files) < pr.get("changed_files", len(files)):
                raise CheckError("PR file listing may be truncated (GitHub limit: 3,000 files)")
            entry["mapping_status"] = "Complete"
        except CheckError as exc:
            entry["errors"].append(str(exc))
            if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                cooldown = str(exc)
        entry["files"] = [file["filename"] for file in files]
        entry["projects"] = [p["name"] for p in projects if project_touches(p, files)]
        release_files = {
            file["filename"] for file in files
            if file["filename"].rsplit("/", 1)[-1] == "release-notes.md"
            or any(file["filename"] == notes_path(p) for p in projects)
        }
        if entry["mapping_status"] == "Complete":
            entry["category"] = (
                "release-note" if release_files else
                "dependency" if dependency_change(pr, files) else "other"
            )
        for project in projects:
            if notes_path(project) not in release_files:
                continue
            entry["notes_projects"].append(project["name"])
            try:
                file = next(f for f in files if f["filename"] == notes_path(project))
                if file.get("status") == "removed":
                    entry["notes_versions"][project["name"]] = []
                    continue
                head_repo = pr["head"].get("repo")
                if not head_repo:
                    raise CheckError("PR head repository is unavailable")
                text = read_notes(client, dict(project, repo=head_repo["full_name"]),
                                  pr["head"]["sha"])
                if text is None:
                    raise CheckError("Changed release-note head file not publicly found")
                entry["notes_versions"][project["name"]] = version_headings(text)
                if contains_version(text, version):
                    entry["target_version_projects"].append(project["name"])
            except CheckError as exc:
                entry["errors"].append(str(exc))
        errors.extend(f"{repo} #{pr['number']}: {error}" for error in entry["errors"])
        entries.append(entry)
    return {"repo": repo, "list_complete": list_complete,
            "status": "Incomplete" if errors else "Complete", "prs": entries}, errors


def scan_prs(client, repo, projects, version, branch):
    # Compatibility for callers needing only exact-version release-note matches.
    inventory, errors = scan_inventory(client, repo, projects, version,
                                       dt.datetime.now(dt.timezone.utc))
    return {p["name"]: [pr for pr in inventory["prs"]
                       if p["name"] in pr["target_version_projects"]
                       and pr["base_branch"] in (None, branch)]
            for p in projects}, errors


def reconcile(inventories, skipped=False):
    repositories = []
    for inventory in inventories:
        prs = inventory["prs"]
        mapped = [pr for pr in prs if pr["mapping_status"] == "Complete" and pr["projects"]]
        outside = [pr for pr in prs
                   if pr["mapping_status"] == "Complete" and not pr["projects"]]
        unknown = [pr for pr in prs if pr["mapping_status"] != "Complete"]
        repositories.append({
            "repo": inventory["repo"], "status": inventory["status"],
            "list_complete": inventory["list_complete"], "total_open": len(prs),
            "mapped": len(mapped), "out_of_scope": len(outside), "unclassified": len(unknown),
            "categories": {category: sum(p["category"] == category for p in mapped)
                           for category in PR_CATEGORIES},
            "out_of_scope_prs": outside, "unclassified_prs": unknown,
        })
    totals = {key: sum(r[key] for r in repositories)
              for key in ("total_open", "mapped", "out_of_scope", "unclassified")}
    totals["categories"] = {
        category: sum(r["categories"][category] for r in repositories)
        for category in PR_CATEGORIES
    }
    return {
        "status": "Skipped" if skipped else
        "Incomplete" if any(r["status"] != "Complete" for r in repositories) else "Complete",
        "repositories": repositories, "totals": totals,
    }


def load_catalog(path):
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("Catalog needs a nonempty projects list")
    names = set()
    for p in projects:
        if not isinstance(p, dict):
            raise ValueError("Each catalog project must be an object")
        for key in ("name", "category", "repo", "path", "images"):
            if key not in p:
                raise ValueError(f"Project missing {key}: {p}")
        for key in ("name", "category", "repo", "path"):
            if not isinstance(p[key], str) or (key != "path" and not p[key].strip()):
                raise ValueError(f"Project {key} must be a string: {p}")
        if p["name"] in names or p["category"] not in ("EAL", "EAS"):
            raise ValueError(f"Duplicate name or invalid category: {p['name']}")
        names.add(p["name"])
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", p["repo"]):
            raise ValueError(f"Invalid repository: {p['repo']}")
        paths = [p["path"], notes_path(p)]
        if "pr_paths" in p:
            if not isinstance(p["pr_paths"], list) or not p["pr_paths"]:
                raise ValueError(f"pr_paths must be a nonempty list: {p['name']}")
            paths.extend(p["pr_paths"])
        for relative in paths:
            if (not isinstance(relative, str) or relative.startswith("/")
                    or "\\" in relative or any(part in (".", "..") for part in relative.split("/"))
                    or (relative and relative.endswith("/"))):
                raise ValueError(f"Invalid repository-relative path: {relative}")
        if p.get("owner") is not None and (
                not isinstance(p["owner"], str) or not p["owner"].strip()):
            raise ValueError(f"Owner must be a nonempty string or null: {p['name']}")
        if not isinstance(p["images"], list) or not p["images"]:
            raise ValueError(f"Images must be a nonempty list: {p['name']}")
        for repository, tag in image_specs(p, "test"):
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
                raise ValueError(f"Invalid Docker Hub repository: {repository}")
            if not re.fullmatch(r"[\w][\w.-]{0,127}", tag):
                raise ValueError(f"Invalid Docker image tag: {tag}")
    return projects


def discover(client, projects, branch):
    candidates, errors = [], []
    for repo in sorted({p["repo"] for p in projects}):
        try:
            tree = client.get(
                f"https://api.github.com/repos/{repo}/git/trees/"
                f"{urllib.parse.quote(branch, safe='')}?recursive=1"
            )
            if tree.get("truncated"):
                errors.append(f"Discovery tree truncated: {repo}; results are incomplete")
            known = {notes_path(p) for p in projects if p["repo"] == repo}
            candidates.extend(
                {"repo": repo, "release_notes": item["path"]}
                for item in tree["tree"]
                if item["type"] == "blob"
                and item["path"].endswith("/docs/user-guide/release-notes.md")
                and item["path"] not in known
            )
        except CheckError as exc:
            errors.append(str(exc))
    return candidates, errors


def run_checks(client, projects, version, image_version, branch, skip_prs=False,
               inventories=None, checked_at=None):
    inventories = inventories if inventories is not None else []
    rows, errors = [], []
    for p in projects:
        print(f"Checking {p['name']}...", flush=True)
        row = {
            "project": p["name"], "category": p["category"],
            "owner": p.get("owner") or "Pending",
            "repo_url": f"https://github.com/{p['repo']}/tree/{urllib.parse.quote(branch, safe='')}/{p['path']}",
            "notes_url": f"https://github.com/{p['repo']}/blob/{urllib.parse.quote(branch, safe='')}/{notes_path(p)}",
            "mapping_note": p.get("mapping_note", ""), "prs": [],
            "release_note_prs": [], "dependency_prs": [], "other_prs": [],
            "unclassified_prs": [], "notes_versions": [], "notes_file_found": None,
        }
        try:
            text = read_notes(client, p, branch)
            row["notes_status"] = "Done" if text and contains_version(text, version) else "Not done"
            headings = version_headings(text)
            row["notes_versions"] = headings
            row["notes_file_found"] = text is not None
            row["notes_detail"] = (
                "File not publicly found" if text is None else
                f"First version: {headings[0]}" if headings else "No version headings found"
            )
        except CheckError as exc:
            row.update(notes_status="Check failed", notes_detail=str(exc))
        row["images"] = [check_image(client, repo, tag) for repo, tag in image_specs(p, image_version)]
        row["images_status"] = image_status(row["images"])
        row["pr_scan"] = "Skipped"
        rows.append(row)
    if not skip_prs:
        for repo in sorted({p["repo"] for p in projects}):
            repo_projects = [p for p in projects if p["repo"] == repo]
            print(f"Scanning all open PRs in {repo}...", flush=True)
            inventory, failures = scan_inventory(client, repo, repo_projects, version, checked_at)
            inventories.append(inventory)
            errors.extend(failures)
            for row in rows:
                if row["project"] not in {p["name"] for p in repo_projects}:
                    continue
                matches = [pr for pr in inventory["prs"] if row["project"] in pr["projects"]]
                for category, key in (("release-note", "release_note_prs"),
                                      ("dependency", "dependency_prs"),
                                      ("other", "other_prs"), ("unknown", "unclassified_prs")):
                    row[key] = [pr for pr in matches if pr["category"] == category]
                row["prs"] = [pr for pr in matches
                              if row["project"] in pr["target_version_projects"]
                              and pr["base_branch"] in (None, branch)]
                row["pr_scan"] = inventory["status"]
    return rows, errors


def signature(row):
    return {
        key: row[key] for key in ("notes_status", "images_status", "pr_scan")
    } | {
        "images": [(i["image"], i["status"]) for i in row["images"]],
        "prs": sorted(p["url"] for p in row["prs"]),
        "owner": row.get("owner"),
        "notes_detail": row.get("notes_detail"),
        "pr_categories": {key: sorted(p["url"] for p in row.get(key, []))
                          for key in ("release_note_prs", "dependency_prs",
                                      "other_prs", "unclassified_prs")},
    }


def pr_label(pr, project=None, age=True):
    label = f"#{pr['number']} {pr['title']}"
    if age:
        label += f" - {pr['age_days']:.2f} days" if pr.get("age_days") is not None else " - age unknown"
    if project and project in pr.get("notes_projects", []):
        versions = pr["notes_versions"].get(project)
        label += " - notes: " + (
            ", ".join(versions) if versions else
            "no version headings" if versions == [] else "check failed"
        )
        if versions is not None and project not in pr.get("target_version_projects", []):
            label += f" (requested {pr['requested_version']} absent)"
    return label


def markdown_table(headers, rows):
    def cell(value):
        return str(value).replace("\r", " ").replace("\n", "<br>").replace("|", "\\|")
    return "\n".join(
        ["| " + " | ".join(cell(h) for h in headers) + " |",
         "| " + " | ".join("---" for _ in headers) + " |"]
        + ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    )


def notes_display(row, version):
    if row["notes_status"] == "Check failed":
        return "Check failed: " + row["notes_detail"]
    versions = row.get("notes_versions", [])
    if row["notes_status"] == "Done":
        return version
    if row.get("notes_file_found") is False or row["notes_detail"] == "File not publicly found":
        return "File not found at catalog path"
    latest = versions[0] if versions else (
        row["notes_detail"].removeprefix("First version: ")
        if row["notes_detail"].startswith("First version: ") else None
    )
    return (latest or "No version headings found") + f" (requested {version} absent)"


def table_headers(branch):
    return ["Component - GitHub link", f"Release notes on {branch}", "Docker images",
            "Open release-note PR", "Other PRs - age", "Dependency PRs - age"]


def project_cells(row, report, link, esc):
    def prs(key, age=True):
        values = "<br>".join(link(p["url"], pr_label(p, row["project"], age))
                             for p in row.get(key, []))
        if row["pr_scan"] != "Complete":
            warning = "Not verified - scan " + row["pr_scan"].lower()
            return values + "<br>" + esc(warning) if values else esc(warning)
        return values or "-"

    component = link(row["repo_url"], row["project"])
    if row.get("mapping_note"):
        component += "<br>" + esc(row["mapping_note"])
    images = esc(row["images_status"]) + "<br>" + "<br>".join(
        link(image["url"], image["image"]) + " - " + esc(image["status"])
        + ("<br>" + esc(image["detail"]) if image["status"] != "Done" else "")
        for image in row["images"]
    )
    return [component, link(row["notes_url"], notes_display(row, report["version"])),
            images, prs("release_note_prs", False), prs("other_prs"), prs("dependency_prs")]


def display_tables(report):
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, label: f"[{esc(label).replace('[', '&#91;').replace(']', '&#93;')}]({url})"
    lines = [f"Release notes: {report['version']}; image tags: {report['image_version']}; "
             f"checked: {report['checked_at']}"]
    if "started_at" in report:
        lines.append("Scan started: " + report["started_at"])
    for category in ("EAL", "EAS"):
        rows = []
        for row in report["projects"]:
            if row["category"] != category:
                continue
            rows.append(project_cells(row, report, link, esc))
        if rows:
            lines.extend([f"\n## {category}", markdown_table(table_headers(report["branch"]), rows)])
    lines.extend(["\n## Project metadata", markdown_table(
        ["Component", "Owner", "PR scan", "Since previous run"],
        [[r["project"], r["owner"], r["pr_scan"], r.get("change", "Not compared")]
         for r in report["projects"]])])
    reconciliation = report.get("reconciliation", reconcile([], skipped=True))
    lines.extend(["\n## Open PR reconciliation - " + reconciliation["status"],
                  "Counts are unique by repository and PR number. Shared PRs appear in multiple "
                  "project rows but count once here. Out of scope is relative to selected projects.",
                  markdown_table(
                      ["Repository", "Release-note", "Other", "Dependency", "Mapped",
                       "Out of scope", "Unclassified", "All open", "Scan"],
                      [[r["repo"], r["categories"]["release-note"], r["categories"]["other"],
                        r["categories"]["dependency"], r["mapped"], r["out_of_scope"],
                        r["unclassified"], r["total_open"], r["status"]]
                       for r in reconciliation["repositories"]])])
    totals = reconciliation["totals"]
    if reconciliation["status"] == "Skipped":
        lines.append("PR inventory skipped; counts are unavailable, not zero.")
    else:
        lines.append(f"Unique totals: {totals['mapped']} mapped + {totals['out_of_scope']} "
                     f"out of scope + {totals['unclassified']} unclassified = "
                     f"{totals['total_open']} enumerated open PRs.")
        if reconciliation["status"] != "Complete":
            lines.append("INCOMPLETE: totals cover enumerated PRs only; do not treat as a complete inventory.")
    for r in reconciliation["repositories"]:
        for key, heading in (("out_of_scope_prs", "Out-of-scope PRs"),
                             ("unclassified_prs", "Unclassified PRs")):
            if r[key]:
                lines.append(f"\n### {r['repo']} - {heading}")
                lines.extend(f"- [{pr_label(p)}]({p['url']})" for p in r[key])
    if report["errors"]:
        lines.extend(["\nErrors:"] + ["- " + error for error in report["errors"]])
    return "\n".join(lines)


def write_reports(report, output):
    output.mkdir(parents=True, exist_ok=True)
    scope = (report["version"], report["image_version"], report["branch"],
             [r["project"] for r in report["projects"]])
    previous = None
    for path in sorted(output.glob("release-*.json"), reverse=True):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise CheckError(f"Cannot read prior report {path}: {exc}") from exc
        old_scope = (old.get("version"), old.get("image_version"), old.get("branch"),
                     [r["project"] for r in old.get("projects", [])])
        if old_scope == scope:
            previous = {r["project"]: r for r in old["projects"]}
            break
    for row in report["projects"]:
        old = previous.get(row["project"]) if previous else None
        row["change"] = (
            "No previous comparable run" if old is None else
            "Changed" if signature(old) != signature(row) else "Unchanged"
        )
        row["notes_on_branch"] = notes_display(row, report["version"])
    stem = "release-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    json_path, csv_path, html_path = [output / (stem + ext) for ext in (".json", ".csv", ".html")]
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    fields = ["project", "category", "owner", "repo_url", "notes_status", "notes_url",
              "notes_detail", "notes_on_branch", "pr_scan", "prs", "release_note_prs", "other_prs",
              "dependency_prs", "unclassified_prs", "images_status", "images", "change", "mapping_note"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in report["projects"]:
            flat = {key: row.get(key, "") for key in fields}
            flat["prs"] = "; ".join(p["url"] for p in row["prs"])
            for key in ("release_note_prs", "other_prs", "dependency_prs", "unclassified_prs"):
                flat[key] = "; ".join(
                    pr_label(p, row["project"], key != "release_note_prs") + " " + p["url"]
                    for p in row.get(key, [])
                )
            flat["images"] = "; ".join(
                f"{i['image']} [{i['status']}] {i['url']} {i['detail']}" for i in row["images"]
            )
            # Prevent spreadsheet formula execution from catalog/API text.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                             for k, v in flat.items()})
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, label: f'<a href="{esc(url)}">{esc(label)}</a>'
    tables = []
    for category in ("EAL", "EAS"):
        body = []
        for r in report["projects"]:
            if r["category"] != category:
                continue
            cells = project_cells(r, report, link, esc)
            body.append("<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>")
        if body:
            tables.append(f"<h2>{category}</h2><table><tr>"
                          + "".join("<th>" + esc(h) + "</th>"
                                    for h in table_headers(report["branch"])) + "</tr>"
                          + "".join(body) + "</table>")
    metadata = "<h2>Project metadata</h2><table><tr><th>Component</th><th>Owner</th>" \
               "<th>PR scan</th><th>Since previous run</th><th>Unclassified PRs</th></tr>"
    for row in report["projects"]:
        cells = [
            link(row["repo_url"], row["project"]), esc(row["owner"]), esc(row["pr_scan"]),
            esc(row["change"]),
            "<br>".join(link(p["url"], pr_label(p)) for p in row.get("unclassified_prs", [])),
        ]
        metadata += "<tr>" + "".join("<td>" + value + "</td>" for value in cells) + "</tr>"
    metadata += "</table>"
    reconciliation = report.get("reconciliation", reconcile([], skipped=True))
    summary_rows, inventory_sections = [], []
    for r in reconciliation["repositories"]:
        values = [r["repo"], r["categories"]["release-note"], r["categories"]["other"],
                  r["categories"]["dependency"], r["mapped"], r["out_of_scope"],
                  r["unclassified"], r["total_open"], r["status"]]
        summary_rows.append("<tr>" + "".join("<td>" + esc(v) + "</td>" for v in values) + "</tr>")
        for key, heading in (("out_of_scope_prs", "Out-of-scope PRs"),
                             ("unclassified_prs", "Unclassified PRs")):
            inventory_sections.append(f"<h3>{esc(r['repo'])}: {heading}</h3><ul>"
                                      + "".join("<li>" + link(p["url"], pr_label(p)) + "</li>"
                                                for p in r[key]) + "</ul>")
    totals = reconciliation["totals"]
    reconciliation_html = (
        f"<h2>Open PR reconciliation - {esc(reconciliation['status'])}</h2>"
        "<p>Unique by repository and PR number. Shared PRs count once. "
        "Out of scope is relative to selected projects. Incomplete counts include only enumerated PRs.</p>"
        + ("<p>Inventory skipped; counts are unavailable, not zero.</p>"
           if reconciliation["status"] == "Skipped" else
           f"<p>{totals['mapped']} mapped + {totals['out_of_scope']} out of scope + "
           f"{totals['unclassified']} unclassified = {totals['total_open']} enumerated open PRs.</p>")
        + "<table><tr><th>Repository</th><th>Release-note</th><th>Other</th><th>Dependency</th>"
        "<th>Mapped</th><th>Out of scope</th><th>Unclassified</th><th>All open</th><th>Scan</th></tr>"
        + "".join(summary_rows) + "</table>" + "".join(inventory_sections)
    )
    warnings = "".join("<li>" + esc(e) + "</li>" for e in report["errors"])
    discovery = "".join(
        "<li>" + esc(c["repo"] + ": " + c["release_notes"]) + "</li>"
        for c in report.get("discovery", [])
    )
    html_path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Release readiness</title>'
        "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top}"
        "th{background:#eee}small{color:#555}</style>"
        f"<h1>Release {esc(report['version'])}</h1>"
        f"<p>Image version: {esc(report['image_version'])}; branch: {esc(report['branch'])}; "
        f"checked: {esc(report['checked_at'])}; started: {esc(report.get('started_at', 'Unknown'))}"
        f"</p><ul>{warnings}</ul>"
        + "".join(tables) + metadata + reconciliation_html
        + "<h2>Untracked release-note paths (review manually)</h2><ul>"
        + discovery + "</ul></html>", encoding="utf-8",
    )
    return html_path, csv_path, json_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Exact release-notes version")
    parser.add_argument("--image-version", help="Defaults to --version")
    parser.add_argument("--category", choices=("EAL", "EAS"))
    parser.add_argument("--project", action="append", help="Exact project name; repeatable")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--catalog", type=Path, default=ROOT / "projects.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports")
    parser.add_argument("--skip-prs", action="store_true", help="Skip expensive open PR scanning")
    parser.add_argument("--discover", action="store_true", help="Find untracked release-note paths")
    parser.add_argument("--validate-catalog", action="store_true", help="Validate catalog without network checks")
    args = parser.parse_args(argv)
    try:
        for value in (args.version, args.image_version or args.version):
            if not re.fullmatch(r"[\w][\w.-]{0,127}", value):
                raise ValueError(f"Invalid release/image version: {value}")
        catalog = load_catalog(args.catalog)
        if args.validate_catalog:
            print(f"Catalog valid: {len(catalog)} projects")
            return 0
        unknown = set(args.project or []) - {p["name"] for p in catalog}
        if unknown:
            raise ValueError("Unknown projects: " + ", ".join(sorted(unknown)))
        selected = [p for p in catalog
                    if (not args.category or p["category"] == args.category)
                    and (not args.project or p["name"] in args.project)]
        if not selected:
            raise ValueError("No projects match the filters")
        token = github_token()
        if not token:
            print("Warning: GitHub is unauthenticated (60 requests/hour). Set GH_TOKEN for PR scans.",
                  file=sys.stderr)
        client = Client(token)
        started_at = dt.datetime.now(dt.timezone.utc)
        inventories = []
        rows, errors = run_checks(client, selected, args.version,
                                  args.image_version or args.version, args.branch, args.skip_prs,
                                  inventories)
        candidates = []
        if args.discover:
            candidates, discovery_errors = discover(client, catalog, args.branch)
            errors.extend(discovery_errors)
        checked_at = dt.datetime.now(dt.timezone.utc)
        for inventory in inventories:
            for pr in inventory["prs"]:
                if pr["age_days"] is not None:
                    pr["age_days"] = pr_age_days(pr["created_at"], checked_at)
        report = {
            "version": args.version, "image_version": args.image_version or args.version,
            "branch": args.branch, "checked_at": checked_at.isoformat(),
            "started_at": started_at.isoformat(),
            "projects": rows, "errors": errors, "discovery": candidates,
            "pr_inventory": inventories, "reconciliation": reconcile(inventories, args.skip_prs),
        }
        paths = write_reports(report, args.output)
        print(display_tables(report))
        for error in errors:
            print("Warning: " + error, file=sys.stderr)
        for path in paths:
            print(path.resolve())
        failed = errors or any(
            r["notes_status"] == "Check failed" or r["images_status"] == "Check failed" for r in rows
        )
        return 2 if failed else 0 if all(
            r["notes_status"] == "Done" and r["images_status"] == "Done" for r in rows
        ) else 1
    except (CheckError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
