"""Tests for tgdatabridge.cli.runner's optional AI error-diagnosis hook
(_print_ai_explanation, wired into the Load Schema / Apply DDL / Migrate
Data failure branches) -- reuses tests/test_cli_runner.py's own fakes and
helpers rather than duplicating them."""
import os

import tgdatabridge.cli.runner as runner
from tests.test_cli_runner import SRC_PW_VAR, TGT_PW_VAR, _job_raw, _patched_introspector, _schema_with_one_table
from tgdatabridge.ai.ai_client import AiError
from tgdatabridge.ai.error_diagnostics import Diagnosis
from tgdatabridge.cli.config import parse_job_config

AI_KEY_VAR = "TGSCT_TEST_CLI_AI_KEY"


def _with_env(fn):
    os.environ[SRC_PW_VAR] = "srcpw"
    os.environ[TGT_PW_VAR] = "tgtpw"
    os.environ[AI_KEY_VAR] = "sk-test"
    try:
        fn()
    finally:
        for var in (SRC_PW_VAR, TGT_PW_VAR, AI_KEY_VAR):
            os.environ.pop(var, None)


def _run_with_failing_source(config, schema):
    """Like test_cli_runner.py's own _run, but Load Schema always fails --
    for exercising the failure branch's AI explanation hook."""
    lines = []

    class _ExplodingConn:
        def connect(self):
            raise RuntimeError("simulated connection failure")

        def close(self):
            pass

    original = runner.introspector_for
    runner.introspector_for = _patched_introspector(schema)
    try:
        code = runner.run_job(
            config, print_fn=lines.append,
            make_source_connector=lambda engine, params: _ExplodingConn(),
            make_target_connector=lambda engine, params: _ExplodingConn(),
        )
    finally:
        runner.introspector_for = original
    return code, lines


def test_no_ai_block_prints_nothing_extra_on_failure():
    def body():
        raw = _job_raw()
        config = parse_job_config(raw)
        schema, _rows = _schema_with_one_table()
        code, lines = _run_with_failing_source(config, schema)
        assert code == 1
        assert not any("AI explanation" in line for line in lines)

    _with_env(body)


def test_an_enabled_ai_block_prints_an_explanation_on_load_schema_failure(monkeypatch):
    def fake_explain_error(client, error_text, context=""):
        assert "simulated connection failure" in error_text
        assert context == "Load Schema"
        return Diagnosis(explanation="The credentials look wrong.", suggested_fixes=["Check the password."])

    monkeypatch.setattr("tgdatabridge.ai.error_diagnostics.explain_error", fake_explain_error)

    def body():
        raw = _job_raw(ai={"enabled": True, "api_key_env": AI_KEY_VAR})
        config = parse_job_config(raw)
        schema, _rows = _schema_with_one_table()
        code, lines = _run_with_failing_source(config, schema)
        assert code == 1
        assert any("The credentials look wrong." in line for line in lines)
        assert any("Check the password." in line for line in lines)

    _with_env(body)


def test_an_ai_error_during_diagnosis_does_not_change_the_exit_code(monkeypatch):
    def raiser(client, error_text, context=""):
        raise AiError("the AI service is unreachable")

    monkeypatch.setattr("tgdatabridge.ai.error_diagnostics.explain_error", raiser)

    def body():
        raw = _job_raw(ai={"enabled": True, "api_key_env": AI_KEY_VAR})
        config = parse_job_config(raw)
        schema, _rows = _schema_with_one_table()
        code, lines = _run_with_failing_source(config, schema)
        assert code == 1  # unchanged -- the AI explanation is best-effort only
        assert any("unreachable" in line for line in lines)

    _with_env(body)


def test_disabled_error_diagnostics_flag_prints_nothing():
    def body():
        raw = _job_raw(ai={"enabled": True, "api_key_env": AI_KEY_VAR, "error_diagnostics": False})
        config = parse_job_config(raw)
        schema, _rows = _schema_with_one_table()
        code, lines = _run_with_failing_source(config, schema)
        assert code == 1
        assert not any("AI explanation" in line for line in lines)

    _with_env(body)


def test_a_missing_api_key_env_is_reported_without_crashing_the_run():
    def body():
        raw = _job_raw(ai={"enabled": True, "api_key_env": "TOTALLY_UNSET_VAR"})
        config = parse_job_config(raw)
        schema, _rows = _schema_with_one_table()
        code, lines = _run_with_failing_source(config, schema)
        assert code == 1
        assert any("AI explanation skipped" in line for line in lines)

    _with_env(body)
