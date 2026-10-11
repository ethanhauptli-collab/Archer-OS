"""Bundled and user brand fonts reach the offline graphics renderer."""

import pytest

from roughcut import brand_fonts, graphics


def test_bundled_venture_fonts_are_found_by_their_real_family_names(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    names = brand_fonts.families()
    assert {"Jost", "Inter", "Fraunces"} <= set(names)
    jost = brand_fonts.available()["Jost"][0]
    assert jost.weight == "100 900"  # variable font: every weight from one file
    assert not jost.italic


def test_only_named_families_are_embedded(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    html = "<html><head><style>h1{font-family:'Jost',sans-serif}</style></head><body><h1>Hi</h1></body></html>"
    out = brand_fonts.embed(html)
    assert out.count("@font-face") == 1 and 'font-family:"Jost"' in out
    assert out.index("data-roughcut-fonts") < out.index("h1{font-family")
    assert brand_fonts.embed("<p style='font-family:Helvetica'>x</p>") == "<p style='font-family:Helvetica'>x</p>"
    # "Inter" inside another word isn't a mention
    assert "@font-face" not in brand_fonts.embed("<p>International</p>")


def test_user_fonts_folder_wins_over_the_bundled_copy(monkeypatch, tmp_path):
    folder = tmp_path / "fonts"
    folder.mkdir()
    (folder / "Jost.ttf").write_bytes((brand_fonts.BUNDLED_DIR / "Jost.ttf").read_bytes())
    monkeypatch.setenv("ROUGHCUT_FONTS", str(folder))
    brand_fonts._scan.cache_clear()
    files = brand_fonts.available()["Jost"]
    assert [f.path.parent for f in files] == [folder]
    brand_fonts._scan.cache_clear()


def test_fraunces_ships_with_its_italic_and_your_own_italic_joins_the_bundled_roman(monkeypatch, tmp_path):
    """The Venture OC look sets pulled quotes in Fraunces italic: a real italic, not slanted roman."""
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    fraunces = brand_fonts.available()["Fraunces"]
    assert sorted(f.italic for f in fraunces) == [False, True] and {f.path.parent for f in fraunces} == {brand_fonts.BUNDLED_DIR}
    css = brand_fonts.font_face_css("<p style=\"font:italic 40px 'Fraunces'\">q</p>")
    assert css.count("@font-face") == 2 and "font-style:italic" in css and "font-style:normal" in css

    # An italic of your own (here, a copy of the bundled one) adds to the bundled roman instead of replacing it.
    folder = tmp_path / "fonts"
    folder.mkdir()
    (folder / "MyFraunces-Italic.ttf").write_bytes((brand_fonts.BUNDLED_DIR / "Fraunces-Italic.ttf").read_bytes())
    monkeypatch.setenv("ROUGHCUT_FONTS", str(folder))
    brand_fonts._scan.cache_clear()
    fraunces = brand_fonts.available()["Fraunces"]
    assert sorted((f.italic, f.path.parent == folder) for f in fraunces) == [(False, False), (True, True)]
    brand_fonts._scan.cache_clear()


def test_fonts_go_into_a_page_without_a_head_after_its_doctype():
    page = "<!doctype html><html><style>.t{font:700 60px Jost}</style><body><p class=t>Venture</p></body></html>"
    out = brand_fonts.embed(page)
    assert out.startswith("<!doctype html><html><style data-roughcut-fonts>") and out.endswith(page[len("<!doctype html><html>") :])
    bare = "<!DOCTYPE html><style>.t{font:700 60px Jost}</style><p class=t>Venture</p>"
    assert brand_fonts.embed(bare).startswith("<!DOCTYPE html><style data-roughcut-fonts>")
    # A <header> isn't the head.
    headed = "<!doctype html><html><body><header style=\"font-family:Jost\">x</header></body></html>"
    assert brand_fonts.embed(headed).startswith("<!doctype html><html><style data-roughcut-fonts>")


@pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")
def test_embedded_fonts_keep_standards_mode_and_draw_a_real_italic(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    from playwright.sync_api import sync_playwright

    page_html = (
        "<!doctype html><html><style>span{font:80px 'Fraunces',monospace}</style><body>"
        "<span id=a>The best part is the families</span><br><span id=b style='font-style:italic'>The best part is the families</span></body></html>"
    )
    with sync_playwright() as p:
        browser = graphics.launch_browser(p)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(brand_fonts.embed(page_html))
        page.evaluate("document.fonts.ready")
        mode = page.evaluate("document.compatMode")
        italic = page.evaluate("document.fonts.check('italic 80px Fraunces')")
        widths = page.evaluate("[a.getBoundingClientRect().width, b.getBoundingClientRect().width]")
        browser.close()
    assert mode == "CSS1Compat"  # not quirks mode
    # Slanted roman keeps the roman's widths; Fraunces Italic has its own letterforms and spacing.
    assert italic and abs(widths[0] - widths[1]) > 10, widths


@pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")
def test_embedded_font_is_what_the_browser_draws(monkeypatch, tmp_path):
    """Offline, with the network blocked, text set in Jost must not fall back."""
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    from playwright.sync_api import sync_playwright

    page_html = "<html><head><style>span{font-size:80px}</style></head><body><span id=a style=\"font-family:'Jost',monospace\">Venture OC wiiiiimmmm</span><span id=b style='font-family:monospace'>Venture OC wiiiiimmmm</span></body></html>"
    with sync_playwright() as p:
        browser = graphics.launch_browser(p)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(brand_fonts.embed(page_html))
        page.evaluate("document.fonts.ready")
        loaded = page.evaluate("document.fonts.check('80px Jost')")
        widths = page.evaluate("[a.getBoundingClientRect().width, b.getBoundingClientRect().width]")
        browser.close()
    assert loaded
    assert abs(widths[0] - widths[1]) > 20, widths  # Jost is proportional, the fallback is monospace


def test_designer_is_told_which_fonts_render(monkeypatch, tmp_path):
    from fractions import Fraction

    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    block = graphics._canvas_block(1920, 1080, Fraction(1001, 24000), {})
    assert "Fonts installed for rendering:" in block and "Jost" in block and "Fraunces" in block
    assert "exact" in graphics.DESIGN_SYSTEM and "font families installed for rendering" in graphics.DESIGN_SYSTEM


@pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")
def test_a_brand_font_renders_without_the_missing_font_warning(monkeypatch, tmp_path):
    """The renderer embeds the bundled family, so the font check has nothing to report."""
    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    from test_graphics_render import _plate, job_for

    html = _plate(extra="<div style=\"position:absolute;left:30%;top:30%;font:700 40px 'Jost',sans-serif;color:#fff\">Venture OC</div>")
    result = graphics._render_one(job_for(tmp_path, html, frames=12, kind="lower_third"))
    assert result["error"] is None
    assert not any("isn't installed" in n for n in result["notes"]), result["notes"]


@pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")
def test_brand_font_is_drawn_from_the_first_frame(monkeypatch, tmp_path):
    """Text a later animation reveals still renders in the brand font on its first frame, not a fallback."""
    import asyncio

    from playwright.async_api import async_playwright

    monkeypatch.setenv("ROUGHCUT_FONTS", str(tmp_path / "none"))
    brand_fonts._scan.cache_clear()
    html = brand_fonts.embed(
        "<html><head><style>span{font-size:80px}</style></head><body>"
        "<span id=a style=\"font-family:'Fraunces',monospace\">Venture OC wiiiimmmm</span></body></html>"
    )

    async def width_after_load() -> tuple[bool, float]:
        async with async_playwright() as p:
            browser = await graphics._launch_browser_async(p)
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html, wait_until="load")
            await graphics.load_fonts(page)
            loaded = await page.evaluate("document.fonts.check('80px Fraunces')")
            width = await page.evaluate("a.getBoundingClientRect().width")
            await browser.close()
            return loaded, width

    loaded, width = asyncio.run(width_after_load())
    assert loaded
    assert width < 1000  # Fraunces draws it about 800 px wide; the monospace fallback about 1130 px
