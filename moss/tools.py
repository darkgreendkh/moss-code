"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import os
import json
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass
from functools import partial

from . import sandbox
from . import shell_policy
from .workspace import IGNORED_PATH_NAMES


@dataclass(frozen=True)
class ToolRunOutput:
    content: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class ToolField:
    type: str
    required: bool = True
    default: object = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fields: dict[str, ToolField]
    risky: bool
    description: str
    # 能力标签（spec-03 §4.2）。未声明 = 空集；risky 且未声明会被 policy 直接拒绝
    # （fail-closed），这样新工具忘了声明会立刻在测试里炸，而不是默默放行。
    capabilities: frozenset = frozenset()
    # 路径作用域：workspace（默认）/ run_dir / memory_dir。
    path_scope: str = "workspace"


# O_NOFOLLOW 在 Windows 上不存在。降级必须显式：进 metadata 供 report 记录，
# 而不是假装这层防护存在。
NOFOLLOW_SUPPORTED = hasattr(os, "O_NOFOLLOW")


def write_text_atomic(path, content):
    # 不允许写穿软链：审批之后目标被换成指向仓库外的软链，写入就落到了别处。
    # 原子替换本身会替掉软链而不是跟随它，但先显式拒绝能让这次尝试留下痕迹。
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            handle.write(str(content))
            temp_name = handle.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def classify_shell_command(command):
    """shell 风险分级。实现下沉到 moss/shell_policy.py。

    留在这里只是为了保持 `from .tools import classify_shell_command` 这个
    既有调用点不变；真正的分级逻辑是基于 shlex 的结构化解析，
    见 shell_policy 模块头的说明。
    """
    return shell_policy.classify_shell_command(command)


BASE_TOOL_SPECS = {
    "list_files": ToolSpec(
        name="list_files",
        fields={"path": ToolField("str", required=False, default=".")},
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="List files in the workspace.",
    ),
    "read_file": ToolSpec(
        name="read_file",
        fields={
            "path": ToolField("str"),
            "start": ToolField("int", required=False, default=1, minimum=1),
            "end": ToolField("int", required=False, default=800, minimum=1),
        },
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="Read a UTF-8 file by line range.",
    ),
    "write_file": ToolSpec(
        name="write_file",
        fields={"path": ToolField("str"), "content": ToolField("str")},
        risky=True,
        capabilities=frozenset({"fs_write"}),
        description="Write a text file.",
    ),
    "edit_file": ToolSpec(
        name="edit_file",
        fields={"path": ToolField("str"), "old_text": ToolField("str"), "new_text": ToolField("str")},
        risky=True,
        capabilities=frozenset({"fs_read", "fs_write"}),
        description="Replace one exact text block in a file.",
    ),
    "search_text": ToolSpec(
        name="search_text",
        fields={"pattern": ToolField("str"), "path": ToolField("str", required=False, default=".")},
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="Search the workspace with rg or a simple fallback.",
    ),
    "update_plan": ToolSpec(
        name="update_plan",
        fields={"steps": ToolField("list")},
        risky=False,
        capabilities=frozenset(),
        description="Replace the current plan. Each step: {id, title, status}.",
    ),
    "run_shell": ToolSpec(
        name="run_shell",
        fields={
            "command": ToolField("str"),
            "timeout": ToolField("int", required=False, default=60, minimum=1, maximum=600),
        },
        risky=True,
        # shell 能干的事没有上界，所以四个能力全给——策略层据此决定拦不拦。
        capabilities=frozenset({"fs_read", "fs_write", "exec", "network"}),
        description="Run a shell command in the repo root.",
    ),
    "memory_write": ToolSpec(
        name="memory_write",
        fields={
            "scope": ToolField("str"),
            "topic": ToolField("str"),
            "text": ToolField("str"),
            "tags": ToolField("list", required=False, default=[]),
        },
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Write a session or project memory after safety checks.",
    ),
    "memory_update": ToolSpec(
        name="memory_update",
        fields={"id": ToolField("str"), "text": ToolField("str")},
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Replace an active durable memory with a new version.",
    ),
    "memory_delete": ToolSpec(
        name="memory_delete",
        fields={"id": ToolField("str")},
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Forget an active durable memory by appending a tombstone.",
    ),
    "memory_search": ToolSpec(
        name="memory_search",
        fields={
            "query": ToolField("str"),
            "limit": ToolField("int", required=False, default=5, minimum=1, maximum=20),
        },
        risky=False,
        capabilities=frozenset(),
        description="Search relevant working, episodic, and durable memory.",
    ),
}

DELEGATE_TOOL_SPEC = ToolSpec(
    name="delegate",
    fields={"task": ToolField("str"), "max_steps": ToolField("int", required=False, default=3, minimum=1)},
    risky=False,
    capabilities=frozenset({"fs_read", "spawn"}),
    description="Ask a bounded read-only child agent to investigate.",
)

USE_SKILL_TOOL_SPEC = ToolSpec(
    name="use_skill",
    fields={"name": ToolField("str")},
    risky=False,
    capabilities=frozenset({"fs_read"}),
    description="Load a skill's instructions by name before doing the work it describes.",
)


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate", "use_skill"}

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":400}}</tool>',
    "search_text": '<tool>{"name":"search_text","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "edit_file": '<tool name="edit_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
    "use_skill": '<tool>{"name":"use_skill","args":{"name":"some-skill"}}</tool>',
    "update_plan": '<tool>{"name":"update_plan","args":{"steps":[{"id":"1","title":"read the parser","status":"in_progress"},{"id":"2","title":"add a test","status":"pending"}]}}</tool>',
    "memory_write": '<tool>{"name":"memory_write","args":{"scope":"project","topic":"key-decisions","text":"Use SQLite","tags":["database"]}}</tool>',
    "memory_update": '<tool>{"name":"memory_update","args":{"id":"mem_123456789abc","text":"Use SQLite WAL"}}</tool>',
    "memory_delete": '<tool>{"name":"memory_delete","args":{"id":"mem_123456789abc"}}</tool>',
    "memory_search": '<tool>{"name":"memory_search","args":{"query":"database choice","limit":5}}</tool>',
}

# 计划步骤的合法状态。多一个状态就多一种模型会写错的写法，四个够用了。
PLAN_STATUSES = ("pending", "in_progress", "done", "blocked")
# 计划规模上限。超过 20 步的"计划"已经不是计划，是把任务复述一遍。
MAX_PLAN_STEPS = 20
MAX_PLAN_TITLE_CHARS = 120


def _context_skills(context):
    provider = getattr(context, "skills", None)
    if provider is None:
        return {}
    return provider() or {}


def _registry_entry(spec, context, runner):
    return {
        "schema": dict(spec.fields),
        "risky": spec.risky,
        "description": spec.description,
        "capabilities": frozenset(spec.capabilities),
        "path_scope": spec.path_scope,
        "spec": spec,
        "run": partial(runner, context),
    }


def _json_schema_for_fields(fields):
    properties = {}
    required = []
    type_map = {
        "str": "string",
        "int": "integer",
        "list": "array",
    }
    for name, field in fields.items():
        json_type = type_map.get(str(field.type), "string")
        payload = {"type": json_type}
        if not field.required:
            payload["default"] = field.default
        if field.minimum is not None:
            payload["minimum"] = field.minimum
        if field.maximum is not None:
            payload["maximum"] = field.maximum
        properties[name] = payload
        if field.required:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def native_tool_definitions(tools, native_tool_format):
    definitions = []
    for name, tool in tools.items():
        schema = _json_schema_for_fields(tool["schema"])
        if native_tool_format == "openai_responses":
            definitions.append(
                {
                    "type": "function",
                    "name": name,
                    "description": tool["description"],
                    "parameters": schema,
                }
            )
        elif native_tool_format == "anthropic_messages":
            definitions.append(
                {
                    "name": name,
                    "description": tool["description"],
                    "input_schema": schema,
                }
            )
        else:
            raise ValueError(f"unknown native tool format: {native_tool_format}")
    return definitions


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: _registry_entry(spec, context, _TOOL_RUNNERS[name])
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = _registry_entry(DELEGATE_TOOL_SPEC, context, tool_delegate)
    # 只有真的存在 skill 时才暴露 use_skill，避免给模型一个无处可用的工具。
    if _context_skills(context):
        tools["use_skill"] = _registry_entry(USE_SKILL_TOOL_SPEC, context, tool_use_skill)
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def tool_spec(name):
    """按名字取 ToolSpec。策略层要用它读能力标签。"""
    return _tool_spec(name)


def _tool_spec(name):
    if name in BASE_TOOL_SPECS:
        return BASE_TOOL_SPECS[name]
    if name == "delegate":
        return DELEGATE_TOOL_SPEC
    if name == "use_skill":
        return USE_SKILL_TOOL_SPEC
    return None


def _validate_schema(name, args):
    spec = _tool_spec(name)
    if spec is None:
        return
    for field_name, field in spec.fields.items():
        if field.required and field_name not in args:
            raise ValueError(f"missing required argument: {field_name}")
        if field_name not in args:
            continue
        value = args[field_name]
        if field.type == "str":
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string")
        elif field.type == "list":
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{field_name} must be a list")
        elif field.type == "int":
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be an integer") from exc
            if field.minimum is not None and number < field.minimum:
                if field.maximum is not None:
                    raise ValueError(f"{field_name} must be in [{field.minimum}, {field.maximum}]")
                raise ValueError(f"{field_name} must be >= {field.minimum}")
            if field.maximum is not None and number > field.maximum:
                if field.minimum is not None:
                    raise ValueError(f"{field_name} must be in [{field.minimum}, {field.maximum}]")
                raise ValueError(f"{field_name} must be <= {field.maximum}")


def validate_tool(context, name, args):
    args = args or {}
    _validate_schema(name, args)

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search_text":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 60))
        if timeout < 1 or timeout > 600:
            raise ValueError("timeout must be in [1, 600]")
        return

    if name == "update_plan":
        steps = args.get("steps")
        if not isinstance(steps, (list, tuple)):
            raise ValueError("steps must be a list")
        if not normalize_plan(steps):
            raise ValueError("steps must contain at least one step with a title")
        return

    if name == "memory_write":
        scope = str(args.get("scope", "")).strip()
        if scope not in {"session", "project"}:
            raise ValueError("scope must be session or project")
        if not str(args.get("topic", "")).strip():
            raise ValueError("topic must not be empty")
        if not str(args.get("text", "")).strip():
            raise ValueError("text must not be empty")
        tags = args.get("tags", [])
        if not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValueError("tags must contain non-empty strings")
        return

    if name == "memory_update":
        if not str(args.get("id", "")).strip():
            raise ValueError("id must not be empty")
        if not str(args.get("text", "")).strip():
            raise ValueError("text must not be empty")
        return

    if name == "memory_delete":
        if not str(args.get("id", "")).strip():
            raise ValueError("id must not be empty")
        return

    if name == "memory_search":
        if not str(args.get("query", "")).strip():
            raise ValueError("query must not be empty")
        limit = int(args.get("limit", 5))
        if limit < 1 or limit > 20:
            raise ValueError("limit must be in [1, 20]")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "edit_file":
        # edit_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return

    if name == "use_skill":
        skill_name = str(args.get("name", "")).strip()
        if not skill_name:
            raise ValueError("name must not be empty")
        if skill_name not in _context_skills(context):
            raise ValueError(f"unknown skill: {skill_name}")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_search_text(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=context.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 60))
    if timeout < 1 or timeout > 600:
        raise ValueError("timeout must be in [1, 600]")
    plan = getattr(context, "sandbox_plan", None)
    wrapped = sandbox.wrap_command(command, plan, workspace=context.root) if plan is not None else None
    returncode, stdout, stderr = run_shell_command(
        wrapped or command,
        cwd=context.root,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=context.shell_env(),
        cancel_token=context.cancel_token,
    )
    content = textwrap.dedent(
        f"""\
        exit_code: {returncode}
        stdout:
        {stdout.strip() or "(empty)"}
        stderr:
        {stderr.strip() or "(empty)"}
        """
    ).strip()
    return ToolRunOutput(
        content=content,
        stdout=stdout,
        stderr=stderr,
        exit_code=returncode,
    )


def _terminate_process_group(process):
    """连同整个进程组一起杀。

    为什么不能只 kill 子进程：命令走 shell=True 起的是一个 shell，
    真正干活的是它的子进程（`pytest`、`npm` 之类）。只杀 shell 会留下孤儿进程
    继续占 CPU、继续写工作区——对 agent 来说是最难排查的一类问题。
    POSIX 上用 killpg，Windows 上没有进程组语义，退回 kill。
    """
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def run_shell_command(command, *, cwd, timeout, env, cancel_token=None, poll_interval=0.1):
    """跑一条 shell 命令，返回 (returncode, stdout, stderr)。

    自己轮询而不是直接用 `subprocess.run(timeout=...)`：那样拿不到取消信号，
    用户 Ctrl-C 之后命令还会继续跑到超时为止。
    command 可以是字符串（走 shell）或 argv 列表（沙箱包裹后的形式）。
    """
    popen_kwargs = {"shell": isinstance(command, str)}
    if hasattr(os, "setsid"):
        # 独立进程组，超时/取消时才能整组杀干净。
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(  # noqa: S602 - shell=True 是这个工具的语义本身
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        **popen_kwargs,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = process.communicate(timeout=poll_interval)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            pass
        if cancel_token is not None and cancel_token.is_set():
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            raise RuntimeError("run_shell cancelled")
        if time.monotonic() >= deadline:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)


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


def _json_result(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def tool_memory_write(context, args):
    return _json_result(context.write_memory(args))


def tool_memory_update(context, args):
    return _json_result(context.update_memory(args))


def tool_memory_delete(context, args):
    return _json_result(context.delete_memory(args))


def tool_memory_search(context, args):
    matches = context.search_memory(args)
    return _json_result(matches) if matches else "no relevant memory"


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, content)
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_edit_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    write_text_atomic(path, text.replace(old_text, str(args["new_text"]), 1))
    return f"edited {path.relative_to(context.root)}"


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


def tool_use_skill(context, args):
    skill_name = str(args.get("name", "")).strip()
    if not skill_name:
        raise ValueError("name must not be empty")
    skill = _context_skills(context).get(skill_name)
    if not skill:
        raise ValueError(f"unknown skill: {skill_name}")
    return skill.get("body", "") or "(skill has no instructions)"


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search_text": tool_search_text,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "update_plan": tool_update_plan,
    "memory_write": tool_memory_write,
    "memory_update": tool_memory_update,
    "memory_delete": tool_memory_delete,
    "memory_search": tool_memory_search,
}
