import re

import main as backend_main


def _render(role, active_page):
    return backend_main.templates.env.get_template("_nav_lead.html").render(
        role=role, active_page=active_page
    )


def test_nav_lead_shows_real_links():
    html = _render(role="admin", active_page="overview")
    assert 'href="/overview"' in html
    assert 'href="/"' in html
    assert 'href="/archive"' in html
    assert 'class="lead-sidebar"' in html


def test_nav_lead_marks_active_page():
    html = _render(role="admin", active_page="overview")
    overview_link = re.search(r'<a[^>]*href="/overview"[^>]*>', html).group(0)
    assert "active" in overview_link


def test_nav_lead_shows_administration_for_admin():
    html = _render(role="admin", active_page="overview")
    assert 'href="/users"' in html


def test_nav_lead_shows_administration_for_super_admin():
    html = _render(role="super_admin", active_page="overview")
    assert 'href="/users"' in html


def test_nav_lead_hides_administration_for_plain_user():
    html = _render(role="user", active_page="live")
    assert 'href="/users"' not in html


def test_nav_lead_disabled_items_are_not_navigable():
    html = _render(role="user", active_page="live")
    disabled_blocks = re.findall(r'<span class="lead-nav-item-disabled"[^>]*>.*?</span>', html)
    assert len(disabled_blocks) == 2  # Properties, Agent Performance
    for block in disabled_blocks:
        assert "href" not in block
    assert "Properties" in html
    assert "Agent Performance" in html
