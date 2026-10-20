"""Shared contracts and real chapter metadata for budgeted article memory."""
from dataclasses import dataclass
from typing import Protocol

from .reference_sections import citation_sections, REFERENCE_VERSION

PARSER_VERSION = f"memory-tree-v2:{REFERENCE_VERSION}"
GENERATION_VERSION = "unconfigured-v1"


@dataclass(frozen=True)
class SummaryRequest:
    library_id: str
    article_id: str
    source_version: str
    node_id: str
    generation_version: str
    request_id: str
    text: str
    dependency_summaries: tuple[str, ...]


@dataclass(frozen=True)
class SummaryResult:
    text: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microunits: int | None


class SummaryGenerator(Protocol):
    """Stage C must reserve durable budget BEFORE invoking this interface.

    Results are untrusted drafts, never publishable solely because this returned.
    The caller owns dependency checks, source revalidation and worker fencing.
    """

    def generate(self, request: SummaryRequest) -> SummaryResult: ...


def index_nodes(article_id: str, version: str, body: str) -> list[dict]:
    """Reuse real citation IDs, storing metadata only (not a summary build DAG).

    The article range includes preamble/unheaded text; Stage C must explicitly
    cover those ranges and split large sections before scheduling model calls.
    """
    sections = citation_sections({"post_id": article_id, "version": version, "body": body})
    return [{"node_id": "article", "parent_id": None, "kind": "article",
             "title": sections[0]["title"] if sections else article_id,
             "start_line": 1, "end_line": len(body.splitlines()), "citation_id": None}] + [
        {"node_id": node["section_id"], "parent_id": node["parent_id"] or "article",
         "kind": "section", "title": node["title"], "start_line": node["start_line"],
         "end_line": node["end_line"], "citation_id": node["section_id"]} for node in sections]
