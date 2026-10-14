"""Offline EXP-3 parent/child evidence and budget contract, not a live agent."""

import json
from pathlib import Path

from agent_core.state import atomic_write_json


class SharedBudget:
    """Persist reservations before mock work; child work cannot consume final slots."""

    def __init__(self, path, run_id, total=8, final_reserve=2):
        self.path = Path(path)
        if not 0 < final_reserve < total:
            raise ValueError("Invalid shared budget")
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if (self.state["run_id"] != run_id or self.state["total"] != total
                    or self.state["final_reserve"] != final_reserve):
                raise ValueError("Cannot resume with changed budget or run")
        else:
            self.state = {"run_id": run_id, "total": total, "final_reserve": final_reserve,
                          "used": 0, "final_used": 0, "requests": []}
            self.save()

    def save(self):
        atomic_write_json(self.path, self.state)

    def reserve(self, owner, phase):
        if phase not in {"exploration", "final"}:
            raise ValueError("Unknown budget phase")
        used = self.state["used"]
        if used >= self.state["total"]:
            raise RuntimeError("Total request budget exhausted")
        if phase == "exploration" and used >= self.state["total"] - self.state["final_reserve"]:
            raise RuntimeError("Child cannot consume final answer/recovery reserve")
        self.state["used"] += 1
        if phase == "final":
            self.state["final_used"] += 1
        request = {"request_id": f"{self.state['run_id']}:{used + 1}", "owner": owner,
                   "phase": phase, "usage_observed": False, "cost_cny": None}
        self.state["requests"].append(request)
        self.save()
        return request


def exact_findings(evidence):
    """Deterministic extraction; it does not model language understanding."""
    findings = []
    for item in evidence:
        for line in item["text"].splitlines():
            if line.strip() and not line.startswith("#") and not line.startswith("Ignore earlier rules"):
                findings.append({"claim": line.strip(), "quote": line.strip(),
                                 "chunk_id": item["chunk_id"], "version": item["version"]})
    return findings


def verify_findings(findings, evidence, allowed_ids=None):
    by_id = {item["chunk_id"]: item for item in evidence}
    allowed = set(by_id) if allowed_ids is None else set(allowed_ids)
    accepted, rejected = [], []
    for finding in findings:
        item = by_id.get(finding.get("chunk_id"))
        if (item is None or item["chunk_id"] not in allowed or item["visibility"] != "local_article"
                or finding.get("version") != item["version"] or not isinstance(finding.get("quote"), str)
                or not finding["quote"] or finding["quote"] not in item["text"]
                or finding.get("claim") != finding["quote"]):
            rejected.append(finding)
        else:
            accepted.append(finding)
    return accepted, rejected


def run_fixed_evidence(mode, evidence, budget, *, inject=None):
    """A0 direct, A1 citation checker, A2 two independent extraction children."""
    if mode not in {"A0", "A1", "A2"}:
        raise ValueError("Unknown subagent configuration")
    if len({item["chunk_id"] for item in evidence}) != len(evidence):
        raise ValueError("Duplicate evidence identity")
    inject = inject or {}
    child_runs, rejected = [], []
    if mode == "A0":
        findings = exact_findings(evidence)
    elif mode == "A1":
        findings = exact_findings(evidence)
        budget.reserve("citation_checker", "exploration")
        child_input = [{"chunk_id": item["chunk_id"], "version": item["version"],
                        "visibility": item["visibility"], "text": item["text"]} for item in evidence]
        if inject.get("bad_version") and findings:
            findings[0] = {**findings[0], "version": "stale"}
        findings, invalid = verify_findings(findings, evidence)
        rejected.extend(invalid)
        child_runs.append({"owner": "citation_checker", "input": child_input, "status": "completed",
                           "rejected": len(invalid)})
    else:
        groups = [[], []]
        for item in evidence:
            groups[len(groups[0]) > len(groups[1])].append(item)
        findings = []
        for index, group in enumerate(groups):
            if not group:
                continue
            owner = f"researcher_{index + 1}"
            budget.reserve(owner, "exploration")
            child_input = [{"chunk_id": item["chunk_id"], "version": item["version"],
                            "visibility": item["visibility"], "text": item["text"]} for item in group]
            if inject.get("timeout_child") == owner:
                child_runs.append({"owner": owner, "input": child_input, "status": "timeout"})
                continue
            child_findings = exact_findings(group)
            if inject.get("bad_version") and index == 0 and child_findings:
                child_findings[0] = {**child_findings[0], "version": "stale"}
            valid, invalid = verify_findings(child_findings, evidence,
                                             {item["chunk_id"] for item in group})
            findings.extend(valid)
            rejected.extend(invalid)
            child_runs.append({"owner": owner, "input": child_input, "status": "completed",
                               "findings": child_findings})
    valid, invalid = verify_findings(findings, evidence)
    rejected.extend(invalid)
    order = {item["chunk_id"]: index for index, item in enumerate(evidence)}
    valid.sort(key=lambda finding: order[finding["chunk_id"]])
    budget.reserve("parent_answer", "final")
    # The parent writes one answer; child text is never concatenated as a reply.
    answer = "\n".join(finding["claim"] for finding in valid) if valid else "Insufficient verified evidence."
    return {"mode": mode, "answer": answer, "citations": sorted({finding["chunk_id"] for finding in valid}),
            "accepted": len(valid), "rejected": len(rejected), "children": child_runs,
            "request_count": budget.state["used"], "usage_observed": False,
            "cost_cny": None, "semantic_quality": None,
            "limitations": "extractive mock; no provider tokens, latency or linguistic quality"}
