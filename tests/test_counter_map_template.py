"""Counter map template (counter_map.html.j2) — render contract tests.

The page must be fully self-hosted (vendored D3, no JS CDNs), fetch its sky
JSON from /data/sky/, and keep the approved mockup copy (kuih bangkit link,
empty state). Mirrors the jinja environment used by sitegen._jinja_env.
"""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "sg_law_cookies" / "templates"
)


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


def test_fetches_sky_index(rendered: str) -> None:
    assert "fetch('/data/sky/index.json')" in rendered


def test_empty_state_copy(rendered: str) -> None:
    assert "the counter is bare" in rendered
