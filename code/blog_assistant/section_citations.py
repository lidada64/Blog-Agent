"""Validate article/section references independently of Markdown layout."""
import hashlib
import re

from .citations import MARKER, audit_answer, read_lines
from .sections import sections

PROMPT_VERSION = "2026-10-13.13"
SECTION_PROMPT_VERSIONS = {"2026-10-13.11", "2026-10-13.12", PROMPT_VERSION}
REFERENCE = re.compile(r"\[\[([^\]|\r\n]+)(?:\|([^\]\r\n]+))?\]\]")


def escape_label(text):
    return re.sub(r"([\\`*_{}\[\]<>()!#|])", r"\\\1", text)


def audit_sections(answer, calls, posts):
    indexed = read_lines(calls)
    findings, sources = [], set()
    malformed = answer.count("[[") != len(list(REFERENCE.finditer(answer)))
    source_label = "引用自：" if re.search(r"[\u3400-\u9fff]", REFERENCE.sub("", answer)) else "Source: "

    def replace(match):
        post_id, section = match.groups()
        finding = {"post_id": post_id, "section": section, "status": "invalid", "reason": "not_read_or_stale"}
        try:
            post = posts.snapshot(post_id)
            read = indexed.get((post_id, post["version"]), {})
            if not read:
                raise ValueError("not_read_or_stale")
            if MARKER.fullmatch(match[0]):
                legacy = audit_answer(match[0], calls, posts)
                if legacy["status"] != "located":
                    raise ValueError(legacy["citations"][0]["reason"])
                location = legacy["citations"][0]
                section = next((s["title"] for s in reversed(sections(post["body"]))
                                if s["start_line"] <= location["start_line"]), None)
                selected = range(location["start_line"], location["end_line"] + 1)
            elif section is not None:
                ranges = [s for s in sections(post["body"]) if s["title"] == section]
                selected = [n for s in ranges for n in range(s["start_line"], s["end_line"] + 1)
                            if n in read and read[n].strip() and not read[n].lstrip().startswith("#")]
                if not selected:
                    raise ValueError("section_not_read")
            else:
                selected = [n for n, line in read.items() if line.strip() and not line.lstrip().startswith("#")]
                if not selected:
                    raise ValueError("no_body_evidence")
            current = post["body"].splitlines()
            if any(read[n] != current[n - 1] for n in selected):
                raise ValueError("read_content_mismatch")
            finding.update(status="located", reason=None, section=section, title=post["title"],
                           version=post["version"], source=post["source"], lines=list(selected),
                           evidence_sha256=hashlib.sha256("\n".join(read[n] for n in selected).encode()).hexdigest())
            key = (post_id, section)
            # Repeated references still need a readable label at their position.
            # Only omit the repeated prefix; deleting markers leaves empty lists.
            prefix = "" if key in sources else source_label
            sources.add(key)
            display_section = section if section != post["title"] else None
            return (prefix + "*" + escape_label(post["title"]) + "*" +
                    (" **" + escape_label(display_section) + "**" if display_section else ""))
        except (OSError, UnicodeError, ValueError, PermissionError) as exc:
            finding["reason"] = str(exc) if isinstance(exc, ValueError) else "source_unavailable"
            return ""
        finally:
            findings.append(finding)

    rendered = REFERENCE.sub(replace, answer)
    status = "invalid" if malformed or any(f["status"] != "located" for f in findings) else "located" if findings else "no_markers"
    # Keep formatting intact on success. Invalid source declarations invalidate
    # the draft as a whole; never certify surrounding paragraphs by deleting lines.
    if status != "located":
        rendered = "本轮回答的文章或章节来源未通过核验，无法提供可靠回答。请明确文章或问题后重试。"
    return {"status": status, "citations": findings, "rendered": rendered,
            "located": sum(f["status"] == "located" for f in findings),
            "invalid": sum(f["status"] != "located" for f in findings), "semantic_support": None}
