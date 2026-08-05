"""仓库地图：目录骨架 + 符号索引。

为什么存在：模型对“这个仓库长什么样”的全部认知原本只有 `git status` 和几份
被砍到 1200 字符的文档，于是每个任务的前 3–5 步都花在 list_files → read_file
认路上。这个模块把“认路”从工具步数换成 prompt 里的几百 token。

刻意的技术选择：不引入 tree-sitter / LSP / embedding。Python 走 stdlib `ast`，
其它语言走行首前缀匹配——认路只需要“这个文件里大致有什么”，不需要精确 AST。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .clock import now
from .token_budget import estimate_tokens

# 缓存里的 schema 版本。改了抽取口径或渲染格式就必须改它，
# 否则老缓存会被当成有效结果直接返回。
REPO_MAP_SCHEMA_VERSION = "repo-map-v1"

DEFAULT_MAX_DEPTH = 3
DEFAULT_PER_DIR_LIMIT = 20
DEFAULT_BUDGET_TOKENS = 800
# 目录骨架最多吃掉多少预算。剩下的留给符号索引——两者缺一，地图就只剩一半价值。
TREE_BUDGET_SHARE = 0.6

# 超过这个大小就不抽符号（仍然进目录树）：巨型文件解析慢，
# 而且它们的符号列表往往长到根本进不了预算。
MAX_SYMBOL_FILE_BYTES = 1_000_000
# 二进制探测只看前 1KB：读整份文件只为判类型是纯浪费。
BINARY_PROBE_BYTES = 1024

# 入口文件优先展示：模型认路时第一件事就是找入口。
ENTRY_FILE_NAMES = ("__main__.py", "cli.py", "main.py", "main.go", "main.rs", "index.js", "index.ts")

# 非 Python 语言的“定义行”开头，和 features/memory.py 的签名口径保持一致。
_SIGNATURE_PREFIXES = (
    "func ", "fn ", "function ",
    "public ", "private ", "protected ", "static ",
    "interface ", "struct ", "trait ", "impl ", "enum ", "type ",
    "export ", "module ", "package ", "class ", "def ",
)
_SIGNATURE_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[\(\{:=<]?")

# 只对这些扩展名尝试抽符号，避免把 .json/.lock 之类的当代码扫。
_TEXT_CODE_SUFFIXES = {
    ".py", ".pyi", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx",
    ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".swift", ".sh",
}


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line_start: int
    line_end: int

    def to_dict(self):
        return {"name": self.name, "kind": self.kind, "start": self.line_start, "end": self.line_end}

    @classmethod
    def from_dict(cls, payload):
        return cls(
            name=str(payload.get("name", "")),
            kind=str(payload.get("kind", "other")),
            line_start=int(payload.get("start", 0)),
            line_end=int(payload.get("end", 0)),
        )


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    mtime_ns: int
    symbols: tuple = ()

    def to_dict(self):
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
        }

    @classmethod
    def from_dict(cls, payload):
        return cls(
            path=str(payload.get("path", "")),
            size=int(payload.get("size", 0)),
            mtime_ns=int(payload.get("mtime_ns", 0)),
            symbols=tuple(Symbol.from_dict(item) for item in payload.get("symbols", [])),
        )


@dataclass(frozen=True)
class RepoMap:
    entries: tuple = ()
    tree_text: str = ""
    cache_key: str = ""
    built_at: str = ""
    truncated: bool = False

    def to_dict(self):
        return {
            "schema_version": REPO_MAP_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
            "tree_text": self.tree_text,
            "cache_key": self.cache_key,
            "built_at": self.built_at,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, payload):
        return cls(
            entries=tuple(FileEntry.from_dict(item) for item in payload.get("entries", [])),
            tree_text=str(payload.get("tree_text", "")),
            cache_key=str(payload.get("cache_key", "")),
            built_at=str(payload.get("built_at", "")),
            truncated=bool(payload.get("truncated", False)),
        )


def _is_binary(path):
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(BINARY_PROBE_BYTES)
    except OSError:
        return True


def _python_symbols(source):
    """用 stdlib ast 抽顶层定义和类内一层方法。

    语法错误的文件直接返回空列表：仓库里存在写坏的 .py 是常态
    （模型自己刚写坏的更常见），地图不该因此抛异常。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    symbols = []
    docstring = ast.get_docstring(tree)
    if docstring:
        first_line = docstring.strip().splitlines()[0].strip()
        if first_line:
            symbols.append(Symbol(name=first_line, kind="module", line_start=1, line_end=1))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                Symbol(node.name, "class", node.lineno, getattr(node, "end_lineno", node.lineno))
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                    symbols.append(
                        Symbol(
                            f"{node.name}.{child.name}",
                            kind,
                            child.lineno,
                            getattr(child, "end_lineno", child.lineno),
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            symbols.append(
                Symbol(node.name, kind, node.lineno, getattr(node, "end_lineno", node.lineno))
            )
    return tuple(symbols)


def _generic_symbols(source):
    """其它语言：行首前缀匹配。够用就好，不追求语法正确性。"""
    symbols = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(_SIGNATURE_PREFIXES):
            continue
        remainder = stripped
        for prefix in _SIGNATURE_PREFIXES:
            if remainder.startswith(prefix):
                remainder = remainder[len(prefix) :]
                break
        match = _SIGNATURE_NAME.match(remainder.strip())
        name = match.group(1) if match else remainder.strip()[:40]
        if not name:
            continue
        symbols.append(Symbol(name=name, kind="other", line_start=lineno, line_end=lineno))
    return tuple(symbols)


def extract_symbols(path):
    suffix = Path(path).suffix.lower()
    if suffix not in _TEXT_CODE_SUFFIXES:
        return ()
    try:
        if path.stat().st_size > MAX_SYMBOL_FILE_BYTES or _is_binary(path):
            return ()
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    if suffix in {".py", ".pyi"}:
        return _python_symbols(source)
    return _generic_symbols(source)


def _recency_weight(mtime_ns, reference_s):
    days = max(0.0, (reference_s - mtime_ns / 1e9) / 86400.0)
    return math.exp(-days / 30.0)


def _rank_key(entry, reference_s):
    """决定预算不够时先保留谁：入口文件 > 大且新 > 路径字典序。

    第三项不是可有可无的：没有它，两次构建的输出可能因为浮点打平而顺序不同，
    地图进的是 prompt 的稳定段，抖动会白白打掉缓存。
    """
    name = entry.path.rsplit("/", 1)[-1]
    is_entry = 0 if name in ENTRY_FILE_NAMES else 1
    score = entry.size * _recency_weight(entry.mtime_ns, reference_s)
    return (is_entry, -score, entry.path)


def _collect_entries(root, ignore, max_depth):
    entries = []
    directories = {}
    root = Path(root)
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except (OSError, PermissionError):
            continue
        for child in children:
            try:
                rel = child.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                if child.is_symlink():
                    continue
                is_dir = child.is_dir()
            except OSError:
                continue
            if ignore is not None and ignore.match(rel, is_dir=is_dir):
                continue
            if is_dir:
                if depth + 1 <= max_depth:
                    stack.append((child, depth + 1))
                directories.setdefault(rel, 0)
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            parent = child.parent.relative_to(root).as_posix()
            parent = "" if parent == "." else parent
            directories[parent] = directories.get(parent, 0) + 1
            entries.append(
                FileEntry(
                    path=rel,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    symbols=extract_symbols(child),
                )
            )
    return entries, directories


def _render_tree(entries, directories, per_dir_limit):
    """渲染目录骨架。按目录分组，每个目录最多列 per_dir_limit 个文件。"""
    grouped = {}
    for entry in entries:
        parent, _, name = entry.path.rpartition("/")
        grouped.setdefault(parent, []).append(name)
    lines = []
    for parent in sorted(set(grouped) | set(directories)):
        names = sorted(grouped.get(parent, []))
        label = parent + "/" if parent else "./"
        shown = names[:per_dir_limit]
        suffix = f" … +{len(names) - len(shown)}" if len(names) > len(shown) else ""
        if not shown:
            lines.append(f"{label}")
            continue
        lines.append(f"{label} {' '.join(shown)}{suffix}")
    return "\n".join(lines)


def build_repo_map(
    root,
    *,
    max_depth=DEFAULT_MAX_DEPTH,
    per_dir_limit=DEFAULT_PER_DIR_LIMIT,
    budget_tokens=DEFAULT_BUDGET_TOKENS,
    ignore=None,
    cache_key="",
):
    entries, directories = _collect_entries(root, ignore, max_depth)
    reference_s = time.time()
    entries.sort(key=lambda entry: _rank_key(entry, reference_s))
    tree_text = _render_tree(entries, directories, per_dir_limit)
    repo_map = RepoMap(
        entries=tuple(entries),
        tree_text=tree_text,
        cache_key=cache_key,
        built_at=now(),
        truncated=False,
    )
    # truncated 记的是“渲染时装不下”，构建阶段先按预算试渲染一次拿到结论，
    # 这样调用方不必自己再判断一遍。
    return RepoMap(
        entries=repo_map.entries,
        tree_text=tree_text,
        cache_key=cache_key,
        built_at=repo_map.built_at,
        truncated=_render(repo_map, budget_tokens)[1],
    )


def render_repo_map(repo_map, budget_tokens=DEFAULT_BUDGET_TOKENS):
    """按预算渲染：目录骨架优先，但它最多只能吃掉 TREE_BUDGET_SHARE。

    顺序不能反——没有骨架的符号列表对认路毫无帮助。但骨架也不能独占预算：
    在文件多的仓库里它会把整份预算吃光，符号索引一条都进不去，
    地图就退化成了一份 `ls -R`。
    """
    return _render(repo_map, budget_tokens)[0]


def _render(repo_map, budget_tokens):
    """返回 (文本, 是否有内容被预算挤掉)。

    truncated 必须由渲染过程自己报告：按渲染后的 token 数反推会漏判——
    行粒度截断经常在离预算还差几十 token 的地方就停下了。
    """
    if not repo_map.entries and not repo_map.tree_text:
        return "", False
    truncated = False
    header = "Repo map:"
    lines = [header]
    used = estimate_tokens(header)
    tree_budget = max(1, int(budget_tokens * TREE_BUDGET_SHARE))
    tree_lines = repo_map.tree_text.splitlines()
    for index, line in enumerate(tree_lines):
        cost = estimate_tokens(line)
        if used + cost > tree_budget:
            lines.append(f"… +{len(tree_lines) - index} more directories")
            used += estimate_tokens(lines[-1])
            truncated = True
            break
        lines.append(line)
        used += cost
    documented = [entry for entry in repo_map.entries if entry.symbols]
    for index, entry in enumerate(documented):
        names = ", ".join(f"{symbol.name}:{symbol.line_start}" for symbol in entry.symbols[:12])
        line = f"- {entry.path}: {names}"
        cost = estimate_tokens(line)
        if used + cost > budget_tokens:
            lines.append(f"… +{len(documented) - index} more files")
            truncated = True
            break
        if len(entry.symbols) > 12:
            truncated = True
        lines.append(line)
        used += cost
    return "\n".join(lines), truncated


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# 这些词在任何请求里都会出现，留着只会把打分拉平。
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "add", "fix", "update", "make", "please", "file", "code", "test", "tests", "py",
    }
)


def tokenize(text):
    """把路径 / 符号名 / 用户请求切成可比对的小写词。

    snake_case 和 camelCase 都要拆开：模型说的是 “repo map”，
    仓库里叫 `repo_map.py` 和 `build_repo_map`，不拆就永远对不上。
    """
    tokens = []
    for chunk in _TOKEN_SPLIT.split(str(text)):
        if not chunk:
            continue
        for part in _CAMEL_SPLIT.split(chunk):
            part = part.lower()
            if part and part not in _STOPWORDS and len(part) > 1:
                tokens.append(part)
    return tokens


def rank_relevant_files(repo_map, query, limit=5):
    """按词频给文件打分，返回最可能相关的路径。

    这是 spec-01 PR-6 的简化版起点锚：TODO 等 spec-05 的 BM25 落地后换成它，
    这里的接口保持不变。路径命中权重高于符号命中——文件名是人给的、
    信息密度最高，符号名里噪声更多。
    """
    query_tokens = set(tokenize(query))
    if not query_tokens or not repo_map or not repo_map.entries:
        return []
    scored = []
    for entry in repo_map.entries:
        path_tokens = tokenize(entry.path)
        symbol_tokens = []
        for symbol in entry.symbols:
            symbol_tokens.extend(tokenize(symbol.name))
        score = 3 * len(query_tokens & set(path_tokens)) + len(query_tokens & set(symbol_tokens))
        if score:
            scored.append((-score, entry.path))
    # 分数打平时按路径字典序：起点锚每轮都会重算，顺序抖动会让缓存白白失效。
    scored.sort()
    return [path for _, path in scored[:limit]]


def compute_cache_key(root, ignore_files=(), *, budget_tokens=DEFAULT_BUDGET_TOKENS, max_depth=DEFAULT_MAX_DEPTH):
    """缓存键：.git 的 HEAD/index、各顶层目录的 mtime、忽略规则内容、schema 版本。

    顶层目录的 mtime 能覆盖“新增/删除文件”这类结构变化；文件内容改动不进 key，
    地图记的是结构不是内容，为一次内容修改重建整张图不划算。
    """
    root = Path(root)
    parts = [REPO_MAP_SCHEMA_VERSION, str(budget_tokens), str(max_depth)]
    for relative in (".git/HEAD", ".git/index"):
        try:
            parts.append(str((root / relative).stat().st_mtime_ns))
        except OSError:
            parts.append("-")
    try:
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or child.is_symlink():
                continue
            # 跳过点目录：缓存自己就写在 .moss/cache 下，把它的 mtime 算进 key
            # 会导致“每写一次缓存就让缓存失效”。.git 另有 HEAD/index 两项覆盖。
            if child.name.startswith("."):
                continue
            try:
                parts.append(f"{child.name}:{child.stat().st_mtime_ns}")
            except OSError:
                continue
    except OSError:
        pass
    for ignore_file in ignore_files:
        try:
            parts.append(hashlib.sha256(Path(ignore_file).read_bytes()).hexdigest())
        except OSError:
            parts.append("-")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def load_cached_repo_map(cache_path, cache_key):
    try:
        payload = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("schema_version") != REPO_MAP_SCHEMA_VERSION:
        return None
    if str(payload.get("cache_key", "")) != cache_key:
        return None
    return RepoMap.from_dict(payload)


def save_cached_repo_map(cache_path, repo_map):
    cache_path = Path(cache_path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：断电/Ctrl-C 时留下半个 JSON，下次启动就得靠异常兜底。
        tmp_path = cache_path.with_name(cache_path.name + f".{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(repo_map.to_dict()), encoding="utf-8")
        os.replace(tmp_path, cache_path)
    except OSError:
        return False
    return True


def get_repo_map(root, *, ignore=None, budget_tokens=DEFAULT_BUDGET_TOKENS, max_depth=DEFAULT_MAX_DEPTH, cache_dir=None):
    """带缓存的入口：key 没变就直接反序列化，不重扫仓库。"""
    root = Path(root)
    cache_dir = Path(cache_dir) if cache_dir is not None else root / ".moss" / "cache"
    cache_path = cache_dir / "repo_map.json"
    cache_key = compute_cache_key(
        root, ignore_files=(root / ".gitignore",), budget_tokens=budget_tokens, max_depth=max_depth
    )
    cached = load_cached_repo_map(cache_path, cache_key)
    if cached is not None:
        return cached
    repo_map = build_repo_map(
        root, ignore=ignore, budget_tokens=budget_tokens, max_depth=max_depth, cache_key=cache_key
    )
    save_cached_repo_map(cache_path, repo_map)
    return repo_map
