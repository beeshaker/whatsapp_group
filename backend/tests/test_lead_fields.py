from lead_fields import (
    extract_agent_tag, extract_phones, extract_source_tag,
    is_valid_phone, normalize_phone, resolve_phone_for_issue,
)


def test_normalize_phone_local_format():
    assert normalize_phone("0746823554") == "254746823554"


def test_normalize_phone_international_with_space():
    assert normalize_phone("+254718 992279") == "254718992279"


def test_normalize_phone_international_no_plus():
    assert normalize_phone("254784549538") == "254784549538"


def test_normalize_phone_idempotent():
    assert normalize_phone("254746823554") == "254746823554"


def test_extract_phones_finds_local_number():
    text = "@~Jabeen kindly contact Samson 0746823554, looking for a 4br for rent"
    assert extract_phones(text) == ["254746823554"]


def test_extract_phones_finds_international_with_space():
    text = "Kindly contact Sarah Bhaijee at +254718 992279 to inquire on units"
    assert extract_phones(text) == ["254718992279"]


def test_extract_phones_ignores_short_digit_runs():
    text = "Budget 120k, size 3000 sq ft, no phone here"
    assert extract_phones(text) == []


def test_extract_phones_multiple_distinct_numbers_in_order():
    text = "call 0746823554 or 0722516801 if unavailable"
    assert extract_phones(text) == ["254746823554", "254722516801"]


def test_extract_phones_no_match_in_plain_text():
    text = "looking for a Godown in babadogo or Mombasa road"
    assert extract_phones(text) == []


def test_resolve_phone_for_issue_uses_snippet_first():
    assert resolve_phone_for_issue(
        "contact Samson 0746823554", "full body irrelevant 0722516801"
    ) == "254746823554"


def test_resolve_phone_for_issue_falls_back_to_body_when_unambiguous():
    assert resolve_phone_for_issue(
        "looking for a 4br", "kindly contact Samson on 0746823554"
    ) == "254746823554"


def test_resolve_phone_for_issue_none_when_body_ambiguous():
    assert resolve_phone_for_issue(
        "looking for a 4br", "0746823554 and 0722516801 both work"
    ) is None


def test_resolve_phone_for_issue_none_when_nothing_found():
    assert resolve_phone_for_issue("looking for a 4br", "no phone mentioned at all") is None


def test_is_valid_phone_accepts_local_format():
    assert is_valid_phone("0746823554") is True


def test_is_valid_phone_accepts_international_format():
    assert is_valid_phone("+254718992279") is True


def test_is_valid_phone_rejects_free_text():
    assert is_valid_phone("not a phone") is False


def test_is_valid_phone_rejects_too_short():
    assert is_valid_phone("07468") is False


def test_extract_agent_tag_finds_tilde_mention():
    assert extract_agent_tag("@~Jabeen kindly contact Samson") == "Jabeen"


def test_extract_agent_tag_finds_mention_after_prefix_text():
    assert extract_agent_tag("Dunhill Consulting Limited: @~Harsha Kindly contact Sarah") == "Harsha"


def test_extract_agent_tag_none_when_no_mention():
    assert extract_agent_tag("kindly contact Samson 0746823554") is None


def test_extract_source_tag_finds_trailing_parenthetical():
    assert extract_source_tag("looking for a Godown in babadogo (Board Enquiry)") == "Board Enquiry"


def test_extract_source_tag_ignores_non_trailing_parenthetical():
    text = "kindly contact Mercy (agent) on 0784549538 looking for commercial space (Website Enquiry)"
    assert extract_source_tag(text) == "Website Enquiry"


def test_extract_source_tag_handles_trailing_period_before_close():
    text = "to inquire on available units on Block A for sale in windsongs. (Website)"
    assert extract_source_tag(text) == "Website"


def test_extract_source_tag_none_when_no_trailing_parenthetical():
    assert extract_source_tag("no source tag on this message") is None
