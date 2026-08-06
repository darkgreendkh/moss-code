"""`/rewind`：把工作区和会话一起退回第 n 步之前。

为什么存在（spec-07 §4.9）：
agent 走错路的时候，用户手上其实有两份需要一起回退的东西——磁盘上的文件，
和会话里的叙事。只回退一份都没用：文件回去了但历史还说"我已经改好了"，
下一轮就会在错误的前提上继续；历史回去了但文件还是改过的样子，模型看到的
仓库和它以为的对不上。

所以这里做三件事，要么全做要么不做：
1. 用 `.moss/runs/<id>/undo/<action_id>/` 里存的旧内容还原文件；
2. 把 history 截回那一步之前的长度；
3. 把 memory 回滚到当时的 checkpoint 快照。

一条硬规则：**用户自己的未提交改动不得被覆盖**。回滚前拿 undo 记录里的
after_sha 和当前文件比一比，对不上就说明这个文件在 agent 改完之后又被动过
（用户手改、另一个进程写的）。那时候宁可什么都不做，把冲突列出来等确认——
悄悄盖掉用户的改动，是这个功能唯一不可接受的失败方式。
"""

import copy

from . import action_ledger
from .features import memory as memorylib
from .tools import write_text_atomic

STATUS_OK = "ok"
STATUS_NOTHING_TO_DO = "nothing_to_do"
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"


def plan_rewind(agent, steps=1):
    """算出这次 rewind 会碰哪些东西，但不动手。

    返回 (要回退的 undo 条目, 冲突列表)。冲突 = 文件在 agent 改完之后又被动过。
    """
    entries = list(agent.session.get("undo", []) or [])
    steps = max(1, int(steps or 1))
    targets = entries[-steps:]
    conflicts = []
    # 每个路径只和"这段窗口里最后一次动它的动作"留下的指纹比。
    # 同一个文件被连改两次时，第一条记录的 after_sha 本来就该对不上——
    # 那是 agent 自己的第二次改动，不是用户的手改。
    latest_after = {}
    for entry in targets:
        manifest = agent.run_store.read_undo(entry["run_id"], entry["action_id"])
        if manifest is None:
            conflicts.append(
                {"action_id": entry["action_id"], "path": "", "reason": "undo_record_missing"}
            )
            continue
        if not manifest.get("restorable", False):
            conflicts.append(
                {
                    "action_id": entry["action_id"],
                    "path": "",
                    "reason": "not_restorable",
                    "tool": manifest.get("tool", ""),
                    "git_object": manifest.get("git_object", ""),
                }
            )
            continue
        for rel_path in manifest.get("paths", []):
            recorded = dict(manifest.get("sha_after", {}) or {}).get(rel_path)
            if recorded is not None:
                latest_after[rel_path] = (entry["action_id"], recorded)
    for rel_path, (action_id, recorded) in sorted(latest_after.items()):
        if action_ledger.file_sha(agent.root / rel_path) != recorded:
            conflicts.append(
                {"action_id": action_id, "path": rel_path, "reason": "changed_since_run"}
            )
    return targets, conflicts


def rewind(agent, steps=1, force=False):
    """执行回滚。返回一份可以直接打给用户看的报告。"""
    targets, conflicts = plan_rewind(agent, steps)
    if not targets:
        return {"status": STATUS_NOTHING_TO_DO, "restored": [], "conflicts": []}
    if conflicts and not force:
        # 什么都不做。部分回滚会留下一个"一半旧一半新"的工作区，
        # 比不回滚更难收拾。
        return {"status": STATUS_NEEDS_CONFIRMATION, "restored": [], "conflicts": conflicts}

    restored = []
    # 倒着回放：后发生的动作先撤，才能回到最早那一步之前的样子。
    for entry in reversed(targets):
        manifest = agent.run_store.read_undo(entry["run_id"], entry["action_id"])
        if manifest is None:
            continue
        existed = dict(manifest.get("existed", {}) or {})
        for rel_path in manifest.get("paths", []):
            target = agent.root / rel_path
            if not existed.get(rel_path, False):
                # 那次动作之前这个文件不存在，回滚就是把它删掉。
                try:
                    target.unlink()
                    restored.append({"path": rel_path, "action": "deleted"})
                except OSError:
                    pass
                continue
            text = agent.run_store.undo_file_text(entry["run_id"], entry["action_id"], rel_path)
            if text is None:
                continue
            write_text_atomic(target, text)
            restored.append({"path": rel_path, "action": "restored"})

    oldest = targets[0]
    _rewind_session(agent, oldest)
    agent.session["undo"] = list(agent.session.get("undo", []) or [])[: -len(targets)]
    agent.session_path = agent.session_store.save(agent.session)
    return {
        "status": STATUS_OK,
        "steps": len(targets),
        "restored": restored,
        "conflicts": conflicts,
        "checkpoint_id": oldest.get("checkpoint_id", ""),
    }


def _rewind_session(agent, entry):
    """历史截回那一步之前，memory 回到当时的 checkpoint 快照。"""
    keep = int(entry.get("history_length", 0) or 0)
    agent.session["history"] = list(agent.session.get("history", []) or [])[:keep]
    checkpoint_id = str(entry.get("checkpoint_id", "") or "")
    checkpoints = agent.session.setdefault("checkpoints", {"current_id": "", "items": {}})
    items = dict(checkpoints.get("items", {}) or {})
    checkpoint = items.get(checkpoint_id)
    if checkpoint is None:
        # 没有可回滚到的 checkpoint（第一步就被撤销）：只截历史，memory 不动。
        # 硬造一份空 memory 会把"读过哪些文件"也一起抹掉，那不是用户要的。
        return
    # 深拷贝：直接用那份快照的话，恢复之后的 memory 写入会改掉 checkpoint 自己。
    snapshot = copy.deepcopy(dict(checkpoint.get("memory", {}) or {}))
    if snapshot:
        agent.session["memory"] = snapshot
        agent.memory = memorylib.LayeredMemory(
            agent.session["memory"],
            workspace_root=agent.root,
            session_id=agent.session["id"],
            event_callback=agent.record_memory_event,
        )
    checkpoints["current_id"] = checkpoint_id
    # 分叉点之后的 checkpoint 一并丢掉：它们描述的是一条已经被撤销的时间线。
    order = list(items)
    cut = order.index(checkpoint_id) + 1
    checkpoints["items"] = {key: items[key] for key in order[:cut]}
    agent.current_plan = list(checkpoint.get("plan", []) or [])


def render_rewind(result):
    status = result.get("status", "")
    if status == STATUS_NOTHING_TO_DO:
        return "nothing to rewind: this session has no recorded file changes."
    if status == STATUS_NEEDS_CONFIRMATION:
        lines = ["rewind stopped: some files changed after the agent touched them."]
        for conflict in result.get("conflicts", []):
            if conflict["reason"] == "not_restorable":
                detail = f"{conflict.get('tool', '?')} cannot be undone automatically"
                if conflict.get("git_object"):
                    detail += f" (git object {conflict['git_object'][:12]})"
                lines.append(f"  - {detail}")
            else:
                lines.append(f"  - {conflict['path'] or '(record)'}: {conflict['reason']}")
        lines.append("re-run with /rewind! to discard those changes anyway.")
        return "\n".join(lines)
    lines = [f"rewound {result.get('steps', 0)} step(s)."]
    for item in result.get("restored", []):
        lines.append(f"  {item['action']}: {item['path']}")
    return "\n".join(lines)
