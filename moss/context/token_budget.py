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

import json
import math
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..clock import now

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


# ---- token 估算的在线校准（spec-06 §4.4） ----
# 估算永远只是估算：同一段文本在不同 provider 的分词器下能差 30%。
# 与其去猜每家的分词规则，不如把"我估了多少 / 后端说是多少"记下来，
# 用滑动中位数校正下一轮的预算。

CALIBRATION_SCHEMA_VERSION = 1
# 样本不够就不动比例。少数几条样本的中位数噪声太大，
# 拿它去缩预算比不校准更危险。
CALIBRATION_MIN_SAMPLES = 5
CALIBRATION_SAMPLE_LIMIT = 50
# 偏差超过这个幅度就认为"不是估算不准，而是哪里对不上了"（比如后端把
# 工具定义也算进 input_tokens）。这时候告警并退回 1.0，不让校准把估算带偏。
CALIBRATION_DRIFT_THRESHOLD = 0.3


@dataclass(frozen=True)
class Calibration:
    provider: str = ""
    model: str = ""
    ratio: float = 1.0
    samples: int = 0
    updated_at: str = ""
    # 原始中位数（没有被 ±30% 闸门夹过的那个），用来解释告警。
    raw_ratio: float = 1.0
    drift: bool = False
    # 用 tiktoken 一类库拿到真值时为 True，此时 ratio 强制 1.0。
    exact: bool = False

    def to_dict(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "ratio": round(float(self.ratio), 4),
            "raw_ratio": round(float(self.raw_ratio), 4),
            "samples": int(self.samples),
            "updated_at": self.updated_at,
            "token_estimate_drift": bool(self.drift),
            "exact": bool(self.exact),
        }


def calibrated_measure(base_measure, calibration):
    """把估算函数按校准比例缩放。ratio 为 1.0 时原样返回，不加一层间接调用。"""
    ratio = float(getattr(calibration, "ratio", 1.0) or 1.0)
    if ratio == 1.0:
        return base_measure

    def measure(text):
        return int(math.ceil(base_measure(text) * ratio))

    return measure


def exact_token_counter(model=""):
    """探测到 tiktoken 就用真值。

    刻意用 import 探测而不是写进依赖：moss 的零第三方运行时依赖是硬约束，
    但用户环境里正好装了的话，没有理由不用更准的那个。
    """
    try:
        import tiktoken  # noqa: PLC0415 - 可选依赖，只能在运行时探测
    except Exception:
        return None
    try:
        encoding = tiktoken.encoding_for_model(str(model))
    except Exception:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None

    def measure(text):
        text = str(text)
        return len(encoding.encode(text)) if text else 0

    return measure


class TokenCalibrationStore:
    """`(provider, model)` 分桶的估算/真值样本，落在 `.moss/cache/` 里。

    放在 cache 目录是刻意的：它是可以随时删掉重建的派生数据，删了只是
    退回未校准状态，不会丢任何用户内容。
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": CALIBRATION_SCHEMA_VERSION, "buckets": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), dict):
            return {"schema_version": CALIBRATION_SCHEMA_VERSION, "buckets": {}}
        return payload

    def _save(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(self.path)

    @staticmethod
    def _bucket_key(provider, model):
        return f"{str(provider or '').strip()}/{str(model or '').strip()}"

    def record(self, provider, model, estimated, actual):
        """记一条样本并返回更新后的校准。估算或真值不可用时原样返回当前校准。"""
        try:
            estimated = int(estimated)
            actual = int(actual)
        except (TypeError, ValueError):
            return self.calibration(provider, model)
        if estimated <= 0 or actual <= 0:
            return self.calibration(provider, model)
        payload = self._load()
        bucket = payload["buckets"].setdefault(self._bucket_key(provider, model), {"samples": []})
        samples = [item for item in bucket.get("samples", []) if isinstance(item, (list, tuple)) and len(item) == 2]
        samples.append([estimated, actual])
        bucket["samples"] = samples[-CALIBRATION_SAMPLE_LIMIT:]
        bucket["updated_at"] = now()
        payload["schema_version"] = CALIBRATION_SCHEMA_VERSION
        try:
            self._save(payload)
        except OSError:
            # 校准是锦上添花，写不进去不该让这一轮失败。
            pass
        return self._from_bucket(provider, model, bucket)

    def calibration(self, provider, model):
        bucket = self._load()["buckets"].get(self._bucket_key(provider, model), {})
        return self._from_bucket(provider, model, bucket)

    @staticmethod
    def _from_bucket(provider, model, bucket):
        samples = [
            (int(item[0]), int(item[1]))
            for item in (bucket or {}).get("samples", [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        ]
        updated_at = str((bucket or {}).get("updated_at", ""))
        ratios = [actual / estimated for estimated, actual in samples if estimated > 0]
        if len(ratios) < CALIBRATION_MIN_SAMPLES:
            return Calibration(
                provider=str(provider or ""),
                model=str(model or ""),
                ratio=1.0,
                raw_ratio=statistics.median(ratios) if ratios else 1.0,
                samples=len(ratios),
                updated_at=updated_at,
            )
        raw_ratio = statistics.median(ratios)
        drift = abs(raw_ratio - 1.0) > CALIBRATION_DRIFT_THRESHOLD
        return Calibration(
            provider=str(provider or ""),
            model=str(model or ""),
            ratio=1.0 if drift else raw_ratio,
            raw_ratio=raw_ratio,
            samples=len(ratios),
            updated_at=updated_at,
            drift=drift,
        )


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]
