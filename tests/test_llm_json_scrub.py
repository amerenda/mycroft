from common.llm import _scrub_json_string, _scrub_json_value


def test_scrub_json_string_removes_nul_surrogates_and_controls():
    raw = "a\x00b\ud800c\udfffd\x01e\tf\ng\rh"
    got = _scrub_json_string(raw)
    assert got == "abcde\tf\ng\rh"


def test_scrub_json_value_recurses_through_structures():
    payload = {
        "bad\x00key": [
            "ok",
            "x\x00y",
            {"nested": "z\ud800w", "n": float("nan")},
        ]
    }
    got = _scrub_json_value(payload)
    assert "badkey" in got
    assert got["badkey"][1] == "xy"
    assert got["badkey"][2]["nested"] == "zw"
    assert got["badkey"][2]["n"] is None
