"""Run bounded, user-authorized Blog QA trials against DeepSeek Responses API."""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_core.metrics import field, usage_metrics
from agent_core.state import atomic_write_json
from blog_assistant.convergence import RetrievalLimits
from blog_assistant.session import BlogStore, create_conversation


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / ".blog-agent-data" / "experiments"
PRICE = {"source": "https://api-docs.deepseek.com/quick_start/pricing/",
         "checked_at": "2026-10-13", "model": "deepseek-flash",
         "peak_usd_per_million_input_miss": 0.3,
         "peak_usd_per_million_input_hit": 0.006,
         "peak_usd_per_million_output": 1.2,
         "cny_per_usd_reference": 6.7313,
         "fx_source": "https://www.boc.cn/english/bocinfo/exr/index.html",
         "guard_cny_per_usd": 8.0}


def peak_cny(input_tokens, output_tokens, cached_tokens=0, *, exchange=8.0):
    miss = input_tokens - cached_tokens
    if min(input_tokens, output_tokens, cached_tokens, miss) < 0:
        raise ValueError("Invalid usage counters")
    usd = (miss * PRICE["peak_usd_per_million_input_miss"]
           + cached_tokens * PRICE["peak_usd_per_million_input_hit"]
           + output_tokens * PRICE["peak_usd_per_million_output"]) / 1_000_000
    return usd * exchange


class BudgetExhausted(RuntimeError):
    pass


class CostGuard:
    """Reserve before network I/O; unknown responses retain their reservation."""
    def __init__(self, path, limit_cny, *, safety_margin_cny=1.0):
        if not 0 < safety_margin_cny < limit_cny:
            raise ValueError("Safety margin must be positive and below the spending cap")
        self.path = Path(path)
        self.safety_margin_cny = safety_margin_cny
        self.ledger = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                       else {"limit_cny": limit_cny, "used_cny_upper": 0.0, "requests": []})
        if self.ledger["limit_cny"] != limit_cny:
            raise ValueError("Saved spending limit differs; do not reset the budget ledger")

    def write(self):
        atomic_write_json(self.path, self.ledger)

    async def create(self, raw_responses, **kwargs):
        kwargs.setdefault("max_output_tokens", 1024)  # Introduction has no retrieval policy.
        serialized = json.dumps({key: kwargs.get(key) for key in ("instructions", "input", "tools", "reasoning")},
                                ensure_ascii=False, default=str).encode("utf-8")
        # Conservative admission estimate. Exact tokenization and server billing
        # are observed from usage afterward; an unobserved request keeps this charge.
        input_ceiling = 2 * len(serialized) + 2048
        output_ceiling = kwargs["max_output_tokens"]
        ceiling = peak_cny(input_ceiling, output_ceiling)
        if self.ledger["used_cny_upper"] + ceiling > self.ledger["limit_cny"] - self.safety_margin_cny:
            raise BudgetExhausted(
                f"Conservative request estimate would exceed the {self.ledger['limit_cny']:g} CNY cap "
                f"with {self.safety_margin_cny:g} CNY margin")
        record = {"at": datetime.now(timezone.utc).isoformat(), "status": "reserved",
                  "input_ceiling_tokens": input_ceiling, "output_ceiling_tokens": output_ceiling,
                  "charged_cny_upper": ceiling, "safety_margin_cny": self.safety_margin_cny}
        self.ledger["requests"].append(record)
        self.ledger["used_cny_upper"] += ceiling
        self.write()
        try:
            response = await raw_responses.create(**kwargs)
        except BaseException as exc:
            record.update(status="unobserved_error", error_type=type(exc).__name__)
            self.write()
            raise
        usage = usage_metrics(response)
        if usage["input_tokens"] is not None and usage["output_tokens"] is not None:
            charged = peak_cny(usage["input_tokens"], usage["output_tokens"],
                               usage["cached_tokens"] or 0)
            record.update(status="usage_observed", input_tokens=usage["input_tokens"],
                          output_tokens=usage["output_tokens"], cached_tokens=usage["cached_tokens"],
                          charged_cny_upper=max(ceiling, charged) if charged > ceiling else charged)
            self.ledger["used_cny_upper"] += record["charged_cny_upper"] - ceiling
        else:
            record["status"] = "usage_missing"
        record["response_status"] = field(response, "status")
        self.write()
        return response


async def run(args):
    output = (OUTPUT_ROOT / args.experiment_id).resolve()
    if not output.is_relative_to(OUTPUT_ROOT.resolve()) or output.exists():
        raise ValueError("Experiment id must name a new directory inside the ignored experiment root")
    cases = json.loads(args.case_file.read_text(encoding="utf-8"))["cases"]
    if not cases or any(not isinstance(case.get("id"), str) or not isinstance(case.get("prompt"), str)
                        for case in cases):
        raise ValueError("Case file needs id/prompt pairs")
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    load_dotenv(ROOT / "code" / ".env")
    if os.getenv("OPENAI_BASE_URL", "").rstrip("/") != "https://api.deepseek.com":
        raise ValueError("This price guard applies only to the official DeepSeek endpoint")
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Missing local DeepSeek API key")
    output.mkdir(parents=True)
    guard = CostGuard(OUTPUT_ROOT / "deepseek-20261012-budget.json", args.max_spend_cny,
                      safety_margin_cny=args.safety_margin_cny)
    manifest = {"experiment_id": args.experiment_id, "provider": "DeepSeek", "model": "deepseek-flash",
                "article_scope": "all local Blog articles authorized by user",
                "case_file_sha256": hashlib.sha256(args.case_file.read_bytes()).hexdigest(),
                "case_ids": [case["id"] for case in cases], "efforts": args.efforts,
                "repetitions": args.repetitions, "max_steps": args.max_steps,
                "max_output_tokens": args.max_output_tokens, "limit_cny": args.max_spend_cny,
                "safety_margin_cny": args.safety_margin_cny,
                "prices": PRICE, "status": "running"}
    atomic_write_json(output / "manifest.json", manifest)
    rows = []
    async with AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"],
                           max_retries=0, timeout=90) as raw:
        client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kwargs: guard.create(raw.responses, **kwargs)))
        for effort in args.efforts:
            for repeat in range(1, args.repetitions + 1):
                for case in cases:
                    run_id = f"{case['id']}-{effort}-{repeat}"
                    store = BlogStore(output / f"{run_id}.json", posts_dir=args.posts_dir)
                    state = store.start("deepseek-flash", new=True, compact_mode="none",
                                        reasoning_effort=effort)
                    conversation = create_conversation(client, state, store, max_steps=args.max_steps,
                        retrieval_limits=RetrievalLimits(model_requests=args.max_steps,
                                                         output_tokens=args.max_output_tokens))
                    before = len(guard.ledger["requests"])
                    try:
                        await conversation.introduce()
                        if state["turns"][-1]["status"] != "completed":
                            status, reply = state["status"], ""
                            error_type = (state.get("last_error") or {}).get("type")
                        else:
                            status, reply = await conversation.submit(case["prompt"])
                            error_type = (state.get("last_error") or {}).get("type")
                    except (BudgetExhausted, Exception) as exc:
                        status, reply = "error", ""
                        error_type = type(exc).__name__
                    requests = guard.ledger["requests"][before:]
                    row = {"run_id": run_id, "case_id": case["id"], "effort": effort,
                           "repetition": repeat, "status": status, "error_type": error_type,
                           "reply": reply, "result": state["turns"][-1].get("result") if state["turns"] else None,
                           "tool_names": [call["name"] for call in state["tool_calls"]],
                           "model_requests": len(requests),
                           "peak_cny_estimate_upper": sum(item["charged_cny_upper"] for item in requests),
                           "usage_complete": all(item["status"] == "usage_observed" for item in requests),
                           "input_tokens": sum(item.get("input_tokens", 0) for item in requests),
                           "output_tokens": sum(item.get("output_tokens", 0) for item in requests),
                           "cached_tokens": sum(item.get("cached_tokens") or 0 for item in requests)}
                    rows.append(row)
                    (output / "runs.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                                                             for item in rows), encoding="utf-8")
                    print(json.dumps({key: row[key] for key in ("run_id", "status", "error_type", "result",
                                                                "tool_names", "model_requests", "peak_cny_estimate_upper")},
                                     ensure_ascii=False), flush=True)
                    if error_type:
                        manifest["status"] = "stopped_on_error"
                        atomic_write_json(output / "manifest.json", manifest)
                        return
    manifest["status"] = "completed"
    manifest["used_cny_upper"] = guard.ledger["used_cny_upper"]
    atomic_write_json(output / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--case-file", type=Path, required=True)
    parser.add_argument("--posts-dir", type=Path, default=ROOT / "Blog")
    parser.add_argument("--efforts", nargs="+", choices=("none", "low", "high", "max"), default=["low"])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-spend-cny", type=float, default=5.0)
    parser.add_argument("--safety-margin-cny", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    args = parser.parse_args()
    if min(args.repetitions, args.max_spend_cny, args.safety_margin_cny,
           args.max_steps, args.max_output_tokens) <= 0 or args.safety_margin_cny >= args.max_spend_cny:
        parser.error("All budgets and repetitions must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
