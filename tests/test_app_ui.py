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
    # MCP is its own screen, opened from the account menu, holding the relay card and the local config.
    assert 'data-view="mcp"' in HTML  # MCP is in the main nav
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
    assert '<section id="view-mail" class="view" hidden>' in HTML  # not the start screen any more
    for gone in ("loadRecent", "doSearch", "renderResults", "openThread", "api().search(", "api().recent("):
        assert gone not in JS


def test_sources_is_a_parent_with_each_source_under_it():
    nav = HTML[HTML.index('<nav class="nav">'):]
    nav = nav[:nav.index("</nav>")]
    # Security Dashboard, then Sources and the per-source entries (filled in by app.js), then Files, MCP and AI access review.
    assert (nav.index('data-view="removed"') < nav.index('data-view="sources"') < nav.index('id="nav-sources"')
            < nav.index('data-view="files"') < nav.index('data-view="mcp"') < nav.index('data-view="review"'))
    assert 'data-view="mail"' not in nav  # each source opens the browse page instead
    assert "Security Dashboard" in nav and "<h2>Security Dashboard</h2>" in HTML
    assert "What was removed" not in HTML


def test_settings_lives_in_the_account_menu_and_mcp_in_the_nav():
    nav = HTML[HTML.index('<nav class="nav">'):]
    nav = nav[:nav.index("</nav>")]
    assert 'data-view="mcp"' in nav and 'data-view="settings"' not in nav
    menu = HTML[HTML.index('id="acct-menu"'):]
    menu = menu[:menu.index('id="acct-btn"')]
    assert 'data-menu-view="settings"' in menu and 'data-menu-view="mcp"' not in menu


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


def test_files_screen_is_the_opt_in_for_pdfs():
    view = HTML[HTML.index('<section id="view-files"'):]
    view = view[:view.index("</section>")]
    assert "Add folder" in view and "Add PDF" in view
    assert "PDFs whose text may be read" in view
    assert 'id="files-add-folder"' in view and 'id="files-add-file"' in view
    assert "pdf_pick" in JS and "pdf_selection" in JS and "pdf_remove" in JS
    assert "read_text" not in JS  # the screen lists paths; it does not read PDF text
    assert 'if (view === "files") loadFiles();' in JS


def test_app_opens_on_the_security_dashboard():
    assert '<section id="view-removed" class="view">' in HTML  # the only view not hidden at start
    assert 'view: "removed"' in JS  # state.view, shown by the first status refresh
    assert "mailInit();  // the mail list may not have been shown yet" in JS  # openSource before mail was ever shown


def test_lucide_licence_ships_with_the_app():
    # Briefcase copies src/safecontext into the app, so the notice travels with the icons.
    notice = (UI.parent.parent / "THIRD_PARTY_NOTICES.txt").read_text()
    assert "ISC License" in notice and "Lucide Icons and Contributors" in notice
    assert "Cole Bemis" in notice  # the MIT notice for icons derived from Feather (database, search, ...)
    assert "THIRD_PARTY_NOTICES.txt" in HTML
