"""Read challenge ratings from stdin and print a Markdown index; no file writes."""

import json
import sys


def level(score):
    return 1 if score <= 3 else 2 if score <= 6 else 3 if score <= 9 else 4


def rank(items):
    if not isinstance(items, list):
        raise ValueError("Input must be a JSON array")
    known, unknown, identifiers = [], [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "title", "status", "M", "F", "D", "U", "basis"}:
            raise ValueError("Invalid challenge fields")
        if not all(isinstance(item[k], str) and item[k].strip() for k in ("id", "title", "status", "basis")) or item["id"] in identifiers:
            raise ValueError("Missing text or duplicate challenge id")
        identifiers.add(item["id"])
        for key in ("M", "F", "D", "U"):
            value = item[key]
            if value is not None and (type(value) is not int or value not in ({0, 3} if key == "U" else {0, 1, 2, 3})):
                raise ValueError("Invalid rating")
        missing = sum(item[k] is None for k in ("M", "F", "D", "U"))
        minimum = sum(item[k] or 0 for k in ("M", "F", "D", "U"))
        maximum = minimum + 3 * missing
        floor = 4 if item["U"] == 3 else 1
        ceiling_floor = 4 if item["U"] in (None, 3) else 1
        record = {**item, "min_score": minimum, "max_score": maximum,
                  "min_level": max(floor, level(minimum)), "max_level": max(ceiling_floor, level(maximum))}
        (unknown if missing else known).append(record)
    key = lambda item: (item["min_level"], item["min_score"], item["id"])
    return sorted(known, key=key), sorted(unknown, key=key)


def clean(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown(items):
    known, unknown = rank(items)
    lines = ["| ID | 难点 | M/F/D/U | 总分 | 级别 | 状态 | 四维依据 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for record in known + unknown:
        scores = "/".join("?" if record[k] is None else str(record[k]) for k in ("M", "F", "D", "U"))
        total = str(record["min_score"]) if record["min_score"] == record["max_score"] else f"{record['min_score']}–{record['max_score']}"
        label = f"L{record['min_level']}" if record["min_level"] == record["max_level"] else f"L{record['min_level']}–L{record['max_level']}"
        if "?" in scores:
            label = "待评 " + label
        lines.append("| " + " | ".join(map(clean, (record["id"], record["title"], scores, total, label, record["status"], record["basis"]))) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        print(markdown(json.load(sys.stdin)))
    except (ValueError, TypeError) as error:
        print(f"Invalid ratings: {error}", file=sys.stderr)
        raise SystemExit(1)
