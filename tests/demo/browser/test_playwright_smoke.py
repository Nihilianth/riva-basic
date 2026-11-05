def test_headless_chromium_runs_page_javascript(chromium) -> None:
    page = chromium.new_page()
    page.set_content("<h1 id=t>Hallo</h1><script>document.title = 'ok' + (1 + 1)</script>")
    assert page.text_content("#t") == "Hallo"
    assert page.title() == "ok2"
    page.close()
