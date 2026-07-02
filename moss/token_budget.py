"""Token 计量与按预算截断。

为什么存在：
字符数不是模型真正的约束——上下文窗口和费用都按 token 计。而且 CJK
（中文、日文、韩文、全角符号）在主流 BPE 分词里的 token 密度远高于
英文：英文经验值约 4 字符/token，中文接近 1 字符/token。用字符数当预算，
会系统性低估中文内容的真实开销。

这个模块提供两样东西：
- `estimate_tokens`：一个便宜、零依赖、对 CJK 友好的 token 估算；
- `clip_to_budget`：按“某种计量的预算”而不是死板的字符数截断文本，
  并且可以选择保留开头 / 结尾 / 两端。
"""

from __future__ import annotations

# CJK 及全角区段：这些字符在主流分词里通常 1 字符 >= 1 token。
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0xA000, 0xA4CF),   # 彝文音节等
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFF00, 0xFFEF),   # 全角与半角变体
)

# 英文/拉丁：经验值约 4 字符 ≈ 1 token。
_LATIN_CHARS_PER_TOKEN = 4


def _is_cjk(ch):
    code = ord(ch)
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def estimate_tokens(text):
    """估算文本的 token 数。

    刻意保持简单、零依赖：CJK/全角字符按 1 token 计，其余字符按约
    4 字符/token 计。它不追求和某个具体分词器逐 token 对齐，只求比
    “字符数”更接近真实成本——尤其在中英混排时不再系统性低估中文。
    非空文本至少记 1 token。
    """
    text = str(text)
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    latin_tokens = (other + _LATIN_CHARS_PER_TOKEN - 1) // _LATIN_CHARS_PER_TOKEN
    return max(1, cjk + latin_tokens)


def clip_to_budget(text, limit, measure=len, keep="head", marker="..."):
    """把 text 截断到 `measure(result) <= limit`，可选择保留哪一端。

    keep="head"   → 保留开头（读代码时 import/签名在前，信息在头部）。
    keep="tail"   → 保留结尾（报错/stack trace 的关键信息通常在末尾）。
    keep="middle" → 两端都留、砍中间（shell 输出的 exit_code 在顶部、
                    stderr 在底部，两端都值得保留）。

    `measure` 是计量函数：传 `len` 就是按字符预算，传 `estimate_tokens`
    就是按 token 预算。因为 `measure(candidate)` 对“保留的字符数”单调
    不减，这里用二分找出能塞进预算的最大截断。
    """
    text = str(text)
    if limit <= 0:
        return ""
    if measure(text) <= limit:
        return text
    if measure(marker) >= limit:
        # 连截断标记本身都放不下，就退回无标记的硬截断。
        marker = ""
    if keep not in ("head", "tail", "middle"):
        keep = "head"

    def candidate(keep_chars):
        if keep_chars <= 0:
            return marker
        if keep == "tail":
            return marker + text[-keep_chars:]
        if keep == "middle":
            left = keep_chars // 2
            right = keep_chars - left
            tail = text[-right:] if right else ""
            return text[:left] + marker + tail
        return text[:keep_chars] + marker

    lo, hi = 0, len(text)
    best = candidate(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = candidate(mid)
        if measure(cand) <= limit:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


# ---- 字符级硬裁剪（不带 token 估算的快路径） ----
# clip/middle 和上面的 clip_to_budget 语义不同：clip_to_budget 按计量函数
# 二分裁剪、保证结果 <= limit；clip 是廉价硬切片，结果为 limit 个字符再拼一条
# 截断说明（会略超 limit）。工具输出裁剪走 clip（性能敏感、每轮多次调用），
# prompt 预算走 clip_to_budget。暂不合并，合并会改变两边的既有契约。
MAX_TOOL_OUTPUT = 16000
MAX_HISTORY = 32000


def clip(text, limit=MAX_TOOL_OUTPUT, keep="head"):
    """把工具原始输出压到硬字符上限内。

    `keep` 决定保留哪一端，因为不同工具的关键信息位置不同：
    - "head"   → 保留开头（读文件、列目录：信息在前）。
    - "tail"   → 保留结尾（报错/日志：关键信息在末尾）。
    - "middle" → 两端都留、砍中间（shell：exit_code 在顶部、stderr 在底部）。
    """
    text = str(text)
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    note = f"[truncated {cut} chars]"
    if keep == "tail":
        return f"...{note}\n" + text[-limit:]
    if keep == "middle":
        left = limit // 2
        right = limit - left
        return text[:left] + f"\n...{note}...\n" + text[-right:]
    return text[:limit] + f"\n...{note}"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]
