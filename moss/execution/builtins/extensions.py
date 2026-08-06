"""Plan, delegation, skill and orchestration tools."""

from ...context.prefix import render_tool_schema

PLAN_STATUSES = ("pending", "in_progress", "done", "blocked")

MAX_PLAN_STEPS = 20

MAX_PLAN_TITLE_CHARS = 120


def _context_skills(context):
    provider = getattr(context, "skills", None)
    if provider is None:
        return {}
    return provider() or {}


def normalize_plan(steps):
    """把模型给的 steps 规整成可渲染的计划。

    宽进严出：id 缺了就补序号、status 写错就退回 pending、title 超长就截断。
    计划是给模型自己看的备忘，为格式问题拒绝掉整次调用得不偿失。
    """
    plan = []
    for index, raw in enumerate(list(steps or [])[:MAX_PLAN_STEPS], start=1):
        if not isinstance(raw, dict):
            raw = {"title": str(raw)}
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        status = str(raw.get("status", "pending")).strip().lower()
        if status not in PLAN_STATUSES:
            status = "pending"
        plan.append(
            {
                "id": str(raw.get("id", "") or index),
                "title": title[:MAX_PLAN_TITLE_CHARS],
                "status": status,
            }
        )
    return plan


def render_plan(plan):
    if not plan:
        return ""
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
    lines = ["Plan:"]
    lines.extend(f"- {marks.get(step['status'], '[ ]')} {step['id']}. {step['title']}" for step in plan)
    return "\n".join(lines)


def tool_update_plan(context, args):
    plan = normalize_plan(args.get("steps"))
    context.set_plan(plan)
    done = sum(1 for step in plan if step["status"] == "done")
    return f"plan updated: {len(plan)} step(s), {done} done"


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    tasks = [str(item).strip() for item in (args.get("tasks") or ()) if str(item).strip()]
    if not str(args.get("task", "")).strip() and not tasks:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


def tool_use_skill(context, args):
    skill_name = str(args.get("name", "")).strip()
    if not skill_name:
        raise ValueError("name must not be empty")
    if not _context_skills(context).get(skill_name):
        raise ValueError(f"unknown skill: {skill_name}")
    # 点亮 skill 是有状态的（能力临时覆盖 + 供应链确认），所以走 runtime 那条路，
    # 这里不自己拼正文——两处各拼一份迟早会漂移。
    return context.activate_skill(skill_name)


def tool_run_orchestration(context, args):
    """跑一段受限编排脚本。每次工具 API 调用仍然逐条走 ToolExecutor。"""
    from ...extensions import code_mode

    emitted, calls = code_mode.run_script(args.get("script", ""), context.run_guarded_tool)
    return code_mode.render_result(emitted, calls)


def tool_describe_tool(context, args):
    """把一个工具的完整参数 schema 取回来。

    目录模式下 prefix 里只有名字和一句话，模型要用某个不熟的工具时得先问一次。
    """
    name = str(args.get("name", "")).strip()
    tool = (getattr(context, "tool_registry", lambda: {})() or {}).get(name)
    if tool is None:
        raise ValueError(f"unknown tool: {name}")
    risk = "approval required" if tool["risky"] else "safe"
    return "\n".join(
        [
            f"{name}({render_tool_schema(tool['schema'])}) [{risk}]",
            str(tool["description"]),
            "capabilities: " + (", ".join(sorted(tool.get("capabilities") or ())) or "(none)"),
        ]
    )

