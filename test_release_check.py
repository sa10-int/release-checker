import base64
import contextlib
import csv
import datetime as dt
import io
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release_check as rc


class FakeClient:
    def __init__(self, value):
        self.value = value

    def get(self, url, missing_ok=False):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class ReleaseTests(unittest.TestCase):
    def test_exact_heading(self):
        self.assertTrue(rc.contains_version("## Version 2026.3.0-rc1\n", "2026.3.0-rc1"))
        for text in ("## Version 2026.3.0", "Example 2026.3.0-rc1",
                     "## Version 2026.3.0-rc10", "## Version 2026.3.0-rc1-extra"):
            self.assertFalse(rc.contains_version(text, "2026.3.0-rc1"))
        self.assertEqual(rc.version_headings(
            "## Version 2026.3.0\n### New\n## 2026.2.0\n### Improved\n"), ["2026.3.0", "2026.2.0"])

    def test_catalog_order_and_variants(self):
        projects = rc.load_catalog(rc.ROOT / "projects.json")
        self.assertEqual(len(projects), 21)
        self.assertEqual(sum(p["category"] == "EAL" for p in projects), 11)
        qna = next(p for p in projects if p["name"] == "chat-question-and-answer-core")
        self.assertEqual(list(rc.image_specs(qna, "2026.3.0-rc1")), [
            ("intel/chatqna", "core_2026.3.0-rc1"),
            ("intel/chatqna", "core_ollama_2026.3.0-rc1"),
            ("intel/chatqna-ui", "core_2026.3.0-rc1"),
        ])
        vector = next(p for p in projects if p["name"] == "vector-retriever")
        self.assertEqual(len(list(rc.image_specs(vector, "test"))), 4)

    def test_image_missing_vs_failure_and_manifest(self):
        self.assertEqual(rc.check_image(FakeClient(None), "intel/test", "v1")["status"], "Not done")
        self.assertEqual(rc.check_image(FakeClient(rc.CheckError("HTTP 403")),
                                        "intel/test", "v1")["status"], "Check failed")
        data = {"name": "v1", "images": [], "tag_status": "active"}
        self.assertEqual(rc.check_image(FakeClient(data), "intel/test", "v1")["status"], "Not done")
        data["images"] = [{"architecture": "amd64"}]
        self.assertEqual(rc.check_image(FakeClient(data), "intel/test", "v1")["status"], "Done")
        data["name"] = "v10"
        self.assertEqual(rc.check_image(FakeClient(data), "intel/test", "v1")["status"], "Check failed")

    def test_image_aggregation(self):
        self.assertEqual(rc.image_status([{"status": "Done"}, {"status": "Not done"}]), "Partial")
        self.assertEqual(rc.image_status([{"status": "Done"}, {"status": "Check failed"}]),
                         "Check failed")

    def test_pagination(self):
        client = rc.Client()
        with patch.object(client, "get", side_effect=[list(range(100)), [100]]) as get:
            self.assertEqual(len(list(client.pages("https://example.test/items?state=open"))), 101)
            self.assertIn("&per_page=100&page=2", get.call_args.args[0])

    def test_notes_decoding(self):
        data = {"encoding": "base64", "content": base64.b64encode(b"## Version v1").decode()}
        self.assertEqual(rc.read_notes(FakeClient(data), {"repo": "a/b", "path": "app"}, "main"),
                         "## Version v1")

    def test_pr_scans_head_and_matches_correct_file(self):
        project = {"name": "app", "repo": "a/b", "path": "app"}
        client = rc.Client()
        pr = {"number": 5, "html_url": "https://github.com/a/b/pull/5",
              "user": {"login": "author"}, "title": "Notes",
              "created_at": "2026-01-01T00:00:00Z",
              "head": {"sha": "abc", "repo": {"full_name": "fork/b"}}}
        file = {"filename": rc.notes_path(project), "status": "modified"}
        with patch.object(client, "pages", side_effect=[[pr], [file]]), \
                patch.object(rc, "read_notes", return_value="## Version v1") as read:
            matches, errors = rc.scan_prs(client, "a/b", [project], "v1", "main")
        self.assertFalse(errors)
        self.assertEqual(matches["app"][0]["number"], 5)
        self.assertEqual(read.call_args.args[1]["repo"], "fork/b")

    def test_pr_failure_is_explicit(self):
        client = rc.Client()
        with patch.object(client, "pages", side_effect=rc.CheckError("HTTP 403")):
            matches, errors = rc.scan_prs(client, "a/b", [{"name": "x", "path": "x"}],
                                          "v1", "main")
        self.assertEqual(matches, {"x": []})
        self.assertTrue(errors)

    def test_reports_escaping_csv_and_history(self):
        row = {
            "project": "<app>", "category": "EAL", "owner": "=formula",
            "repo_url": "https://github.com/a/b", "notes_status": "Done",
            "notes_url": "https://github.com/a/b/notes", "notes_detail": "<test>",
            "pr_scan": "Not needed", "prs": [], "images_status": "Done",
            "images": [{"image": "intel/app:v1", "status": "Done",
                        "url": "https://hub.docker.com/r/intel/app", "detail": ""}],
            "mapping_note": "",
        }
        report = {"version": "v1", "image_version": "v1", "branch": "main",
                  "checked_at": "now", "projects": [row], "errors": [], "discovery": []}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            html_path, csv_path, json_path = rc.write_reports(report, output)
            self.assertIn("&lt;app&gt;", html_path.read_text(encoding="utf-8"))
            self.assertIn("'=formula", csv_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(json.loads(json_path.read_text())["projects"][0]["change"],
                             "No previous comparable run")
            rc.write_reports(report, output)
            self.assertEqual(row["change"], "Unchanged")
            row["images"][0]["status"] = "Not done"
            row["images_status"] = "Not done"
            rc.write_reports(report, output)
            self.assertEqual(row["change"], "Changed")

    def test_main_generates_reports_with_mocked_network(self):
        def fake_get(client, url, missing_ok=False):
            if "hub.docker.com" in url:
                tag = url.rstrip("/").rsplit("/", 1)[-1]
                return {"name": tag, "images": [{"architecture": "amd64"}], "tag_status": "active"}
            return {"encoding": "base64",
                    "content": base64.b64encode(b"## Version 2026.3.0-rc1\n").decode()}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(rc.Client, "get", fake_get), \
                patch.object(rc, "github_token", return_value="fake"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = rc.main(["--version", "2026.3.0-rc1", "--skip-prs",
                              "--output", directory])
            self.assertEqual(result, 0)
            self.assertEqual(len(list(Path(directory).glob("*.html"))), 1)

    def make_pr(self, number, title="Functional change", **extra):
        return {
            "number": number, "title": title, "html_url": f"https://github.com/a/b/pull/{number}",
            "user": {"login": "developer"}, "labels": [],
            "created_at": "2026-11-01T12:00:00Z",
            "head": {"sha": "abc", "repo": {"full_name": "fork/b"}},
            "base": {"ref": "main"}, **extra,
        }

    def test_inventory_categories_shared_prs_and_reconciliation(self):
        projects = [{"name": "a", "path": "app/a"}, {"name": "b", "path": "app/b"}]
        pulls = [self.make_pr(n) for n in range(1, 5)]
        files = [
            [{"filename": rc.notes_path(projects[0]), "status": "modified"},
             {"filename": rc.notes_path(projects[1]), "status": "modified"},
             {"filename": "app/a/package.json", "status": "modified"}],
            [{"filename": "app/a/src.py"}, {"filename": "app/b/requirements.txt"}],
            [{"filename": "app/b/src.py"}],
            [{"filename": "untracked/readme.md"}],
        ]
        now = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)
        with patch.object(rc.Client, "pages", side_effect=[pulls, *files]), \
                patch.object(rc, "read_notes", return_value="## Version 2026.3.0\n"):
            inventory, errors = rc.scan_inventory(rc.Client(), "a/b", projects, "rc1", now)
        self.assertFalse(errors)
        self.assertEqual([p["category"] for p in inventory["prs"]],
                         ["release-note", "dependency", "other", "other"])
        pr = inventory["prs"][0]
        self.assertEqual(pr["projects"], ["a", "b"])
        self.assertEqual(pr["age_days"], 4.25)
        self.assertEqual(pr["notes_versions"]["a"], ["2026.3.0"])
        self.assertIn("requested rc1 absent", rc.pr_label(pr, "a", False))
        self.assertNotIn("days", rc.pr_label(pr, "a", False))
        totals = rc.reconcile([inventory])["totals"]
        self.assertEqual(totals, {
            "total_open": 4, "mapped": 3, "out_of_scope": 1, "unclassified": 0,
            "categories": {"release-note": 1, "dependency": 1, "other": 1},
        })

    def test_dependency_detection_and_directory_boundaries(self):
        for pr, files in [
            (self.make_pr(1, user={"login": "dependabot[bot]"}), []),
            (self.make_pr(1, labels=[{"name": "dependencies"}]), []),
            (self.make_pr(1, "Bump requests"), []),
            (self.make_pr(1), [{"filename": "app/requirements-dev.txt"}]),
            (self.make_pr(1), [{"filename": "app/uv.lock"}]),
        ]:
            self.assertTrue(rc.dependency_change(pr, files))
        self.assertFalse(rc.dependency_change(self.make_pr(1), [{"filename": "app/main.py"}]))
        project = {"name": "vector", "path": "services/vector/nested",
                   "pr_paths": ["services/vector"]}
        self.assertTrue(rc.project_touches(project, [{"filename": "services/vector/backend/a.py"}]))
        self.assertFalse(rc.project_touches(project, [{"filename": "services/vector-other/a.py"}]))
        self.assertTrue(rc.project_touches(project, [
            {"filename": "outside/a.py", "previous_filename": "services/vector/a.py"}]))
        self.assertTrue(rc.project_touches({"name": "root", "path": ""},
                                          [{"filename": "src/main.py"}]))

    def test_done_notes_are_scanned_and_all_base_branches_included(self):
        project = {"name": "app", "repo": "a/b", "category": "EAL", "path": "app",
                   "images": ["app"], "owner": "Team"}
        client, inventories = rc.Client(), []
        pr = self.make_pr(1, base={"ref": "release"})
        with patch.object(rc, "read_notes", return_value="## Version v1\n"), \
                patch.object(rc, "check_image", return_value={"status": "Done"}), \
                patch.object(client, "pages", side_effect=[
                    [pr], [{"filename": "app/main.py"}]]) as pages, \
                contextlib.redirect_stdout(io.StringIO()):
            rows, errors = rc.run_checks(client, [project], "v1", "image-v2", "main",
                                         inventories=inventories)
        self.assertFalse(errors)
        self.assertEqual(rows[0]["notes_status"], "Done")
        self.assertEqual(rows[0]["other_prs"][0]["number"], 1)
        self.assertEqual(rows[0]["pr_scan"], "Complete")
        self.assertNotIn("base=", pages.call_args_list[0].args[0])
        self.assertEqual(len(inventories), 1)

    def test_rate_limit_retains_all_enumerated_prs_as_unclassified(self):
        pulls = [self.make_pr(1), self.make_pr(2)]
        with patch.object(rc.Client, "pages", side_effect=[pulls, rc.CheckError("HTTP 403")]):
            inventory, errors = rc.scan_inventory(
                rc.Client(), "a/b", [{"name": "a", "path": "a"}], "v1",
                dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc))
        self.assertEqual(len(inventory["prs"]), 2)
        self.assertTrue(errors)
        summary = rc.reconcile([inventory])
        self.assertEqual(summary["status"], "Incomplete")
        self.assertEqual(summary["totals"]["unclassified"], 2)
        self.assertEqual(summary["totals"]["out_of_scope"], 0)

    def test_partial_pr_list_and_file_truncation(self):
        def partial_list(url):
            if "/pulls?" in url:
                yield self.make_pr(1, changed_files=2)
                raise rc.CheckError("HTTP 429")
            yield {"filename": "app/main.py"}
        with patch.object(rc.Client, "pages", side_effect=partial_list):
            inventory, errors = rc.scan_inventory(
                rc.Client(), "a/b", [{"name": "a", "path": "app"}], "v1",
                dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc))
        self.assertFalse(inventory["list_complete"])
        self.assertEqual(inventory["prs"][0]["mapping_status"], "Incomplete")
        self.assertTrue(any("truncated" in error for error in errors))
        self.assertEqual(rc.reconcile([inventory])["totals"]["unclassified"], 1)

    def test_pr_list_and_file_pagination_end_to_end(self):
        client = rc.Client()
        pulls = [self.make_pr(n) for n in range(1, 102)]
        files = [{"filename": f"outside/{n}.py"} for n in range(100)]
        def get(url, missing_ok=False):
            page = rc.urllib.parse.parse_qs(rc.urllib.parse.urlparse(url).query)["page"][0]
            if "/pulls?" in url:
                return pulls[:100] if page == "1" else pulls[100:]
            if "/pulls/1/files?" in url:
                return files if page == "1" else [{"filename": "app/src.py"}]
            return []
        with patch.object(client, "get", side_effect=get):
            inventory, errors = rc.scan_inventory(
                client, "a/b", [{"name": "a", "path": "app"}], "v1",
                dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc))
        self.assertFalse(errors)
        self.assertEqual(len(inventory["prs"]), 101)
        self.assertEqual(inventory["prs"][0]["projects"], ["a"])
        self.assertEqual(len(inventory["prs"][0]["files"]), 101)

    def test_deduplication_and_same_number_in_different_repositories(self):
        now = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
        with patch.object(rc.Client, "pages", side_effect=[
                [self.make_pr(1), self.make_pr(1)], [{"filename": "outside/a.py"}]]):
            first, errors = rc.scan_inventory(rc.Client(), "a/b", [], "v1", now)
        self.assertFalse(errors)
        self.assertEqual(len(first["prs"]), 1)
        with patch.object(rc.Client, "pages", side_effect=[
                [self.make_pr(1)], [{"filename": "outside/a.py"}]]):
            second, errors = rc.scan_inventory(rc.Client(), "c/d", [], "v1", now)
        self.assertFalse(errors)
        self.assertEqual(rc.reconcile([first, second])["totals"]["total_open"], 2)

    def test_invalid_age_and_deleted_or_failed_notes_are_explicit(self):
        project = {"name": "a", "path": "app"}
        pr = self.make_pr(1, created_at="invalid")
        with patch.object(rc.Client, "pages", side_effect=[
                [pr], [{"filename": rc.notes_path(project), "status": "removed"}]]):
            inventory, errors = rc.scan_inventory(
                rc.Client(), "a/b", [project], "v1", dt.datetime.now(dt.timezone.utc))
        self.assertIsNone(inventory["prs"][0]["age_days"])
        self.assertEqual(inventory["prs"][0]["notes_versions"]["a"], [])
        self.assertTrue(errors)
        with patch.object(rc.Client, "pages", side_effect=[
                [self.make_pr(1)], [{"filename": rc.notes_path(project), "status": "modified"}]]), \
                patch.object(rc, "read_notes", side_effect=rc.CheckError("head unavailable")):
            inventory, errors = rc.scan_inventory(
                rc.Client(), "a/b", [project], "v1", dt.datetime.now(dt.timezone.utc))
        self.assertEqual(inventory["prs"][0]["category"], "release-note")
        self.assertTrue(errors)
        self.assertIn("check failed", rc.pr_label(inventory["prs"][0], "a"))

    def test_new_catalog_and_owner_validation_without_network(self):
        project = {"name": "new", "category": "EAS", "repo": "new/repo", "path": "",
                   "owner": "Verified Team", "images": [{"repository": "intel/new", "tag": "{version}"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps({"projects": [project]}))
            with patch.object(rc.Client, "get") as get, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rc.main(["--version", "v1", "--catalog", str(path),
                                          "--validate-catalog"]), 0)
                get.assert_not_called()
            self.assertEqual(rc.load_catalog(path)[0]["owner"], "Verified Team")
            for field, value in (("pr_paths", ["../outside"]), ("owner", ""),
                                 ("category", "OTHER"), ("pr_paths", [])):
                path.write_text(json.dumps({"projects": [dict(project, **{field: value})]}))
                with self.assertRaises(ValueError):
                    rc.load_catalog(path)

    def test_full_cli_reports_categories_reconciliation_and_separate_versions(self):
        catalog = {"projects": [
            {"name": "app", "category": "EAL", "repo": "a/b", "path": "app",
             "owner": "Team", "images": ["app"]},
            {"name": "suite", "category": "EAS", "repo": "c/d", "path": "suite",
             "owner": None, "images": ["suite"]},
        ]}
        def get(client, url, missing_ok=False):
            if "hub.docker.com" in url:
                self.assertIn("/tags/image-v2/", url)
                return {"name": "image-v2", "images": [{"architecture": "amd64"}]}
            if "/pulls?" in url:
                return [self.make_pr(1, "<Notes>", created_at="2026-01-01T00:00:00Z"),
                        self.make_pr(2, "=Bump", created_at="2026-01-01T00:00:00Z"),
                        self.make_pr(3, "Outside", created_at="2026-01-01T00:00:00Z")] if "/a/b/" in url else []
            if "/pulls/1/files?" in url:
                return [{"filename": "app/docs/user-guide/release-notes.md", "status": "modified"}]
            if "/pulls/2/files?" in url:
                return [{"filename": "app/package.json"}]
            if "/pulls/3/files?" in url:
                return [{"filename": "outside/src.py"}]
            text = "## Version other\n" if "fork/b" in url else "## Version v1\n"
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(catalog))
            output = Path(directory) / "reports"
            with patch.object(rc.Client, "get", get), \
                    patch.object(rc, "github_token", return_value="fake"), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = rc.main(["--version", "v1", "--image-version", "image-v2",
                                "--catalog", str(path), "--output", str(output)])
            self.assertEqual(code, 0)
            report = json.loads(next(output.glob("*.json")).read_text())
            self.assertEqual(report["reconciliation"]["totals"]["total_open"], 3)
            self.assertEqual(report["reconciliation"]["totals"]["out_of_scope"], 1)
            self.assertEqual(report["projects"][0]["release_note_prs"][0]["number"], 1)
            self.assertEqual(report["projects"][0]["dependency_prs"][0]["number"], 2)
            html_text = next(output.glob("*.html")).read_text(encoding="utf-8")
            for text in ("<h2>EAL</h2>", "<h2>EAS</h2>", "Dependency PRs - age",
                         "requested v1 absent", "&lt;Notes&gt;", "Out-of-scope PRs"):
                self.assertIn(text, html_text)
            self.assertIn("## EAL", stdout.getvalue())
            self.assertIn("## EAS", stdout.getvalue())
            with next(output.glob("*.csv")).open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertIn("days", rows[0]["dependency_prs"])
            self.assertIn("requested v1 absent", rows[0]["release_note_prs"])
            self.assertNotIn("days", rows[0]["release_note_prs"])

    def test_skill_frontmatter_and_workflow(self):
        text = (rc.ROOT / ".github" / "skills" / "release-readiness" / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: release-readiness\n"))
        frontmatter = text.split("---", 2)[1]
        self.assertIn("description: '", frontmatter)
        self.assertIn("--image-version", text)
        self.assertIn("--validate-catalog", text)
        self.assertIn("owner", text)
        self.assertIn("| Component - GitHub link | Release notes on main | Docker images |", text)

    def test_actual_notes_display_preserves_readiness_status(self):
        project = {"name": "app", "repo": "a/b", "category": "EAL", "path": "app",
                   "images": ["app"]}
        for text, expected, status in (
            ("## Version 2026.3.0\n", "2026.3.0 (requested rc1 absent)", "Not done"),
            ("## 2026.2.0\n", "2026.2.0 (requested rc1 absent)", "Not done"),
            ("## Version rc1\n", "rc1", "Done"),
            ("# Release Notes\n", "No version headings found (requested rc1 absent)", "Not done"),
            (None, "File not found at catalog path", "Not done"),
        ):
            with self.subTest(text=text), \
                    patch.object(rc, "read_notes", return_value=text), \
                    patch.object(rc, "check_image", return_value={"status": "Done"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                rows, _ = rc.run_checks(rc.Client(), [project], "rc1", "rc1", "main", True)
            self.assertEqual(rc.notes_display(rows[0], "rc1"), expected)
            self.assertEqual(rows[0]["notes_status"], status)
            self.assertEqual(rows[0]["notes_file_found"], text is not None)
        with patch.object(rc, "read_notes", side_effect=rc.CheckError("HTTP 403")), \
                patch.object(rc, "check_image", return_value={"status": "Done"}), \
                contextlib.redirect_stdout(io.StringIO()):
            rows, _ = rc.run_checks(rc.Client(), [project], "rc1", "rc1", "main", True)
        self.assertEqual(rc.notes_display(rows[0], "rc1"), "Check failed: HTTP 403")
        self.assertIsNone(rows[0]["notes_file_found"])
        legacy = {"notes_status": "Not done", "notes_detail": "First version: 2026.3.0"}
        self.assertEqual(rc.notes_display(legacy, "rc1"), "2026.3.0 (requested rc1 absent)")

    def test_exact_six_column_tables_links_and_partial_scan_warnings(self):
        class Tables(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tables = []
                self.current = None
                self.in_header = False
                self.links = []

            def handle_starttag(self, tag, attrs):
                if tag == "table":
                    self.current = []
                    self.tables.append(self.current)
                elif tag == "th":
                    self.in_header = True
                    self.current.append("")
                elif tag == "a":
                    self.links.append(dict(attrs)["href"])

            def handle_endtag(self, tag):
                if tag == "th":
                    self.in_header = False

            def handle_data(self, data):
                if self.in_header:
                    self.current[-1] += data

        image_url = "https://hub.docker.com/r/intel/chatqna/tags?name=core_rc1"
        pr = self.make_pr(4)
        pr.update(url=pr["html_url"], age_days=1.25)
        row = {
            "project": "app", "category": "EAL", "owner": "Verified Team",
            "repo_url": "https://github.com/a/b/tree/release/app",
            "notes_url": "https://github.com/a/b/blob/release/app/docs/user-guide/release-notes.md",
            "notes_status": "Not done", "notes_detail": "First version: v1",
            "notes_versions": ["v1"], "notes_file_found": True,
            "images_status": "Partial", "images": [
                {"image": "intel/chatqna:core_rc1", "url": image_url,
                 "status": "Done", "detail": ""},
                {"image": "intel/missing:rc1", "url": "https://hub.docker.com/r/intel/missing/tags?name=rc1",
                 "status": "Not done", "detail": "Exact tag not publicly found"}],
            "pr_scan": "Incomplete", "prs": [], "other_prs": [pr], "mapping_note": "",
        }
        report = {"version": "rc1", "image_version": "rc1", "branch": "release",
                  "checked_at": "now", "projects": [row, dict(row, project="suite", category="EAS")],
                  "errors": ["HTTP 403"], "discovery": []}
        markdown = rc.display_tables(report)
        headers = "| " + " | ".join(rc.table_headers("release")) + " |"
        self.assertEqual(markdown.count(headers), 2)
        self.assertIn(f"[intel/chatqna:core_rc1]({image_url})", markdown)
        self.assertIn("v1 (requested rc1 absent)", markdown)
        self.assertIn("Not verified - scan incomplete", markdown)
        self.assertIn("#4 Functional change - 1.25 days", markdown)
        self.assertIn("Verified Team", markdown)
        with tempfile.TemporaryDirectory() as directory:
            html_path, csv_path, json_path = rc.write_reports(report, Path(directory))
            parser = Tables()
            parser.feed(html_path.read_text(encoding="utf-8"))
            self.assertEqual(parser.tables[:2], [rc.table_headers("release")] * 2)
            for url in (row["repo_url"], row["notes_url"], image_url, row["images"][1]["url"]):
                self.assertIn(url, parser.links)
            with csv_path.open(encoding="utf-8-sig", newline="") as file:
                csv_rows = list(csv.DictReader(file))
            self.assertEqual(csv_rows[0]["notes_on_branch"], "v1 (requested rc1 absent)")
            self.assertEqual(json.loads(json_path.read_text())["projects"][0]["notes_status"], "Not done")
            row["notes_detail"] = "First version: v2"
            row["notes_versions"] = ["v2"]
            rc.write_reports(report, Path(directory))
            self.assertEqual(row["change"], "Changed")


if __name__ == "__main__":
    unittest.main()
