"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from .workspace import now


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    # stable_hash 只覆盖「稳定头」（身份/规则/Tools/Skills/示例），不含 workspace 段。
    # prompt_cache_key 用它而不是 hash：否则 agent 自己每次 write_file/edit_file
    # 都会改 `git status` -> workspace 段变 -> 整段 hash 变 -> 缓存路由键每轮抖动，
    # 恰好在「多步编辑」这个最该命中缓存的场景里把缓存打散。
    stable_hash: str
    workspace_fingerprint: str
    tool_signature: str
    skill_signature: str
    built_at: str


def tool_signature(tools):
    payload = []
    for name in sorted(tools):
        tool = tools[name]
        payload.append(
            {
                "name": name,
                "schema": tool["schema"],
                "risky": tool["risky"],
                "description": tool["description"],
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def skill_signature(skills):
    # 只对会进入 prefix 的字段（name + description）做指纹，
    # 这样 skill 的增删改名都会改变整段 prefix 的 hash，从而正确触发重建。
    payload = []
    for name in sorted(skills or {}):
        skill = skills[name]
        payload.append({"name": name, "description": skill.get("description", "")})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def build_prompt_prefix(workspace, tools, skills=None, built_at=None):
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    tool_text = "\n".join(tool_lines)
    skills = skills or {}
    skill_lines = [f"- {name}: {skill.get('description', '')}".rstrip() for name, skill in skills.items()]
    # skill 列在 Tools 下面；没有 skill 时整段省略，保证无 skill 时的 prefix 与改动前逐字节一致。
    skills_section = ("\n\nSkills:\n" + "\n".join(skill_lines)) if skill_lines else ""
    examples = "\n".join(
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            '<tool name="edit_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "<final>Done.</final>",
        ]
    )
    # 稳定头：agent 的“工作手册”——它是谁、有哪些工具/技能、怎么调用。
    # 这一段在一次任务内不随 agent 自己的文件改动而变，所以适合做缓存键。
    head = textwrap.dedent(
        f"""\
        You are moss, a small local coding agent working inside a local repository.

        Rules:
        - Use tools instead of guessing about the workspace.
        - Return exactly one <tool>...</tool> or one <final>...</final>.
        - Tool calls must look like:
          <tool>{{"name":"tool_name","args":{{...}}}}</tool>
        - For write_file and edit_file with multi-line text, prefer XML style:
          <tool name="write_file" path="file.py"><content>...</content></tool>
        - Final answers must look like:
          <final>your answer</final>
        - Never invent tool results.
        - Keep answers concise and concrete.
        - If the user asks you to create or update a specific file and the path is clear, use write_file or edit_file instead of repeatedly listing files.
        - Before writing tests for existing code, read the implementation first.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        - Required tool arguments must not be empty. Do not call read_file, write_file, edit_file, run_shell, or delegate with args={{}}.

        Tools:
        {tool_text}{skills_section}

        Valid response examples:
        {examples}
        """
    ).strip()
    # 易变尾：workspace 快照（含 git status / recent_commits），每轮可能变。
    # 拼在稳定头之后，整段 text == 改动前逐字节一致（旧模板里 workspace 也在最后）。
    text = f"{head}\n\n{workspace.text()}"
    signature = tool_signature(tools)
    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        stable_hash=hashlib.sha256(head.encode("utf-8")).hexdigest(),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=signature,
        skill_signature=skill_signature(skills),
        built_at=built_at or now(),
    )
