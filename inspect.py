"""
Selector Inspector — run this FIRST to find the correct CSS selectors
for the live SportPesa Aviator page. It opens the page, logs all
clickable elements inside the iframe (if present), and saves a
screenshot so you can see what the bot sees.

Usage:
    python inspect.py
"""

import asyncio
from playwright.async_api import async_playwright
import config


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        print("[1] Going to login page…")
        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        print("[2] Logging in…")
        try:
            await page.fill('input[type="tel"], input[name="username"]', config.USERNAME)
            await page.fill('input[type="password"]', config.PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"  Login step error (may be OK): {e}")

        print("[3] Opening Aviator…")
        await page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        await page.screenshot(path="screenshot_main.png", full_page=True)
        print("  Main page screenshot saved → screenshot_main.png")

        # ── Check for iframes ───────────────────────────────────────────────
        frames = page.frames
        print(f"\n[4] Frames detected: {len(frames)}")
        for i, f in enumerate(frames):
            print(f"  Frame[{i}] url={f.url[:80]}")

        # ── Dump all buttons + inputs from each frame ───────────────────────
        for i, frame in enumerate(frames):
            print(f"\n─── Frame[{i}] elements ───")
            try:
                buttons = await frame.query_selector_all("button")
                for btn in buttons:
                    txt  = (await btn.inner_text()).strip()[:50]
                    cls  = await btn.get_attribute("class") or ""
                    did  = await btn.get_attribute("data-testid") or ""
                    print(f"  <button> text={txt!r:30s} class={cls[:40]!r} testid={did!r}")

                inputs = await frame.query_selector_all("input")
                for inp in inputs:
                    typ  = await inp.get_attribute("type") or "text"
                    name = await inp.get_attribute("name") or ""
                    ph   = await inp.get_attribute("placeholder") or ""
                    cls  = await inp.get_attribute("class") or ""
                    print(f"  <input>  type={typ!r:8s} name={name!r:20s} placeholder={ph!r:20s} class={cls[:30]!r}")
            except Exception as e:
                print(f"  (could not query frame: {e})")

        print("\n[5] Browser stays open for 60s — inspect DevTools manually.")
        await asyncio.sleep(60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
