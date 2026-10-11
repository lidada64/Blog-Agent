"""Bounded Markdown reads from one explicitly selected local sample directory."""
import hashlib
from pathlib import Path

MAX_ARTICLE_BYTES = 128 * 1024
MAX_ARTICLES = 100
MAX_OUTPUT_CHARS = 6000

class SamplePosts:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("posts directory must be a directory")

    def _path(self, identifier):
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200
                or identifier in {".", ".."} or any(char in identifier for char in '/\\:\0')):
            raise ValueError("post_id must be a filename stem without path separators")
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
        return {**post, "start_line": start_line, "end_line": end, "text": "\n".join(selected),
                "next_line": end + 1 if end < len(lines) else None}

    def candidates(self):
        paths = []
        for path in self.directory.glob("*.md"):
            paths.append(path)
            if len(paths) > MAX_ARTICLES:
                raise ValueError("Select a sample directory with at most 100 articles")
        return sorted(paths)

    def find_file(self, name):
        """Literal case-insensitive filename substring; only safe Markdown files."""
        matches, issues = [], []
        for path in self.candidates():
            if name.casefold() not in path.name.casefold():
                continue
            try:
                self._path(path.stem)
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
                if not path.is_absolute() or path.parent.resolve() != self.directory or path.suffix.casefold() != ".md":
                    raise PermissionError("Only discovered files in the selected directory are readable")
                post = self.snapshot(path.stem)
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
                post = self.snapshot(path.stem)
                lines = post.pop("body").splitlines()
                matches = [(i, line) for i, line in enumerate(lines, 1)
                           if any(term in line.casefold() for term in terms)]
                if matches:
                    line_number, snippet = matches[0]
                    results.append({**post, "start_line": line_number, "end_line": line_number,
                                    "snippet": snippet[:500], "score": len(matches),
                                    "snippet_truncated": len(snippet) > 500})
            except (OSError, UnicodeError, ValueError, PermissionError) as exc:
                issues.append({"post_id": path.stem[:100], "error_type": type(exc).__name__})
        results.sort(key=lambda post: (-post["score"], post["post_id"]))
        # Total tool output stays bounded: up to five 500-char snippets plus metadata.
        return {"matches": results[:limit], "issues": issues[:5], "issues_count": len(issues)}
