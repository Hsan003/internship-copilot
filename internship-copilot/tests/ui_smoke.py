"""Optional end-to-end UI smoke test (needs: pip install playwright && playwright install chromium; a running server).
Usage:  python tests/ui_smoke.py http://localhost:8765 /tmp/shots
"""
import sys, time
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
errors = []

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))

    page.goto(BASE); page.wait_for_selector("#pills .pill"); time.sleep(0.5)
    page.screenshot(path=f"{OUT}/01_new.png")

    page.click("#btn-sample-fr"); page.wait_for_selector("#job-preview:not([hidden])"); time.sleep(0.6)
    assert page.locator("#bp-select option").count() >= 3, "shipped forms not listed"
    page.select_option("#bp-select", "letter-short.yaml"); time.sleep(0.3)
    assert page.locator("#bp-summary .tag").count() == 4
    page.fill("#contact-name", "Claire Dubois"); page.fill("#contact-role", "Engineering Manager")
    page.fill("#note", "I follow your engineering blog about route optimisation.")
    page.screenshot(path=f"{OUT}/02_job_preview.png", full_page=True)

    page.click("#btn-generate")
    page.wait_for_selector("#tab-app.active .ws", timeout=60000); time.sleep(1.5)
    page.screenshot(path=f"{OUT}/03_app_letter.png", full_page=False)
    page.screenshot(path=f"{OUT}/03b_app_full.png", full_page=True)

    assert page.locator(".para").count() == 4, "the chosen short form was not used"
    # edit a paragraph, save & rebuild
    ta = page.locator(".para textarea").nth(1)
    ta.fill(ta.input_value() + " Edited by me.")
    page.click("text=Save & rebuild PDF"); time.sleep(2.0)
    assert "Edited by me." in page.locator(".para textarea").nth(1).input_value(), "edit not persisted"
    page.click(".panel-tabs button:has-text('CV')"); time.sleep(1.0)
    page.screenshot(path=f"{OUT}/04_app_cv_tab.png")

    # spontaneous
    page.click("#tabs button[data-tab=spont]"); page.wait_for_selector("#sp-domains .chip")
    page.fill("#sp-company", "Orbit Labs"); page.fill("#sp-contact", "Marc Weber"); page.fill("#sp-email", "marc@orbit.example")
    page.click("#sp-domains .chip:nth-child(1)"); page.click("#sp-domains .chip:nth-child(2)")
    page.fill("#sp-note", "We met at the DevOpsDays Paris booth.")
    page.screenshot(path=f"{OUT}/05_spont_form.png", full_page=True)
    page.click("#btn-sp-generate"); page.wait_for_selector("#tab-app.active .ws", timeout=60000); time.sleep(1.2)
    page.screenshot(path=f"{OUT}/06_spont_result.png", full_page=True)

    # tracker
    page.click("#tabs button[data-tab=tracker]"); page.wait_for_selector("table.tr"); time.sleep(0.4)
    page.screenshot(path=f"{OUT}/07_tracker.png")

    # profile editor
    page.click("#tabs button[data-tab=profile]"); page.wait_for_selector("#ed-text"); time.sleep(0.5)
    page.click("text=CV template (LaTeX)"); time.sleep(0.4)
    page.click("#ed-build-en"); page.wait_for_selector("#ed-preview iframe, #ed-preview .notice.error", timeout=30000); time.sleep(1.2)
    page.screenshot(path=f"{OUT}/08_profile_cv_template.png", full_page=False)

    # duplicate a form from the editor
    page.click("text=Form (letter): letter-short.yaml"); time.sleep(0.4)
    page.once("dialog", lambda d: d.accept("My ui form"))
    page.click("#ed-duplicate"); page.wait_for_function("document.querySelector('#ed-title').textContent.includes('letter-my-ui-form.yaml')", timeout=10000)
    assert page.locator("#ed-reset").is_hidden(), "reset must be hidden for a form you created"
    page.screenshot(path=f"{OUT}/08b_form_editor.png")

    # setup
    page.click("#tabs button[data-tab=setup]"); time.sleep(0.8)
    page.screenshot(path=f"{OUT}/09_setup.png", full_page=True)
    b.close()

print("JS/console problems:", errors if errors else "none")
