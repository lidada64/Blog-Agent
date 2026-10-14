"""Isolated CPU embedding worker; only explicit --download permits network."""
import argparse
import json
import os
import sys
from pathlib import Path

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    if not args.download:
        os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from fastembed import TextEmbedding
    encoder = TextEmbedding(model_name=MODEL, cache_dir=str(args.cache), threads=2,
                            local_files_only=not args.download)
    revision = Path(encoder.model._model_dir).name
    if args.download:
        probe = list(encoder.embed(["本地语义检索"]))
        manifest = {"model": MODEL, "dimension": len(probe[0]), "revision": revision,
                    "runtime": "fastembed-0.9.0", "ready": True}
        target = args.cache.parent / "embedding-manifest.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temporary.replace(target)
        print(json.dumps(manifest))
        return
    for line in sys.stdin:
        data = json.loads(line)
        if (not isinstance(data, dict) or set(data) != {"texts", "kind"}
                or data["kind"] not in {"query", "passage"}
                or not isinstance(data["texts"], list) or not 1 <= len(data["texts"]) <= 64
                or not all(isinstance(t, str) and 0 < len(t) <= 1500 for t in data["texts"])):
            raise ValueError("Invalid worker input")
        vectors = [v.tolist() for v in encoder.embed(
            data["texts"], batch_size=16)]
        print(json.dumps({"vectors": vectors, "model": MODEL, "dimension": 384, "revision": revision}), flush=True)


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    main()
