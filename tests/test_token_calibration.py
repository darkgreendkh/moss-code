"""token 估算的在线校准（spec-06 §4.4）。

估算永远只是估算：同一段文本在不同 provider 的分词器下能差 30%。
测试守的是"样本不够就别乱调、够了就收敛、偏得离谱就告警并退回 1.0"。
"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.context.token_budget import (
    CALIBRATION_MIN_SAMPLES,
    CALIBRATION_SAMPLE_LIMIT,
    Calibration,
    TokenCalibrationStore,
    calibrated_measure,
    estimate_tokens,
)


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def test_ratio_stays_one_until_there_are_enough_samples(tmp_path):
    store = TokenCalibrationStore(tmp_path / "token_calibration.json")

    for _ in range(CALIBRATION_MIN_SAMPLES - 1):
        calibration = store.record("deepseek", "deepseek-v4-pro", 1000, 1200)

    assert calibration.samples == CALIBRATION_MIN_SAMPLES - 1
    assert calibration.ratio == 1.0
    assert calibration.drift is False


def test_ratio_converges_once_samples_accumulate(tmp_path):
    store = TokenCalibrationStore(tmp_path / "token_calibration.json")

    for _ in range(CALIBRATION_MIN_SAMPLES):
        calibration = store.record("deepseek", "deepseek-v4-pro", 1000, 1200)

    assert calibration.samples == CALIBRATION_MIN_SAMPLES
    assert abs(calibration.ratio - 1.2) < 1e-9
    assert calibration.drift is False

    measure = calibrated_measure(estimate_tokens, calibration)
    text = "hello world " * 200
    assert measure(text) > estimate_tokens(text)
    assert measure(text) == int(estimate_tokens(text) * 1.2) + (1 if estimate_tokens(text) * 1.2 % 1 else 0)


def test_large_drift_warns_and_falls_back_to_one(tmp_path):
    store = TokenCalibrationStore(tmp_path / "token_calibration.json")

    for _ in range(CALIBRATION_MIN_SAMPLES + 2):
        calibration = store.record("openai", "gpt-5-5", 1000, 2500)

    assert calibration.drift is True
    assert calibration.ratio == 1.0
    assert abs(calibration.raw_ratio - 2.5) < 1e-9
    assert calibration.to_dict()["token_estimate_drift"] is True


def test_samples_are_bucketed_per_provider_model_and_capped(tmp_path):
    store = TokenCalibrationStore(tmp_path / "token_calibration.json")

    for _ in range(CALIBRATION_SAMPLE_LIMIT + 20):
        store.record("deepseek", "deepseek-v4-pro", 1000, 1100)
    store.record("ollama", "qwen3:8b", 1000, 900)

    deepseek = store.calibration("deepseek", "deepseek-v4-pro")
    ollama = store.calibration("ollama", "qwen3:8b")

    assert deepseek.samples == CALIBRATION_SAMPLE_LIMIT
    assert abs(deepseek.ratio - 1.1) < 1e-9
    assert ollama.samples == 1
    assert ollama.ratio == 1.0


def test_zero_and_broken_samples_are_ignored(tmp_path):
    store = TokenCalibrationStore(tmp_path / "token_calibration.json")

    store.record("deepseek", "deepseek-v4-pro", 0, 100)
    store.record("deepseek", "deepseek-v4-pro", 100, 0)
    store.record("deepseek", "deepseek-v4-pro", "nope", None)

    assert store.calibration("deepseek", "deepseek-v4-pro").samples == 0


def test_corrupt_calibration_file_degrades_to_no_calibration(tmp_path):
    path = tmp_path / "token_calibration.json"
    path.write_text("{not json", encoding="utf-8")
    store = TokenCalibrationStore(path)

    assert store.calibration("deepseek", "deepseek-v4-pro").ratio == 1.0
    assert store.record("deepseek", "deepseek-v4-pro", 100, 120).samples == 1


def test_calibrated_measure_is_identity_at_ratio_one():
    measure = calibrated_measure(estimate_tokens, Calibration())

    assert measure is estimate_tokens


def test_runtime_records_samples_only_from_real_usage(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.model_client.last_completion_metadata = {"input_tokens": 4321, "output_tokens": 12}

    agent.ask("hello")

    calibration = agent.token_calibration_store.calibration("fake", "fake")
    assert calibration.samples == 1

    # 没有真实 usage 的那一轮不该产生样本：拿自己的估算当真值只会固化偏差。
    agent.model_client.outputs.append("<final>done again</final>")
    agent.model_client.last_completion_metadata = {}
    agent.ask("hello again")
    assert agent.token_calibration_store.calibration("fake", "fake").samples == 1


def test_calibration_reaches_the_report_and_the_next_budget(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    for _ in range(CALIBRATION_MIN_SAMPLES):
        agent.token_calibration_store.record("fake", "fake", 1000, 1250)
    agent.token_calibration = agent.token_calibration_store.calibration("fake", "fake")
    agent.context_manager.measure = agent.token_measure()
    agent.model_client.last_completion_metadata = {"input_tokens": 100, "output_tokens": 5}

    text = "budget calibration " * 50
    assert agent.context_manager.measure(text) > estimate_tokens(text)

    agent.ask("hello")
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["token_calibration"]["samples"] >= CALIBRATION_MIN_SAMPLES
    assert report["token_calibration"]["token_estimate_drift"] is False
