"""Public command-line facade assembled from responsibility modules."""

import sys

from ..runs import checkpoint as checkpointlib
from ..runs import rewind as rewindlib
from .commands.mcp import build_mcp_arg_parser, run_mcp_command
from .commands.memory import build_memory_arg_parser, run_memory_command
from .commands.runs import build_runs_arg_parser, run_runs_command
from .factory import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    _effective_provider,
    build_agent,
)
from .parser import build_arg_parser
from .repl import (
    HELP_DETAILS,
    _ctype_codeset,
    _scrub_undecodable,
    build_welcome,
    enable_line_editing,
    make_progress_printer,
)

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["memory"]:
        return run_memory_command(argv[1:])
    if argv[:1] == ["runs"]:
        return run_runs_command(argv[1:])
    if argv[:1] == ["mcp"]:
        return run_mcp_command(argv[1:])
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    if getattr(args, "explain", False):
        # 恢复之前先说清楚会恢复出什么。走 stdout：它是这次调用的产出，
        # 应当可以被管道接走。
        print(checkpointlib.render_explain(checkpointlib.explain_resume(agent)))
        return 0

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    # 装上实时进度渲染器，让工具循环里的每一步都对用户可见。
    progress = make_progress_printer(sys.stderr)
    agent.progress_observer = progress

    # 首次运行最常见的踩坑：没配 key 就直接调用，拿到一坨 HTTP 报错。
    # 这里提前给一句人话提示，指出该设哪个环境变量。Ollama 不需要 key，跳过。
    if hasattr(agent.model_client, "api_key") and not agent.model_client.api_key:
        provider = _effective_provider(args)
        env_hint = {
            "deepseek": "MOSS_DEEPSEEK_API_KEY (or DEEPSEEK_API_KEY)",
            "openai": "MOSS_OPENAI_API_KEY (or OPENAI_API_KEY)",
            "anthropic": "MOSS_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)",
        }.get(provider, "the provider API key")
        print(
            f"warning: no API key found for provider '{provider}'. "
            f"Set {env_hint} in your .env or environment before sending a request.",
            file=sys.stderr,
        )

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                answer = agent.ask(prompt)
            except KeyboardInterrupt:
                progress.clear()
                print("\n(cancelled)", file=sys.stderr)
                return 130
            except RuntimeError as exc:
                progress.clear()
                print(str(exc), file=sys.stderr)
                return 1
            progress.clear()
            print(answer)
            # 后端错误现在会被 agent 收敛成一次已收尾的失败运行（而不是抛异常），
            # 但 one-shot / CI 场景需要用退出码反映失败，否则脚本会误以为成功。
            task_state = getattr(agent, "current_task_state", None)
            if task_state is not None and task_state.status == "failed":
                return 1
        return 0

    enable_line_editing()

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        # 空行单独 print 而不塞进 prompt：readline 要靠 prompt 算显示宽度，
        # prompt 里带 \n 会让它的列计数错位，宽字符重绘就跟着花屏。
        print()
        try:
            user_input = _scrub_undecodable(input("moss> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0
        except UnicodeDecodeError:
            # 没有 readline 兜底的环境（Windows、被裁剪的构建）里，半个 UTF-8
            # 序列还是可能漏进来。丢掉这一行让用户重输，绝不因为一次误删
            # 就把整个会话带走。
            print(
                "warning: could not decode that input (broken multi-byte sequence); please retype it.",
                file=sys.stderr,
            )
            continue

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory.render_memory_details())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reload":
            result = agent.reload_registry()
            changed = result["added"] + result["removed"] + result["tools_added"] + result["tools_removed"]
            print("tools and skills reloaded" + (f" ({', '.join(changed)})" if changed else ""))
            continue
        if user_input.split(" ")[0] in ("/rewind", "/rewind!"):
            command, _, argument = user_input.partition(" ")
            try:
                steps = int(argument.strip() or "1")
            except ValueError:
                print("usage: /rewind [n]", file=sys.stderr)
                continue
            print(rewindlib.render_rewind(agent.rewind(steps=steps, force=command.endswith("!"))))
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            answer = agent.ask(user_input)
        except KeyboardInterrupt:
            # Ctrl-C 只取消当前这一轮任务，回到提示符，而不是退出整个程序。
            progress.clear()
            print("\n(cancelled)", file=sys.stderr)
            continue
        except RuntimeError as exc:
            progress.clear()
            print(str(exc), file=sys.stderr)
            continue
        progress.clear()
        print(answer)

__all__ = [
    "build_agent",
    "build_arg_parser",
    "build_mcp_arg_parser",
    "build_memory_arg_parser",
    "build_runs_arg_parser",
    "build_welcome",
    "enable_line_editing",
    "main",
    "run_mcp_command",
    "run_memory_command",
    "run_runs_command",
    "_ctype_codeset",
]
