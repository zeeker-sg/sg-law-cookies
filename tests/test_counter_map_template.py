"""Counter map template (counter_map.html.j2) — render contract tests.

The page must be fully self-hosted (vendored D3, no JS CDNs), fetch its sky
JSON from /data/sky/, and keep the approved mockup copy (kuih bangkit link,
empty state). Mirrors the jinja environment used by sitegen._jinja_env.

Phase 6 (Levels 2-3): all page JS lives in /static/sky.js (no inline script
bodies); the template carries the Level-3 panel dialog, the mobile focus
list, and the backdrop hooks that sky.js drives; sky.js owns the data
fetching and the #<date>/<cookieid> deep links.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "sg_law_cookies" / "templates"
)
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "sg_law_cookies" / "static"
SKY_JS = STATIC_DIR / "sky.js"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(
            enabled_extensions=("html", "j2", "xml"), default_for_string=True
        ),
        keep_trailing_newline=True,
    )


@pytest.fixture(scope="module")
def rendered() -> str:
    # Stub context: the page needs no variables (data arrives client-side),
    # and base.html.j2 only uses defaults/blocks.
    return _env().get_template("counter_map.html.j2").render()


@pytest.fixture(scope="module")
def sky_js() -> str:
    return SKY_JS.read_text(encoding="utf-8")


def test_template_parses() -> None:
    source = (TEMPLATES_DIR / "counter_map.html.j2").read_text(encoding="utf-8")
    _env().parse(source)  # raises TemplateSyntaxError on a bad template


def test_no_js_cdn_urls(rendered: str) -> None:
    for cdn in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "d3js.org"):
        assert cdn not in rendered


def test_vendored_d3_referenced(rendered: str) -> None:
    assert '<script src="/static/d3.v7.min.js"></script>' in rendered


def test_sky_css_linked(rendered: str) -> None:
    assert '/static/sky.css' in rendered


def test_kuih_bangkit_wikipedia_link(rendered: str) -> None:
    assert "https://en.wikipedia.org/wiki/Kue_bangkit" in rendered


def test_fetches_sky_index(sky_js: str) -> None:
    # The fetch moved from the inline template script into /static/sky.js.
    assert "fetch('/data/sky/index.json')" in sky_js
    assert "/data/sky/" in sky_js  # per-day files too


def test_empty_state_copy(rendered: str) -> None:
    assert "the counter is bare" in rendered


# ── Phase 6: JS externalised to /static/sky.js ────────────────────────


def test_sky_js_referenced_deferred(rendered: str) -> None:
    assert '<script src="/static/sky.js" defer></script>' in rendered


def test_no_inline_script_bodies(rendered: str) -> None:
    """Every <script> tag has a src= and an empty body — no inline JS."""
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", rendered, flags=re.S)
    assert scripts, "expected the d3 + sky.js script tags"
    for attrs, body in scripts:
        assert "src=" in attrs, f"inline <script{attrs}> found"
        assert body.strip() == "", "script tag with src= must have an empty body"


def test_sky_js_parses_as_javascript() -> None:
    """node --check when node is available; otherwise a sanity fallback."""
    node = shutil.which("node")
    if node:
        proc = subprocess.run(
            [node, "--check", str(SKY_JS)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
    else:
        src = SKY_JS.read_text(encoding="utf-8")
        assert "function" in src
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            assert src.count(opener) == src.count(closer), (
                f"unbalanced {opener}{closer} in sky.js"
            )


# ── Phase 6: Level 2/3 markup hooks ───────────────────────────────────


def test_panel_dialog_markup(rendered: str) -> None:
    assert 'id="panel"' in rendered
    assert 'role="dialog"' in rendered
    assert 'aria-modal="true"' in rendered
    assert 'aria-labelledby="panel-headline"' in rendered
    assert 'id="panel-close"' in rendered
    assert 'id="panel-backdrop"' in rendered


def test_panel_content_hooks(rendered: str) -> None:
    for hook in (
        'id="panel-meta"',
        'id="panel-headline"',
        'id="panel-summary"',
        'id="panel-why"',
        'id="panel-issues"',
        'id="panel-concepts"',
        'id="panel-source"',
    ):
        assert hook in rendered, f"missing panel hook {hook}"
    assert "What the court decided" in rendered


def test_mobile_focus_list_hook(rendered: str) -> None:
    # PRD 7.7: ≤720px focus mode degrades to a stacked list under the SVG.
    assert 'id="focus-list"' in rendered


def test_sky_js_hash_deep_links(sky_js: str) -> None:
    assert "location.hash" in sky_js
    assert "hashchange" in sky_js
    assert "replaceState" in sky_js  # no full reloads


def test_sky_js_levels_and_idiom(sky_js: str) -> None:
    # Level 2/3 machinery and the settled idiom hooks are present.
    for needle in (
        "enterFocus",
        "exitFocus",
        "openPanel",
        "closePanel",
        "renderFocusList",
        "hexPath",          # hexagons = judgments
        "url(#tart)",       # pineapple-tart styling = high significance
        "chip-node",        # chocolate-chip FOLIO concept dots
        "Escape",
    ):
        assert needle in sky_js, f"missing {needle} in sky.js"
