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
    assert 'id="mcp-card"' not in HTML[HTML.index('<section id="view-search"'):HTML.index('<section id="view-mcp"')]
    # The link and its Copy button come before the per-app instructions.
    assert HTML.index('id="mcp-url"') < HTML.index('id="mcp-link-copy"') < HTML.index('class="mcp-tabs"')
    for app in ("grok", "claude", "muse"):
        assert f'id="tab-{app}" data-app="{app}"' in HTML
        assert f'id="panel-{app}"' in HTML
    assert "grok.com/connectors" in HTML and "claude.ai/customize/connectors" in HTML


def test_open_buttons_only_name_apps_with_a_connectors_page():
    from safecontext.grok_link import CONNECTOR_PAGES
    assert set(re.findall(r'data-open-app="([^"]+)"', HTML)) <= set(CONNECTOR_PAGES)
