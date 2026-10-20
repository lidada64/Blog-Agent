"""Bounded Markdown reads from one explicitly selected local sample directory."""
import base64
import hashlib
import json
import os
from pathlib import Path
from .sections import join_lines

MAX_ARTICLE_BYTES = 128 * 1024
MAX_ARTICLES = 100
MAX_OUTPUT_CHARS = 6000
MAX_DIRECTORIES = 256
MAX_SCAN_ENTRIES = 4096

class SamplePosts:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("posts directory must be a directory")

    def _path(self, identifier):
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200
                or any(char in identifier for char in '\\:\0')
                or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in identifier.split('/'))):
            raise ValueError("post_id must be a relative article path without .md or traversal segments")
        path = (self.directory / f"{identifier}.md").resolve(strict=True)
        if not path.is_relative_to(self.directory):
            raise PermissionError("Article resolves outside the selected directory")
        if not path.is_file():
            raise ValueError("Article is not a file")
        return path

    def snapshot(self, identifier):
        path = self._path(identifier)
        with path.open("rb") as stream:
            raw = stream.read(MAX_ARTICLE_BYTES + 1)
        if len(raw) > MAX_ARTICLE_BYTES:
            raise ValueError("Sample article exceeds 128 KiB")
        text = raw.decode("utf-8")
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), identifier)
        return {"post_id": identifier, "title": title[:200], "version": hashlib.sha256(raw).hexdigest(),
                "source": path.as_uri(), "visibility": "local_article", "body": text}

    def read(self, identifier, start_line, limit):
        post = self.snapshot(identifier)
        lines = post.pop("body").splitlines()
        if start_line > len(lines):
            raise ValueError("start_line is outside the article")
        selected, length = [], 0
        for line in lines[start_line - 1:start_line - 1 + limit]:
            if length + len(line) + 1 > MAX_OUTPUT_CHARS:
                if not selected:
                    raise ValueError("Single line exceeds output budget; use smaller source lines")
                break
            selected.append(line)
            length += len(line) + 1
        end = start_line + len(selected) - 1
        return {**post, "start_line": start_line, "end_line": end, "text": join_lines(selected),
                "next_line": end + 1 if end < len(lines) else None}

    def read_full(self, identifier):
        """Return an entire short article or an explicit segmented-read fallback."""
        post = self.snapshot(identifier)
        body = post.pop("body")
        lines = body.splitlines()
        if len(body) > MAX_OUTPUT_CHARS:
            return {**post, "start_line": 1, "end_line": 0, "total_lines": len(lines),
                    "text": "", "complete": False, "next_line": 1,
                    "incomplete_reason": "full_text_exceeds_output_budget; use read_post"}
        return {**post, "start_line": 1, "end_line": len(lines), "total_lines": len(lines),
                "text": body, "complete": True, "next_line": None}

    def candidates(self):
        paths, pending, directories, entries = [], [self.directory], 0, 0
        while pending:
            directory = pending.pop()
            directories += 1
            if directories > MAX_DIRECTORIES:
                raise ValueError("Selected knowledge root exceeds directory scan budget")
            with os.scandir(directory) as children:
                for entry in children:
                    entries += 1
                    if entries > MAX_SCAN_ENTRIES:
                        raise ValueError("Selected knowledge root exceeds entry scan budget")
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False) and path.resolve(strict=True) == path.absolute():
                        pending.append(path)
                        continue
                    if path.suffix.casefold() == ".md":
                        paths.append(path)
                        if len(paths) > MAX_ARTICLES:
                            raise ValueError("Select a knowledge root with at most 100 articles")
                    # Junctions and directory symlinks are not traversed, even
                    # when they point back into the root (cycles/duplicate scope).
        return sorted(paths)

    def identifier(self, path):
        return path.relative_to(self.directory).with_suffix("").as_posix()

    def catalog(self):
        """Version the bounded recursive scope, including unreadable articles."""
        items, issues, versions = [], [], []
        for path in self.candidates():
            try:
                post = self.snapshot(self.identifier(path))
                post.pop("body")
                items.append(post)
                versions.append([path.relative_to(self.directory).as_posix(), post["version"]])
            except (OSError, UnicodeError, ValueError, PermissionError) as exc:
                issues.append({"filename": path.relative_to(self.directory).as_posix()[:200], "error_type": type(exc).__name__})
                try:
                    stat = path.stat()
                    marker = [stat.st_size, stat.st_mtime_ns]
                except OSError:
                    marker = None
                versions.append([path.relative_to(self.directory).as_posix(), type(exc).__name__, marker])
        version = hashlib.sha256(json.dumps(
            [str(self.directory), versions], ensure_ascii=False).encode()).hexdigest()
        return items, issues, version

    def list_posts(self, limit, cursor=""):
        items, issues, version = self.catalog()
        offset = 0
        if cursor:
            try:
                page = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if (not isinstance(page, dict) or set(page) != {"snapshot", "offset"}
                        or type(page["offset"]) is not int or not 0 < page["offset"] < len(items)):
                    raise ValueError("Invalid catalog cursor")
            except (ValueError, UnicodeError, TypeError) as exc:
                raise ValueError("Invalid catalog cursor; restart with an empty cursor") from exc
            if page["snapshot"] != version:
                raise ValueError("Catalog changed or cursor belongs to another directory; restart listing")
            offset = page["offset"]
        selected = []
        for item in items[offset:offset + limit]:
            if len(json.dumps(selected + [item], ensure_ascii=False).encode()) > 12000:
                if not selected:
                    raise ValueError("Catalog metadata exceeds page budget")
                break
            selected.append(item)
        end = offset + len(selected)
        next_cursor = (base64.urlsafe_b64encode(json.dumps(
            {"snapshot": version, "offset": end}).encode()).decode() if end < len(items) else None)
        return {"items": selected, "total": len(items), "next_cursor": next_cursor,
                "snapshot_version": version, "issues": issues[:5], "issues_count": len(issues),
                "complete": next_cursor is None and not issues, "offset": offset}

    def find_file(self, name):
        """Literal case-insensitive filename substring; only safe Markdown files."""
        matches, issues = [], []
        for path in self.candidates():
            if name.casefold() not in path.relative_to(self.directory).as_posix().casefold():
                continue
            try:
                self._path(self.identifier(path))
                # Return listed path so aliases can be resolved and checked again on read.
                matches.append(str(path.absolute()))
            except (OSError, ValueError, PermissionError) as exc:
                issues.append({"filename": path.name[:200], "error_type": type(exc).__name__})
        return {"ok": True, "data": matches[:5], "truncated": len(matches) > 5,
                "issues": issues[:5], "issues_count": len(issues)}

    def find_text(self, paths, text):
        """Literal content matches with source, content version and exact line numbers."""
        matches, issues = [], []
        for value in paths:
            try:
                path = Path(value)
                if (not path.is_absolute() or not path.is_relative_to(self.directory)
                        or path.suffix.casefold() != ".md"):
                    raise PermissionError("Only discovered files in the selected directory are readable")
                post = self.snapshot(self.identifier(path))
                for line_number, line in enumerate(post.pop("body").splitlines(), 1):
                    if text.casefold() in line.casefold():
                        matches.append({**post, "path": str(path), "line": line_number,
                                        "snippet": line[:500], "snippet_truncated": len(line) > 500})
            except (OSError, UnicodeError, ValueError, PermissionError) as exc:
                issues.append({"filename": Path(value).name[:200], "error_type": type(exc).__name__})
        return {"ok": True, "data": matches[:5], "truncated": len(matches) > 5,
                "issues": issues[:5], "issues_count": len(issues)}

    def search(self, query, limit):
        terms = query.casefold().split()
        candidates = self.candidates()
        results, issues = [], []
        for path in sorted(candidates):
            try:
                post = self.snapshot(self.identifier(path))
                lines = post.pop("body").splitlines()
                matches = [(i, line) for i, line in enumerate(lines, 1)
                           if any(term in line.casefold() for term in terms)]
                if matches:
                    line_number, snippet = matches[0]
                    results.append({**post, "start_line": line_number, "end_line": line_number,
                                    "snippet": snippet[:500], "score": len(matches),
                                    "snippet_truncated": len(snippet) > 500})
            except (OSError, UnicodeError, ValueError, PermissionError) as exc:
                issues.append({"post_id": self.identifier(path)[:200], "error_type": type(exc).__name__})
        results.sort(key=lambda post: (-post["score"], post["post_id"]))
        # Total tool output stays bounded: up to five 500-char snippets plus metadata.
        return {"matches": results[:limit], "issues": issues[:5], "issues_count": len(issues)}
