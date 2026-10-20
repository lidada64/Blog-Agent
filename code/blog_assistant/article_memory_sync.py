"""Complete-or-explicitly-partial inventory, independent of foreground read limits."""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .article_memory_build import index_nodes
from .retrieval import SamplePosts


@dataclass(frozen=True)
class ScanLimits:
    max_entries: int = 100_000
    max_article_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if self.max_entries < 1 or self.max_article_bytes < 1:
            raise ValueError("scan limits must be positive")


@dataclass(frozen=True)
class ArticleSnapshot:
    article_id: str
    version: str
    nodes: tuple[dict, ...]


@dataclass(frozen=True)
class ScanResult:
    library_id: str
    root: str
    scan_epoch: int
    articles: tuple[ArticleSnapshot, ...]
    issues: tuple[dict, ...]

    @property
    def complete(self) -> bool:
        return not self.issues


def stable_snapshot(posts: SamplePosts, article_id: str, limit: int) -> ArticleSnapshot:
    """Double content read catches same-size/mtime edits; fail closed on races.

    This is not a filesystem lock. Future publish/read paths MUST revalidate.
    """
    path = posts._path(article_id)
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("article_byte_limit")
    with posts._path(article_id).open("rb") as stream:
        again = stream.read(limit + 1)
    after = path.stat()
    if raw != again or (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("source_changed_during_scan")
    version = hashlib.sha256(raw).hexdigest()
    return ArticleSnapshot(article_id, version, tuple(index_nodes(article_id, version, raw.decode("utf-8"))))


def scan_library(library_id: str, root: str, scan_epoch: int,
                 limits: ScanLimits = ScanLimits()) -> ScanResult:
    articles, issues = [], []
    try:
        posts = SamplePosts(root)
    except (OSError, ValueError) as exc:
        return ScanResult(library_id, root, scan_epoch, (), ({"path": ".", "reason": type(exc).__name__},))
    pending, entries = [posts.directory], 0
    while pending:
        directory = pending.pop()
        try:
            # Revalidate directories on every traversal; never follow links/junctions.
            if directory.resolve(strict=True) != directory.absolute():
                raise ValueError("linked_directory")
            with os.scandir(directory) as children:
                for entry in children:
                    entries += 1
                    if entries > limits.max_entries:
                        issues.append({"path": ".", "reason": "entry_limit"})
                        return ScanResult(library_id, root, scan_epoch, tuple(articles), tuple(issues))
                    path = Path(entry.path)
                    relative = path.relative_to(posts.directory).as_posix()
                    try:
                        if entry.is_symlink() or path.resolve(strict=True) != path.absolute():
                            raise ValueError("linked_entry")
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        elif path.suffix.casefold() == ".md":
                            # Keep the existing canonical article identity/path policy.
                            if path.suffix != ".md":
                                raise ValueError("noncanonical_markdown_suffix")
                            articles.append(stable_snapshot(posts, posts.identifier(path), limits.max_article_bytes))
                    except (OSError, UnicodeError, ValueError) as exc:
                        issues.append({"path": relative, "reason": str(exc) if isinstance(exc, ValueError)
                                       and not isinstance(exc, UnicodeError) else type(exc).__name__})
        except (OSError, ValueError) as exc:
            issues.append({"path": directory.relative_to(posts.directory).as_posix(),
                           "reason": type(exc).__name__})
    return ScanResult(library_id, root, scan_epoch, tuple(articles), tuple(issues))


def synchronize(store, library_id: str, limits: ScanLimits = ScanLimits(), *, token=None) -> ScanResult:
    root, epoch = store.begin_scan(library_id, token)
    result = scan_library(library_id, root, epoch, limits)
    store.apply_scan(result, token)
    return result
