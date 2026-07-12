from vehicle_plate import extract_plates, is_valid_plate, normalize_plate, resolve_plate_for_issue


def test_normalize_plate_strips_space_and_uppercases():
    assert normalize_plate("kmgq 947z") == "KMGQ947Z"


def test_normalize_plate_idempotent_on_already_normalized_input():
    assert normalize_plate("KMGQ947Z") == "KMGQ947Z"


def test_extract_plates_finds_spaced_plate():
    assert extract_plates("brakes are grinding on KMGQ 947Z today") == ["KMGQ947Z"]


def test_extract_plates_finds_unspaced_plate():
    assert extract_plates("bike KMGQ947Z has a flat tyre") == ["KMGQ947Z"]


def test_extract_plates_finds_plate_missing_trailing_letter():
    assert extract_plates("KMGQ 947 needs service") == ["KMGQ947"]


def test_extract_plates_case_insensitive():
    assert extract_plates("issue with kmgq 947z") == ["KMGQ947Z"]


def test_extract_plates_multiple_distinct_plates_in_order():
    assert extract_plates("KMGQ 947Z has brake issue, KZZT 501M has flat tyre") == ["KMGQ947Z", "KZZT501M"]


def test_extract_plates_rejects_embedded_substring():
    assert extract_plates("reference ASKMGQ947Z is not a plate") == []


def test_extract_plates_no_match_in_plain_text():
    assert extract_plates("my bike needs service please") == []


def test_resolve_plate_for_issue_uses_snippet_first():
    assert resolve_plate_for_issue("KMGQ 947Z brakes grinding", "full body irrelevant KDA 123B") == "KMGQ947Z"


def test_resolve_plate_for_issue_falls_back_to_body_when_unambiguous():
    assert resolve_plate_for_issue("brakes grinding", "issue on KMGQ 947Z reported today") == "KMGQ947Z"


def test_resolve_plate_for_issue_none_when_body_ambiguous():
    assert resolve_plate_for_issue("brakes grinding", "KMGQ 947Z and KZZT 501M both broken") is None


def test_resolve_plate_for_issue_none_when_nothing_found():
    assert resolve_plate_for_issue("brakes grinding", "brakes grinding, no plate mentioned") is None


def test_is_valid_plate_accepts_full_format():
    assert is_valid_plate("KMGQ947Z") is True


def test_is_valid_plate_accepts_missing_trailing_letter():
    assert is_valid_plate("KMGQ947") is True


def test_is_valid_plate_rejects_free_text():
    assert is_valid_plate("not a plate") is False


def test_is_valid_plate_rejects_partial_plate():
    assert is_valid_plate("KM947Z") is False
