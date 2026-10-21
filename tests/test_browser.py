"""Real-browser tests (Chromium via Playwright).

Optional:  pip install playwright && playwright install chromium
Run with:  BROWSER_TESTS=1 pytest -q tests/test_browser.py
(set CHROMIUM_PATH to use an already installed Chromium)
"""

import asyncio
import os

import pytest

from tests.conftest import ADMIN_PASSWORD
from tests.helpers import SocketClient, wait_for

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not os.environ.get("BROWSER_TESTS"), reason="set BROWSER_TESTS=1 to run browser tests"),
]
playwright_api = pytest.importorskip("playwright.async_api")


async def launch(p):
    path = os.environ.get("CHROMIUM_PATH")
    return await p.chromium.launch(executable_path=path) if path else await p.chromium.launch()


def watch(page, errors):
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))


def test_landing_flow_and_logo_text(server):
    async def go():
        errors = []
        async with playwright_api.async_playwright() as p:
            b = await launch(p)
            page = await b.new_page(viewport={"width": 1280, "height": 800})
            watch(page, errors)
            await page.goto(server.url + "/")
            # the full logo (with the school name under it) is used on the gate
            assert "logo-full-light" in await page.get_attribute(".gate-logo", "src")
            await page.click("#openBtn")
            await page.wait_for_selector("#welcome:not([hidden])")
            await page.keyboard.press("Enter")
            await page.wait_for_selector("#home:not([hidden])")
            assert "logo-full-light" in await page.get_attribute(".hero-logo", "src")
            await page.evaluate("window.scrollTo(0, 600)")
            await page.wait_for_selector("#nav.scrolled")  # wait_for_function would need eval, which the CSP blocks
            await page.goto(server.url + "/about")
            await page.wait_for_selector(".about-hero img")
            await b.close()
        assert not errors, errors
    asyncio.run(go())


def test_teacher_and_student_in_the_browser(server):
    async def go():
        errors = []
        a = await SocketClient(server.url).connect()
        await a.call("admin:login", {"password": ADMIN_PASSWORD})
        await a.call("data:reset", {"confirm": "RESET"})
        async with playwright_api.async_playwright() as p:
            b = await launch(p)
            admin = await b.new_page(viewport={"width": 1366, "height": 900})
            watch(admin, errors)
            await admin.goto(server.url + "/admin")
            await admin.fill("#pw", ADMIN_PASSWORD)
            await admin.click("#loginForm button[type=submit]")
            await admin.wait_for_selector("#shell:not([hidden])")
            await admin.click(".a-tabs button[data-tab=classes]")
            await admin.select_option("#classSide select", "10")
            await admin.fill("#classSide input[type=number] >> nth=1", "2")
            await admin.click("#classSide button:has-text('إضافة')")
            await admin.wait_for_selector(".class-item >> text=10/2")
            await admin.click("button:has-text('أسئلة تجريبية')")
            await admin.wait_for_selector(".qcard")
            await admin.click(".a-tabs button[data-tab=students]")
            await admin.wait_for_selector("#stuTable")

            student = await b.new_page(viewport={"width": 390, "height": 844})
            watch(student, errors)
            await student.goto(server.url + "/play")
            await student.click("text=العاشر")
            await student.click("button.class-btn >> text=10/1")
            await student.fill("input.name-input", "طالب المتصفح")
            await student.click("form button[type=submit]")
            await student.wait_for_selector(".hello")
            await admin.wait_for_selector("#stuTable >> text=طالب المتصفح")

            # rename from the Students tab reaches the phone
            await admin.click("#stuTable button[title='تعديل الاسم']")
            await admin.fill(".modal input", "اسم معدّل")
            await admin.click(".modal button:has-text('حفظ')")
            await student.wait_for_selector("#whoName >> text=اسم معدّل")

            display = await b.new_page(viewport={"width": 1600, "height": 900})
            watch(display, errors)
            await display.goto(server.url + "/display")
            await display.wait_for_selector(".idle-brand img[src*='logo-full-light']")
            await a.call("display:message", {"text": "استراحة قصيرة", "show": True})
            await display.wait_for_selector(".message-card >> text=استراحة قصيرة")
            await b.close()
        await a.close()
        assert not errors, errors
    asyncio.run(go())
