"""Plan a ledger append with a character limit. Read-only; creates no files."""

import argparse
import json
import re
from datetime import date
from pathlib import Path


def size(text):
    return sum(not char.isspace() for char in text)


def plan(folder, incoming, limit=6000):
    if type(limit) is not int or limit < 1:
        raise ValueError("Limit must be a positive integer")
    folder = Path(folder)
    volumes = sorted((int(match.group(1)), path) for path in folder.glob("records-*.md")
                     if (match := re.fullmatch(r"records-(\d{3,})\.md", path.name)))
    if [n for n, _ in volumes] != list(range(1, len(volumes) + 1)):
        raise ValueError("Volume numbers must be unique and consecutive")
    incoming_size = size(incoming)
    if not incoming.strip():
        raise ValueError("Incoming record is empty")
    if volumes:
        number, latest = volumes[-1]
        existing = size(latest.read_text(encoding="utf-8"))
        if existing + incoming_size <= limit:
            return {"target": str(latest), "new_volume": False, "result_characters": existing + incoming_size,
                    "limit": limit, "header": None}
    else:
        number = 0
    next_number = number + 1
    template = (Path(__file__).resolve().parents[1] / "references/volume-template.md").read_text(encoding="utf-8")
    previous = "[总入口](README.md)" if not number else f"[上一卷](records-{number:03d}.md)"
    header = template.split("<!-- ENTRY_TEMPLATE -->")[0].format(
        volume=f"{next_number:03d}", date=date.today().isoformat(), previous=previous)
    total = size(header) + incoming_size
    if total > limit:
        raise ValueError("Record cannot fit a new volume; split at semantic sections with the same ID")
    return {"target": str(folder / f"records-{next_number:03d}.md"), "new_volume": True,
            "result_characters": total, "limit": limit, "header": header}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--incoming-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=6000)
    args = parser.parse_args()
    try:
        print(json.dumps(plan(args.ledger_dir, args.incoming_file.read_text(encoding="utf-8"), args.limit), ensure_ascii=True, indent=2))
    except (OSError, ValueError) as error:
        parser.error(str(error))
