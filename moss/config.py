"""Project-local configuration helpers."""

import json
import os
import re
import sys
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise ValueError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def load_project_env(start, override=True):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for lineno, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            parsed = _parse_env_line(line)
        except ValueError as exc:
            # 一行写错不应该让整个 agent 起不来。这是团队场景里非常真实的踩坑：
            # 某人往 .env 里粘了一段带空格或注释符的东西，结果所有人启动即崩。
            # 跳过这一行并给出定位信息，让其余合法配置照常生效。
            print(f"warning: skipping {env_path.name}:{lineno}: {exc}", file=sys.stderr)
            continue
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def load_project_config(root):
    """读 `.moss/config.json`。

    为什么和 `.env` 分开：`.env` 装的是密钥和开关（扁平字符串），
    这里装的是结构化配置（比如文档名单这种列表）。文件不存在或写坏了都返回空 dict——
    配置写错不该让 agent 起不来，这条和 `.env` 的坏行处理保持一致。
    """
    path = Path(root) / ".moss" / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring invalid {path}: {exc}", file=sys.stderr)
        return {}
    return payload if isinstance(payload, dict) else {}


def project_config_section(root, section):
    value = load_project_config(root).get(section)
    return value if isinstance(value, dict) else {}


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default
