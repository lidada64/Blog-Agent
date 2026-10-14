"""Source-derived Markdown sections and bounded, exact line chunks."""
import hashlib
import re

CHUNK_VERSION = "markdown-lines-v3-350"


def join_lines(lines):
    """Round-trip splitlines(), including an explicitly selected final empty line.

    A single trailing newline terminates the preceding line; splitlines() does
    not count it as another empty line. Preserve that last empty source line by
    retaining its own terminator instead of changing the declared end_line.
    """
    return "\n".join(lines) + ("\n" if lines and lines[-1] == "" else "")


def sections(body):
    lines = body.splitlines()
    found, fence = [], None
    for number, line in enumerate(lines, 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            char = marker[1][0]
            if fence is None:
                fence = (char, len(marker[1]))
            elif char == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading and fence is None:
            found.append({"title": heading[1], "start_line": number})
    for index, item in enumerate(found):
        item["end_line"] = found[index + 1]["start_line"] - 1 if index + 1 < len(found) else len(lines)
    return found


def chunks(post, max_chars=350):
    """Keep exact lines; split long lines with explicit character coordinates."""
    lines = post["body"].splitlines()
    headings = sections(post["body"])
    starts = {s["start_line"]: s["title"] for s in headings}
    result, group, size, section, start = [], [], 0, None, 1

    def flush():
        nonlocal group, size
        if group and any(line.strip() and not re.match(r"^\s*#{1,6}\s", line) for line in group):
            text = "\n".join(group)
            end = start + len(group) - 1
            result.append({key: post[key] for key in ("post_id", "title", "source", "version", "visibility")}
                          | {"chunk_id": hashlib.sha256(f"{post['post_id']}:{post['version']}:{start}:{end}:{CHUNK_VERSION}".encode()).hexdigest(),
                             "start_line": start, "end_line": end, "section": section, "text": text})
        group, size = [], 0

    for number, line in enumerate(lines, 1):
        if number in starts:
            flush()
            section = starts[number]
        if group and size + len(line) + 1 > max_chars:
            flush()
        if len(line) > max_chars:
            flush()
            for offset in range(0, len(line), max_chars):
                text = line[offset:offset + max_chars]
                result.append({key: post[key] for key in ("post_id", "title", "source", "version", "visibility")}
                              | {"chunk_id": hashlib.sha256(f"{post['post_id']}:{post['version']}:{number}:{offset}:{CHUNK_VERSION}".encode()).hexdigest(),
                                 "start_line": number, "end_line": number, "section": section, "text": text,
                                 "line_fragment": True, "start_character": offset, "end_character": offset + len(text)})
            continue
        if not group:
            start = number
        group.append(line)
        size += len(line) + 1
    flush()
    return result
