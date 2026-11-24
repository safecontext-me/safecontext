"""A made-up home folder for the AI access review's opt-in scan in demo mode, so the demo never
reads the real ~/Library/Application Support/Claude or ~/.claude. Values are obviously fake."""
from __future__ import annotations

import json
from pathlib import Path


def demo_ai_home(root: Path) -> Path:
    root = Path(root)
    desktop = root / "Library" / "Application Support" / "Claude"
    desktop.mkdir(parents=True, exist_ok=True)
    (desktop / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {
        "safecontext": {"command": "/Applications/SafeContext.app/Contents/MacOS/SafeContext", "args": ["mcp"]},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/alex"]},
        "weather": {"command": "uvx", "args": ["weather-mcp"], "env": {"WEATHER_API_KEY": "demo-not-a-real-key"}},
    }}, indent=2))
    code = root / ".claude"
    code.mkdir(parents=True, exist_ok=True)
    (code / "settings.json").write_text(json.dumps({"permissions": {
        "defaultMode": "acceptEdits",
        "allow": ["Bash(npm test:*)", "Bash(curl:*)", "WebFetch", "Read(~/projects/**)"]}}, indent=2))
    (root / ".claude.json").write_text(json.dumps({
        "mcpServers": {"github": {"type": "http", "url": "https://example.invalid/mcp"}},
        "projects": {"/Users/alex/projects/site": {"mcpServers": {"postgres": {"command": "npx"}}}}}, indent=2))
    return root
