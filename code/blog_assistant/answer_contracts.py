"""Application-owned answer evidence types; no model can authorize a bypass."""
import ast
import operator
import re
from decimal import Decimal, localcontext
from fractions import Fraction

VERSION = "2026-10-13.13"
KINDS = {"article", "runtime", "calculation", "conversation", "rewrite", "clarify"}
EVIDENCE = {"article": {"article_text"}, "runtime": {"runtime"}, "calculation": {"local_calculation"},
            "conversation": {"conversation_metadata"}, "rewrite": {"verified_reply_and_sources", "conversation_metadata"},
            "clarify": {"runtime", "verified_reply_and_sources"}}
REFERENCE_TO_REPLY = re.compile(r"(?:之前|前面|刚才|刚刚|上面|上一轮|上一次).{0,8}(?:回答|回复|解释)|\b(?:previous|earlier|last|above) (?:answer|response|explanation)\b", re.I)
REWRITE = re.compile(r"总结|概括|简述|简短|压缩|改写|缩短|一句话|\b(?:summari[sz]e|shorten|rewrite|one sentence|brief)\b", re.I)
ARTICLE_SCOPE = re.compile(r"文章|原文|博客|资料库|文件|\b(?:article|post|library|file)\b", re.I)
RUNTIME = re.compile(r"你(?:们)?(?:是|能|可以|会|有|支持).{0,15}(?:谁|什么|干嘛|功能|能力)|你是谁|怎么使用你|如何使用你|介绍.{0,3}你自己|\b(?:who are you|what (?:can|do) you do|your capabilities|how do i use you)\b", re.I)


def enabled(state):
    return state.get("prompt", {}).get("version") in {VERSION, "2026-10-15.14"}


def arithmetic(text):
    expression = text.strip().rstrip("？?。!").strip()
    expression = re.sub(r"^(?:(?:请(?:帮我)?|帮我)?(?:计算(?:一下)?|算一下)|请问|请告诉我|告诉我|what is|calculate)\s*", "", expression, flags=re.I)
    expression = re.sub(r"(?:等于(?:多少|几)?|是多少|是什么|=)\s*$", "", expression).strip()
    expression = expression.replace("×", "*").replace("÷", "/")
    if not re.fullmatch(r"[0-9.\s()+*/\-]+", expression) or not re.search(r"[+*/\-]", expression):
        return None
    try:
        if len(expression) > 160:
            raise ValueError("too_large")
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 48:
            raise ValueError("too_large")
        def evaluate(node):
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                token = ast.get_source_segment(expression, node)
                if len(token) > 40:
                    raise ValueError("too_large")
                result = Fraction(token)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div}:
                result = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                          ast.Div: operator.truediv}[type(node.op)](evaluate(node.left), evaluate(node.right))
            else:
                raise ValueError("unsupported_expression")
            if abs(result.numerator) > 10**40 or result.denominator > 10**40:
                raise ValueError("too_large")
            return result
        value = evaluate(tree.body)
        remaining = value.denominator
        for factor in (2, 5):
            while remaining % factor == 0:
                remaining //= factor
        rendered = str(value)
        if remaining == 1 and value.denominator != 1:
            with localcontext() as context:
                context.prec = 100
                rendered = format(Decimal(value.numerator) / Decimal(value.denominator), "f").rstrip("0").rstrip(".")
        return {"expression": expression, "value": rendered, "error": None}
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return {"expression": expression, "value": None, "error": "invalid_or_outside_supported_arithmetic"}


def prior_turns(state, turn):
    return [t for t in state["turns"] if t["kind"] == "user" and t["number"] < turn["number"]
            and t["status"] == "completed"]


def target_answer(state, turn):
    """Match actual cited subjects, never a broad candidate list from discovery."""
    from .task_context import mentions
    earlier = prior_turns(state, turn)
    text = turn["text"]
    mentioned = []
    for old in earlier:
        citations = old.get("citation_audit", {}).get("citations", [])
        if any(mentions(text, c.get("post_id")) or mentions(text, c.get("title")) for c in citations):
            mentioned.append(old)
    if mentioned:
        return mentioned[-1]
    subject_text = REFERENCE_TO_REPLY.sub("", text)
    subject_text = re.sub(r"\b(?:your|the|this|that|in one sentence|briefly)\b|一句话|一个句子", "", subject_text, flags=re.I)
    subject = re.search(r"(?:总结|概括|简述|summari[sz]e)\s*(?:一下|下)?(.+?)(?:是什么)?[。？?]?$", subject_text.strip(), re.I)
    if subject and subject[1].strip() not in {"它", "它们", "这段", "这段话", "上面的内容", "一下"}:
        return None
    # If the requested named subject belongs to another known task, do not
    # summarize an unrelated last answer. Unknown references request clarification.
    from .task_context import mentions as topic_mentioned
    if any(topic_mentioned(text, c["post_id"]) or topic_mentioned(text, c["title"])
           for task in state.get("task_memory", {}).get("tasks", []) for c in task["candidates"]):
        return None
    return earlier[-1] if earlier else None


def classify(state, turn):
    text = turn["text"].strip()
    contract = {"version": 1, "kind": "article", "evidence": "article_text",
                "target_turn": None, "one_sentence": False, "brief": False, "format_retries": 0}
    calculation = arithmetic(text)
    if calculation is not None:
        contract.update(kind="calculation", evidence="local_calculation", calculation=calculation)
    elif re.search(r"我(?:的)?(?:上一轮|上一次|刚才|之前).{0,8}(?:说|问|输入).{0,8}(?:什么|啥)|\bwhat did i (?:say|ask) (?:last|before|previously)\b", text, re.I):
        earlier = prior_turns(state, turn)
        contract.update(kind="conversation", evidence="conversation_metadata", target_turn=earlier[-1]["number"] if earlier else None)
    elif REFERENCE_TO_REPLY.search(text) and REWRITE.search(text):
        target = target_answer(state, turn)
        local_reply = target and target.get("result") == "completed" and (
            target.get("answer_contract", {}).get("kind") in {"calculation", "runtime", "conversation"}
            or target.get("retrieval", {}).get("route") == "help")
        contract.update(kind="rewrite" if target else "clarify", evidence="verified_reply_and_sources",
                        target_turn=target["number"] if target else None,
                        one_sentence=bool(re.search(r"一句话|一个句子|one sentence", text, re.I)),
                        brief=bool(REWRITE.search(text)) and not bool(re.search(r"详细|展开|多段|\b(?:detailed|expand)\b", text, re.I)))
        if local_reply:
            contract["evidence"] = "conversation_metadata"
    elif RUNTIME.search(text) or text.casefold().rstrip("？?。!") in {"help", "帮助", "使用帮助"}:
        mixed = ARTICLE_SCOPE.search(text) or re.search(r"[,，;；].*(?:解释|读取|比较|\b(?:explain|read|compare)\b)", text, re.I)
        contract.update(kind="clarify" if mixed else "runtime", evidence="runtime")
    return contract


def validate_contracts(state):
    numbers = {t["number"] for t in state["turns"] if t["kind"] == "user"}
    for turn in state["turns"]:
        contract = turn.get("answer_contract")
        if contract is None:
            continue
        target = contract.get("target_turn") if isinstance(contract, dict) else None
        if (not isinstance(contract, dict) or contract.get("version") != 1 or contract.get("kind") not in KINDS
                or not isinstance(contract.get("evidence"), str)
                or type(contract.get("one_sentence")) is not bool
                or type(contract.get("brief")) is not bool
                or type(contract.get("format_retries")) is not int or not 0 <= contract["format_retries"] <= 1
                or (target is not None and (type(target) is not int or target not in numbers or target >= turn["number"]))):
            raise ValueError("Invalid saved answer contract")
        if contract["evidence"] not in EVIDENCE[contract["kind"]] or (contract["kind"] == "rewrite" and target is None):
            raise ValueError("Invalid saved evidence contract")
        if contract["kind"] == "calculation":
            # A saved calculated answer is recomputed from the original request,
            # not trusted as an arbitrary checkpoint-provided response.
            if contract.get("calculation") != arithmetic(turn.get("text", "")):
                raise ValueError("Invalid saved calculation")


def direct_reply(state, turn):
    contract = turn.get("answer_contract", {})
    kind = contract.get("kind")
    if kind == "calculation":
        result = contract["calculation"]
        return ("该表达式不在当前支持的四则计算范围内，或含除零等错误。请提供较短的四则表达式。"
                if result["error"] else result["expression"] + " = " + result["value"])
    if kind == "conversation":
        target = next((t for t in state["turns"] if t["number"] == contract["target_turn"]), None)
        return "你上一轮说的是：\n" + target["text"] if target else "当前会话还没有更早的用户消息。"
    if kind == "clarify":
        return "请明确要改写哪一次回答，或是想了解助手功能、还是解释某篇文章。"
    return None


def one_sentence(answer):
    from .section_citations import REFERENCE
    body = REFERENCE.sub("", answer).strip()
    if not body or len(body) > 500 or re.search(r"(?m)^\s*(?:#{1,6}\s|[-*]\s|\d+\.\s)|\n\s*\n|\|.*\|", body):
        return False
    sentences = [s for s in re.split(r"[。！？]+|(?<=[.!?])\s+", body) if s.strip()]
    return len(sentences) == 1


def requested_shape(answer, contract):
    if contract.get("one_sentence"):
        return one_sentence(answer)
    if not contract.get("brief"):
        return True
    from .section_citations import REFERENCE
    body = REFERENCE.sub("", answer).strip()
    return bool(body) and len(body) <= 500 and not re.search(r"\n\s*\n|(?m:^\s*(?:#{1,6}\s|[-*]\s|\d+\.\s))", body)
