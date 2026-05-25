"""response_envelope のテスト (v0.5.0-rc1)"""
from lab_executor.response_envelope import make_envelope, make_error, is_envelope


def test_envelope_ok_minimal():
    env = make_envelope("ok")
    assert env["status"] == "ok"
    assert env["data"] == {}
    assert env["errors"] == []
    assert "timestamp" in env["metadata"]
    assert is_envelope(env)


def test_envelope_with_data():
    env = make_envelope("ok", data={"job_id": "job_001"})
    assert env["data"]["job_id"] == "job_001"


def test_envelope_with_elapsed_and_job_id():
    env = make_envelope("ok", elapsed_s=1.234, job_id="job_001")
    assert env["metadata"]["elapsed_s"] == 1.234
    assert env["metadata"]["job_id"] == "job_001"


def test_envelope_error_with_errors():
    err = make_error("timeout", "device did not respond", instrument="GPIB0::1")
    env = make_envelope("error", errors=[err])
    assert env["status"] == "error"
    assert len(env["errors"]) == 1
    assert env["errors"][0]["error_class"] == "timeout"
    assert env["errors"][0]["instrument"] == "GPIB0::1"
    assert env["errors"][0]["recoverable"] is True


def test_envelope_partial_failure():
    env = make_envelope(
        "partial_failure",
        data={"summary": {"total": 100, "success": 98, "failed": 2}},
        errors=[
            make_error("timeout", "x", target_id="sample057"),
            make_error("hardware", "y", target_id="sample088", recoverable=False),
        ],
    )
    assert env["status"] == "partial_failure"
    assert env["data"]["summary"]["total"] == 100
    assert len(env["errors"]) == 2
    assert env["errors"][1]["recoverable"] is False


def test_make_error_with_recommended_actions():
    err = make_error(
        "timeout", "...",
        recommended_next_actions=[
            {"action": "retry", "tool": "retry_failed"},
        ],
    )
    assert err["recommended_next_actions"][0]["action"] == "retry"


def test_make_error_omits_none_fields():
    err = make_error("timeout", "x")
    assert "instrument" not in err
    assert "target_id" not in err
    assert "details" not in err


def test_is_envelope_rejects_non_dict():
    assert is_envelope("not a dict") is False
    assert is_envelope(None) is False
    assert is_envelope([]) is False


def test_is_envelope_rejects_missing_fields():
    assert is_envelope({"status": "ok"}) is False
    assert is_envelope({"status": "ok", "data": {}, "errors": []}) is False  # no metadata


def test_is_envelope_rejects_invalid_status():
    env = make_envelope("ok")
    env["status"] = "weird"
    assert is_envelope(env) is False


def test_envelope_running_status():
    env = make_envelope("running", data={"job_id": "j1", "current_step": 3})
    assert env["status"] == "running"
    assert is_envelope(env)


def test_envelope_extra_metadata():
    env = make_envelope("ok", extra_metadata={"resource": "GPIB0::1"})
    assert env["metadata"]["resource"] == "GPIB0::1"
