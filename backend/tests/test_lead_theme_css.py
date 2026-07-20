import re
from pathlib import Path

BASE_CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "base.css"
LEAD_CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "lead-theme.css"

TOKEN_RE = re.compile(r"(--[a-zA-Z0-9-]+)\s*:")


def _tokens(path: Path) -> set[str]:
    return set(TOKEN_RE.findall(path.read_text()))


def test_lead_theme_css_file_exists():
    assert LEAD_CSS.exists()


def test_base_css_root_tokens_unchanged():
    expected_base_tokens = {
        "--bg", "--surface", "--surface-2", "--line", "--text", "--muted", "--muted-2",
        "--teal", "--teal-deep", "--teal-soft", "--red", "--red-soft", "--urgent",
        "--urgent-soft", "--amber", "--amber-soft", "--green", "--green-soft",
        "--purple", "--purple-soft", "--cyan", "--cyan-soft", "--blue", "--blue-soft",
        "--shadow", "--radius-xl", "--radius-lg", "--radius-md", "--radius-sm",
    }
    assert _tokens(BASE_CSS) == expected_base_tokens


def test_lead_theme_tokens_do_not_collide_with_base_css():
    base_tokens = _tokens(BASE_CSS)
    lead_tokens = _tokens(LEAD_CSS)
    assert lead_tokens, "lead-theme.css should define at least one custom property"
    assert base_tokens & lead_tokens == set()


def test_lead_theme_defines_expected_tokens():
    expected = {
        "--lead-sidebar-bg", "--lead-sidebar-bg-active", "--lead-sidebar-text",
        "--lead-sidebar-text-active", "--lead-sidebar-line", "--lead-accent-blue",
        "--lead-accent-blue-soft", "--lead-accent-orange", "--lead-accent-orange-soft",
        "--lead-card-bg", "--lead-card-line", "--lead-shadow",
    }
    assert expected <= _tokens(LEAD_CSS)


def test_lead_theme_defines_expected_component_classes():
    content = LEAD_CSS.read_text()
    expected_classes = [
        ".lead-shell", ".lead-sidebar", ".lead-sidebar-brand", ".lead-nav-item",
        ".lead-nav-item-disabled", ".lead-main", ".lead-stat-cards", ".lead-stat-card",
        ".lead-widgets-grid", ".lead-widget-card", ".lead-panel", ".lead-panel-tabs",
        ".lead-panel-tab", ".lead-panel-tab-disabled", ".lead-panel-tab-content",
        ".lead-status-tabs", ".lead-status-tab", ".lead-toolbar-btn-disabled",
    ]
    for cls in expected_classes:
        assert cls in content, f"missing expected class {cls}"
