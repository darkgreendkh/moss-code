"""CLI runtime object composition and provider selection."""

import argparse
from datetime import datetime
import os
import uuid

from ..config import load_project_env, provider_env
from ..context.prefix import PROMPT_VERSION
from ..context.repository.workspace import WorkspaceContext
from ..execution.safety import policy as policylib
from ..providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from ..providers.recording import RecordingModelClient, ReplayModelClient
from ..runs import checkpoint as checkpointlib
from ..runs.session import SessionStore
from ..runtime import Moss

DEFAULT_SECRET_ENV_NAMES = (
    "MOSS_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "MOSS_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "MOSS_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

DEFAULT_OLLAMA_MODEL = "qwen3:8b"

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

DEFAULT_OPENAI_MODEL = "gpt-5-5"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"

DEFAULT_PROVIDER = "deepseek"

PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")

SECRET_ENV_NAMES_VAR = "MOSS_SECRET_ENV_NAMES"


def _effective_provider(args):
    # Provider 选择优先级：
    # 1. 用户显式传入 --provider
    # 2. 项目 .env / shell 里的 MOSS_PROVIDER
    # 3. 代码里的默认 provider
    provider = getattr(args, "provider", None) or provider_env(
        "MOSS_PROVIDER", default=DEFAULT_PROVIDER
    )
    if provider not in PROVIDER_CHOICES:
        choices = ", ".join(PROVIDER_CHOICES)
        raise ValueError(f"unknown provider: {provider}. expected one of: {choices}")
    return provider


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("MOSS_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("MOSS_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("MOSS_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = _effective_provider(args)
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        # 只回落到本 provider 自己的 key：base URL 指向官方 endpoint 后，
        # 拿别家的 key 去请求必定 401，跨 provider 回落只会把错误藏成"认证失败"。
        api_key = provider_env("MOSS_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="openai",
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env("MOSS_ANTHROPIC_API_KEY", ("ANTHROPIC_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="anthropic",
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("MOSS_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="deepseek",
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def _build_aux_model_client(args):
    """按 `--aux-provider` / `--aux-model` 装配脏活后端。

    两个都不给就返回 None，路由器把所有脏活回落主模型——那是消融基线，
    行为与加路由前逐字节一致。
    """
    aux_model = getattr(args, "aux_model", None)
    aux_provider = getattr(args, "aux_provider", None)
    if not aux_model and not aux_provider:
        return None
    # 复用主 client 的装配路径：provider 选择、base URL、key 回落的规则只有一份。
    aux_args = argparse.Namespace(**vars(args))
    aux_args.provider = aux_provider or _effective_provider(args)
    aux_args.model = aux_model
    # aux 的 base_url 不该继承主 provider 的：换了 provider 还指着老 endpoint 必定 404。
    if aux_provider and aux_provider != _effective_provider(args):
        aux_args.base_url = None
    # 录制回放只包主线。脏活的输出不进主线 history，录进磁带只会污染指纹。
    aux_args.record = None
    aux_args.replay = None
    return _build_model_client(aux_args)


def _wrap_cassette_client(model, args, workspace, secret_env_names):
    """按 `--record` / `--replay` 把真实 client 包成录制或回放 client。

    两个都不给时原样返回——录制回放是可选设施，不该在默认路径上多一层包装。
    """
    replay_dir = getattr(args, "replay", None)
    record_dir = getattr(args, "record", None)
    if replay_dir and record_dir:
        raise ValueError("--record and --replay are mutually exclusive")
    if replay_dir:
        return ReplayModelClient(
            replay_dir,
            on_miss=getattr(args, "replay_on_miss", "fail"),
            # passthrough 要有真后端可落；其余策略下它只是个身份信息来源。
            inner=model,
            root=workspace.repo_root,
            miss_observer=None,
        )
    if record_dir:
        return RecordingModelClient(
            model,
            record_dir,
            root=workspace.repo_root,
            secret_env_names=secret_env_names,
            prompt_version=PROMPT_VERSION,
        )
    return model


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Moss 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Moss`，或一个从旧 session 恢复出来的 `Moss`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    policy = policylib.Policy.build(
        allow=policylib.parse_capability_rules(getattr(args, "allow_rules", [])),
        deny=policylib.parse_capability_rules(getattr(args, "deny_rules", [])),
    )
    allowed_network_hosts = tuple(
        host.strip() for host in str(getattr(args, "allowed_network_hosts", "") or "").split(",") if host.strip()
    )
    run_budget_limits = {
        "max_input_tokens": getattr(args, "max_input_tokens", None),
        "max_output_tokens": getattr(args, "max_output_tokens", None),
        "max_wall_clock_s": getattr(args, "max_seconds", None),
        "max_usd": getattr(args, "max_usd", None),
    }
    feature_flags = {
        "prompt_cache": not bool(getattr(args, "no_prompt_cache", False)),
    }
    tool_protocol = getattr(args, "tool_protocol", "auto")
    context_mode = getattr(args, "context_mode", "rerender")
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.moss/sessions")
    model = _wrap_cassette_client(_build_model_client(args), args, workspace, configured_secret_names)
    aux_model = _build_aux_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        session = store.load(session_id)
        fork_from = getattr(args, "fork", None)
        if fork_from:
            # 分叉出的新会话独立落盘，原会话一个字节都不动 —— 分叉的意义就是
            # "再试一条路"，把原来那条弄坏了就白分叉了。
            session = checkpointlib.fork_session(
                session, fork_from, datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            )
        session = checkpointlib.apply_resume_parts(
            session, checkpointlib.parse_resume_parts(getattr(args, "resume_parts", "all"))
        )
        return Moss(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session=session,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            parallel_tools=getattr(args, "parallel_tools", "off") == "on",
            run_budget_limits=run_budget_limits,
            verify_before_final=getattr(args, "verify_before_final", "on") == "on",
            injection_scan=getattr(args, "injection_scan", "on") == "on",
            policy=policy,
            sandbox=getattr(args, "sandbox", "auto"),
            allowed_network_hosts=allowed_network_hosts,
            feature_flags=feature_flags,
            tool_protocol=tool_protocol,
            context_mode=context_mode,
            reflect_mode=getattr(args, "reflect", "rule"),
            compaction_mode=getattr(args, "compaction", "off"),
            aux_model_client=aux_model,
            code_mode=bool(getattr(args, "enable_code_mode", False)),
        )
    return Moss(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        parallel_tools=getattr(args, "parallel_tools", "off") == "on",
        run_budget_limits=run_budget_limits,
        verify_before_final=getattr(args, "verify_before_final", "on") == "on",
        injection_scan=getattr(args, "injection_scan", "on") == "on",
        policy=policy,
        sandbox=getattr(args, "sandbox", "auto"),
        allowed_network_hosts=allowed_network_hosts,
        feature_flags=feature_flags,
        tool_protocol=tool_protocol,
        context_mode=context_mode,
        reflect_mode=getattr(args, "reflect", "rule"),
        compaction_mode=getattr(args, "compaction", "off"),
        aux_model_client=aux_model,
        code_mode=bool(getattr(args, "enable_code_mode", False)),
    )

