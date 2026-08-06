"""Blinded optional judge scoring with human-gold calibration guardrails."""

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path


_BLIND_KEYS = {"variant", "provider", "model", "source", "run_id", "task_id"}


@dataclass(frozen=True)
class JudgeVerdict:
    score: float
    rubric_hits: tuple[str, ...]
    rationale: str
    judge_model: str
    judge_prompt_sha: str

    @classmethod
    def from_mapping(cls, value):
        value = dict(value or {})
        score = float(value.get("score", -1))
        if not 0 <= score <= 1:
            raise ValueError("judge score must be between zero and one")
        return cls(
            score=score,
            rubric_hits=tuple(str(item) for item in value.get("rubric_hits") or ()),
            rationale=str(value.get("rationale") or ""),
            judge_model=str(value.get("judge_model") or ""),
            judge_prompt_sha=str(value.get("judge_prompt_sha") or ""),
        )


def _blind(value):
    if isinstance(value, dict):
        return {key: _blind(nested) for key, nested in value.items() if key not in _BLIND_KEYS}
    if isinstance(value, list):
        return [_blind(item) for item in value]
    return value


def build_blinded_judge_prompt(*, task, trace_summary, final_answer, reference_answer=None):
    task = dict(task or {})
    payload = {
        "instruction": (
            "Score only subjective rubric coverage from 0 to 1. "
            "Do not decide deterministic binary pass/fail."
        ),
        "rubric": task.get("rubric"),
        "task": _blind(task),
        "trace_summary": _blind(trace_summary),
        "final_answer": str(final_answer or ""),
        "reference_answer": reference_answer,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def combine_verifier_and_judge(verifier_passed, verdict, *, calibrated):
    if not isinstance(verdict, JudgeVerdict):
        verdict = JudgeVerdict.from_mapping(verdict)
    return {
        "binary_pass": bool(verifier_passed),
        "partial_score": verdict.score,
        "judge_calibrated": bool(calibrated),
        "needs_human_review": not calibrated or 0.0 < verdict.score < 1.0,
        "judge": asdict(verdict),
    }


def cohen_kappa(human_labels, judge_labels):
    human = list(human_labels)
    judge = list(judge_labels)
    if not human or len(human) != len(judge):
        raise ValueError("kappa requires equal non-empty label sequences")
    categories = set(human) | set(judge)
    observed = sum(left == right for left, right in zip(human, judge)) / len(human)
    expected = sum(
        (human.count(category) / len(human)) * (judge.count(category) / len(judge))
        for category in categories
    )
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def _correlation(left, right):
    left = [float(value) for value in left]
    right = [float(value) for value in right]
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    if not denominator:
        return 1.0 if left == right else 0.0
    return numerator / denominator


def calibrate_judge(rows, *, judge_model, judge_prompt_sha, kappa_threshold=0.7):
    rows = [dict(row) for row in rows]
    completed = [row for row in rows if "human_label" in row and "judge_label" in row]
    if len(completed) < 50:
        return {
            "status": "insufficient_gold",
            "n": len(completed),
            "required": 50,
            "judge_model": str(judge_model),
            "judge_prompt_sha": str(judge_prompt_sha),
            "calibration_sha": None,
        }
    human = [row["human_label"] for row in completed]
    judged = [row["judge_label"] for row in completed]
    kappa = cohen_kappa(human, judged)
    correlation = _correlation(human, judged)
    calibration_payload = json.dumps(completed, sort_keys=True, separators=(",", ":"))
    return {
        "status": "calibrated" if kappa >= float(kappa_threshold) else "uncalibrated",
        "n": len(completed),
        "kappa": kappa,
        "correlation": correlation,
        "kappa_threshold": float(kappa_threshold),
        "judge_model": str(judge_model),
        "judge_prompt_sha": str(judge_prompt_sha),
        "calibration_sha": "sha256:" + hashlib.sha256(calibration_payload.encode("utf-8")).hexdigest(),
    }


def judge_budget_guard(*, capability_usd, judge_usd, max_share=0.15):
    capability_usd = float(capability_usd)
    judge_usd = float(judge_usd)
    total = capability_usd + judge_usd
    share = judge_usd / total if total else 0.0
    return {
        "judge_cost_share": share,
        "max_share": float(max_share),
        "within_guardrail": share <= float(max_share),
        "action": "keep" if share <= float(max_share) else "downsample",
    }


def load_gold_slots(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    slots = list(payload.get("slots") or ())
    if payload.get("schema_version") != 1 or len(slots) != 50:
        raise ValueError("judge gold scaffold must contain exactly 50 slots")
    if len({slot.get("slot_id") for slot in slots}) != 50:
        raise ValueError("judge gold slot IDs must be unique")
    return slots
