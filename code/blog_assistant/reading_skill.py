"""Explicit, frozen opt-in to the reviewed upstream reading protocol."""
import hashlib
import os
from pathlib import Path

NAME = "efficient-web-research"
SOURCE_HASH = "28d5db760b0a2c0c2e606601b8db2ab0a5108221bf40004715655eccb9f0cfc7"
ADAPTER = """本地适配（优先于上面的网页研究建议）：
本实验使用 efficient-web-research 的分层阅读方法，只作用于文章问答。
先确定问题、比较对象与覆盖要求，再发现来源、定位相关段落、读取正文、按来源归纳。
现有工具映射：search_posts 找候选；find_file 找文件；find_text 在已发现文件内定位；
read_post_full 读取短文或获取长文导航；read_post 按返回范围读取。只调用实际提供的工具。
本机没有联网搜索、URL抓取、浏览器或子代理；收到网址时如实说明能力范围。
候选摘要、标题、导航只用于定位，文章结论必须由本轮可见正文支持，保留原有引用格式。
问答只读相关范围；全文总结检查章节覆盖，未读部分须明确说明。比较任务逐一覆盖指定来源，
不因第一篇能回答而结束；原技能的三来源、首尾和固定token上限仅作启发，不替代任务范围。
完成条件是所需证据已覆盖或预算要求停止；预算不足交付有依据的部分和缺口。
采用用户要求的语言与格式；原技能的Confidence不代表已经通过语义验证。
遵循下方应用规则与程序权限、预算、旧回答改写及停止条件。
"""


def load_snapshot():
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "skills" / NAME / "SKILL.md"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_HASH:
        raise ValueError("Reading skill differs from the reviewed version; review it before enabling")
    return {"name": NAME, "version": 1, "sha256": SOURCE_HASH,
            "text": raw.decode("utf-8"), "adapter": ADAPTER}


def validate_snapshot(state):
    skill = state.get("reading_skill")
    if skill is None:
        return
    if (not isinstance(skill, dict) or skill.get("name") != NAME
            or skill.get("version") != 1 or skill.get("sha256") != SOURCE_HASH
            or not isinstance(skill.get("text"), str)
            or hashlib.sha256(skill["text"].encode("utf-8")).hexdigest() != SOURCE_HASH
            or skill.get("adapter") != ADAPTER):
        raise ValueError("Invalid saved reading skill snapshot")


def guidance(state):
    skill = state.get("reading_skill")
    if not skill:
        return ""
    return "阅读技能实验（原文）：\n" + skill["text"] + "\n" + skill["adapter"] + "\n应用规则：\n"


def overhead(state):
    return len(guidance(state).encode("utf-8"))
