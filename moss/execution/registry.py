"""Explicit tool validation and registry assembly."""

from functools import partial

from .builtins.extensions import (
    _context_skills,
    normalize_plan,
    render_plan,
    tool_delegate,
    tool_describe_tool,
    tool_run_orchestration,
    tool_update_plan,
    tool_use_skill,
)
from .builtins.files import (
    NOFOLLOW_SUPPORTED,
    tool_edit_file,
    tool_list_files,
    tool_read_artifact,
    tool_read_file,
    tool_search_text,
    tool_write_file,
    write_text_atomic,
)
from .builtins.memory import (
    tool_memory_delete,
    tool_memory_search,
    tool_memory_update,
    tool_memory_write,
)
from .builtins.shell import classify_shell_command, run_shell_command, tool_run_shell
from .specs import (
    BASE_TOOL_SPECS,
    DELEGATE_TOOL_SPEC,
    DESCRIBE_TOOL_SPEC,
    RUN_ORCHESTRATION_TOOL_SPEC,
    TOOL_EXAMPLES,
    USE_SKILL_TOOL_SPEC,
    ToolField,
    ToolRunOutput,
    ToolSpec,
    _tool_spec,
    legal_tool_names,
    tool_example,
    tool_spec,
)

def _context_mcp_tools(context):
    # 和 _context_skills 一样走 getattr：context 不一定是完整的 ToolContext
    # （评测和测试会传更窄的桩），少一个可选字段不该让整张注册表建不起来。
    provider = getattr(context, "mcp_tools", None)
    return dict(provider() or {}) if provider is not None else {}


def _context_catalog_threshold(context):
    from ..context.prefix import TOOL_CATALOG_THRESHOLD

    return int(getattr(context, "catalog_threshold", TOOL_CATALOG_THRESHOLD) or TOOL_CATALOG_THRESHOLD)


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
    # code mode 双前置：显式开关 + 沙箱可用。策略层挡不住 __builtins__ 逃逸，
    # 只有 OS 隔离能兜底——所以没有沙箱就根本不给这个工具。
    if getattr(context, "code_mode_enabled", False):
        tools["run_orchestration"] = _registry_entry(
            RUN_ORCHESTRATION_TOOL_SPEC, context, tool_run_orchestration
        )
    # 外部 MCP 工具在**启动期**并进来，和内置工具走同一套护栏。
    # 运行期不做动态发现：模型看到的动作集合在 run 内必须是冻结的。
    tools.update(_context_mcp_tools(context))
    if len(tools) > _context_catalog_threshold(context):
        # 目录模式下 schema 不进 prefix，得留一条按需取回的路。
        tools["describe_tool"] = _registry_entry(DESCRIBE_TOOL_SPEC, context, tool_describe_tool)
    return tools


def _validate_schema(name, args, spec=None):
    spec = spec or _tool_spec(name)
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
    # MCP 工具的 spec 不在 BASE_TOOL_SPECS 里，但它照样要过参数校验——
    # 外部工具的入参**更**需要校验，它的实现不在我们手上。
    registry = getattr(context, "tool_registry", None)
    external = (
        (registry() or {}).get(name, {}).get("spec")
        if registry is not None and name.startswith("mcp__")
        else None
    )
    _validate_schema(name, args, spec=external)
    if external is not None:
        return

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

    if name == "read_artifact":
        path = context.run_path(args["path"])
        if not path.is_file():
            raise ValueError("path is not an artifact file")
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
        tasks = [str(item).strip() for item in (args.get("tasks") or ()) if str(item).strip()]
        task = str(args.get("task", "")).strip()
        if not task and not tasks:
            raise ValueError("task must not be empty")
        if not all(isinstance(item, str) for item in (args.get("focus") or ())):
            raise ValueError("focus must contain strings")
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

    if name == "describe_tool":
        if not str(args.get("name", "")).strip():
            raise ValueError("name must not be empty")
        return

    if name == "run_orchestration":
        from ..extensions import code_mode

        # 校验期就跑 AST 白名单：一段逃逸脚本该在审批摘要出现之前就被拒掉，
        # 不该让用户对着它按一次 y。
        code_mode.validate_script(args.get("script", ""))
        return


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search_text": tool_search_text,
    "read_artifact": tool_read_artifact,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "update_plan": tool_update_plan,
    "memory_write": tool_memory_write,
    "memory_update": tool_memory_update,
    "memory_delete": tool_memory_delete,
    "memory_search": tool_memory_search,
}

# Concise name for new consumers; the long-standing name remains public.
build_tools = build_tool_registry

__all__ = [
    "BASE_TOOL_SPECS",
    "DELEGATE_TOOL_SPEC",
    "DESCRIBE_TOOL_SPEC",
    "NOFOLLOW_SUPPORTED",
    "RUN_ORCHESTRATION_TOOL_SPEC",
    "TOOL_EXAMPLES",
    "USE_SKILL_TOOL_SPEC",
    "ToolField",
    "ToolRunOutput",
    "ToolSpec",
    "build_tool_registry",
    "build_tools",
    "classify_shell_command",
    "legal_tool_names",
    "native_tool_definitions",
    "normalize_plan",
    "render_plan",
    "run_shell_command",
    "tool_delegate",
    "tool_edit_file",
    "tool_example",
    "tool_read_file",
    "tool_spec",
    "tool_write_file",
    "validate_tool",
    "write_text_atomic",
]
