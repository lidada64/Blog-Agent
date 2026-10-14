"""Check model citation locations against this turn's actual article reads."""

import hashlib
import re


MARKER = re.compile(r"\[\[([^\]|\r\n]+)\|L([1-9]\d*)(?:-L([1-9]\d*))?\]\]")
MARKER_LIKE = re.compile(r"\[\[[^\]\r\n]*\]\]")
MAX_CITED_LINES = 8
CITATION_PROMPT_VERSION = "2026-10-13.10"
CITATION_AUDIT_VERSIONS = {"2026-10-13.1", "2026-10-13.2", "2026-10-13.3",
                           "2026-10-13.4", "2026-10-13.5", "2026-10-13.6", "2026-10-13.7", "2026-10-13.8", "2026-10-13.9", CITATION_PROMPT_VERSION}
FAIL_CLOSED_CITATION_VERSIONS = {"2026-10-13.4", "2026-10-13.5", "2026-10-13.6", "2026-10-13.7", "2026-10-13.8", "2026-10-13.9", CITATION_PROMPT_VERSION}


def read_lines(calls):
    """Index exact lines from completed, nonempty reads only."""
    rows = {}
    for call in calls:
        if call.get("status") != "completed" or call.get("name") not in {"read_post", "read_post_full"}:
            continue
        data = call.get("result", {}).get("data", {})
        if not data.get("text") or data.get("visibility") != "local_article":
            continue
        if call["name"] == "read_post_full" and not data.get("complete"):
            continue
        lines = data["text"].splitlines()
        if len(lines) != data["end_line"] - data["start_line"] + 1:
            continue
        key = (data["post_id"], data["version"])
        slot = rows.setdefault(key, {})
        slot.update({data["start_line"] + offset: line for offset, line in enumerate(lines)})
    return rows


def audit_answer(answer, calls, posts, *, fail_closed=False, bullet_only=False, max_visible_lines=None):
    """Structural check only; a human must still judge semantic support."""
    indexed = read_lines(calls)
    findings = []
    replacements = []
    marker_spans = []
    snapshots = {}
    for match in MARKER_LIKE.finditer(answer):
        marker = MARKER.fullmatch(match.group())
        finding = {"marker": match.group(), "status": "invalid_format", "post_id": None,
                   "start_line": None, "end_line": None, "version": None,
                   "evidence_sha256": None, "reason": "invalid_format"}
        if marker:
            post_id, start_text, end_text = marker.groups()
            start, end = int(start_text), int(end_text or start_text)
            finding.update(post_id=post_id, start_line=start, end_line=end)
            if end < start or end - start + 1 > MAX_CITED_LINES:
                finding["reason"] = "range_too_broad_or_reversed"
            else:
                if post_id not in snapshots:
                    try:
                        snapshots[post_id] = posts.snapshot(post_id)
                    except (OSError, UnicodeError, ValueError, PermissionError):
                        snapshots[post_id] = None
                current = snapshots[post_id]
                if current is None:
                    finding["reason"] = "source_unavailable"
                else:
                    matching = [(version, lines) for (article, version), lines in indexed.items()
                                if article == post_id and version == current["version"]]
                    if not matching:
                        finding["reason"] = "not_read_or_stale"
                    else:
                        version, lines = matching[-1]
                        if any(line not in lines for line in range(start, end + 1)):
                            finding["reason"] = "range_not_read"
                        else:
                            source_lines = current["body"].splitlines()
                            selected = source_lines[start - 1:end]
                            if selected != [lines[line] for line in range(start, end + 1)]:
                                finding["reason"] = "read_content_mismatch"
                            else:
                                finding.update(status="located", reason=None, version=version,
                                               evidence_sha256=hashlib.sha256("\n".join(selected).encode()).hexdigest())
        findings.append(finding)
        display = (f"【{finding['post_id']} L{finding['start_line']}-L{finding['end_line']} "
                   f"v{finding['version'][:12]}】" if finding["status"] == "located" else "【引用未核验】")
        replacements.append((match.span(), display))
        marker_spans.append((match.span(), finding["status"], display))
    rendered = answer
    for (start, end), display in reversed(replacements):
        rendered = rendered[:start] + display + rendered[end:]
    uncited_lines = []
    for number, line in enumerate(answer.splitlines(), 1):
        stripped = line.strip()
        if (not stripped or stripped.startswith("#") or stripped.startswith("```")
                or re.fullmatch(r"[-*_=|\s]+", stripped)):
            continue
        if not MARKER.search(line):
            uncited_lines.append(number)
    status = ("no_markers" if not findings else "invalid" if any(item["status"] != "located" for item in findings)
              else "uncited_lines" if uncited_lines else "located")
    omitted_lines = 0
    format_omitted_lines = 0
    visible_claim_lines = 0
    if fail_closed:
        kept = []
        offset = 0
        for raw_line in answer.splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            line_end = offset + len(line)
            matches = [(span, marker_status, display) for span, marker_status, display in marker_spans
                       if offset <= span[0] and span[1] <= line_end]
            # Every displayed claim line needs at least one valid marker. A
            # malformed or dangling marker invalidates the whole line.
            valid = (matches and all(marker_status == "located" for _, marker_status, _ in matches)
                     and line.count("[[") == len(matches) and line.count("]]") == len(matches)
                     and not line.lstrip().startswith("```"))
            format_ok = ((not bullet_only or line.lstrip().startswith(("- ", "* ")))
                         and (max_visible_lines is None or visible_claim_lines < max_visible_lines))
            if valid and format_ok:
                display_line = line
                for (start, end), _, display in reversed(matches):
                    display_line = display_line[:start - offset] + display + display_line[end - offset:]
                kept.append(display_line)
                visible_claim_lines += 1
            elif line.strip():
                omitted_lines += 1
                if valid and not format_ok:
                    format_omitted_lines += 1
            offset += len(raw_line)
        if kept:
            rendered = "\n".join(kept)
            if omitted_lines:
                rendered = f"已省略 {omitted_lines} 行引用未通过核验或未引用的内容。\n" + rendered
        else:
            rendered = ("本轮没有可定位到已读原文的断言，无法提供可靠回答。"
                        f"已省略 {omitted_lines} 行未经核验的内容。")
    return {"status": status, "citations": findings, "rendered": rendered,
            "located": sum(item["status"] == "located" for item in findings),
            "invalid": sum(item["status"] != "located" for item in findings),
            "uncited_lines": uncited_lines,
            "omitted_lines": omitted_lines, "format_omitted_lines": format_omitted_lines,
            "visible_claim_lines": visible_claim_lines,
            "semantic_support": None}
