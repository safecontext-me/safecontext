"""Static checks on the window's HTML and JavaScript (no browser needed)."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui"
HTML = (UI / "index.html").read_text()
JS = (UI / "app.js").read_text()


def test_every_id_the_script_looks_up_exists():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"]+)"\)', JS))
    assert used - ids == set()


def test_ids_are_unique():
    ids = re.findall(r'id="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))


def test_mcp_card_has_link_and_a_tab_per_app():
    assert "Use in Grok" not in HTML
    # MCP is its own screen in the sidebar, holding the relay card and the local config.
    assert '<button data-view="mcp">MCP</button>' in HTML
    mcp = HTML[HTML.index('<section id="view-mcp"'):]
    mcp = mcp[:mcp.index("</section>")]
    assert 'id="mcp-card"' in mcp and 'id="mcp-json"' in mcp
    # The link and its Copy button come before the per-app instructions.
    assert HTML.index('id="mcp-url"') < HTML.index('id="mcp-link-copy"') < HTML.index('class="mcp-tabs"')
    for app in ("grok", "claude", "muse"):
        assert f'id="tab-{app}" data-app="{app}"' in HTML
        assert f'id="panel-{app}"' in HTML
    assert "grok.com/connectors" in HTML and "claude.ai/customize/connectors" in HTML


def test_open_buttons_only_name_apps_with_a_connectors_page():
    from safecontext.grok_link import CONNECTOR_PAGES
    assert set(re.findall(r'data-open-app="([^"]+)"', HTML)) <= set(CONNECTOR_PAGES)


def test_search_screen_is_gone():
    assert 'id="view-search"' not in HTML and 'data-view="search"' not in HTML
    assert '<section id="view-mail" class="view">' in HTML  # shown at start, not hidden
    for gone in ("loadRecent", "doSearch", "renderResults", "openThread", "api().search(", "api().recent("):
        assert gone not in JS


def test_sources_is_a_parent_with_each_source_under_it():
    nav = HTML[HTML.index('<nav class="nav">'):]
    nav = nav[:nav.index("</nav>")]
    # Sources, then the per-source entries (filled in by app.js), then the other screens.
    assert nav.index('data-view="sources"') < nav.index('id="nav-sources"') < nav.index('data-view="mcp"')
    assert 'data-view="mail"' not in nav  # each source opens the browse page instead
    assert '<button data-view="removed">Security Dashboard</button>' in nav
    assert "What was removed" not in HTML


def _panel(app):
    p = HTML[HTML.index(f'id="panel-{app}"'):]
    return p[:p.index("</div>")]


def test_claude_signs_in_with_approve_and_muse_stays_link_only():
    claude, muse = _panel("claude"), _panel("muse")
    assert "Approve" in claude and "safecontext" in claude
    assert "link-only-box" not in claude and "coming" not in claude and "No sign-in" not in claude
    assert "link-only-box" in muse and "coming for Muse" in muse


def test_approve_dialog_names_the_app_it_was_sent():
    assert '<h2><span id="ga-title">' in HTML and 'id="ga-client"' in HTML
    assert '$("ga-title").textContent = who;' in JS and '$("ga-client").textContent = who;' in JS
    assert 'const who = g.approval.client || "An AI app";' in JS
