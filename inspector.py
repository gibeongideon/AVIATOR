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
            await page.fill('input[name="user"]', config.USERNAME)
            await page.fill('input[name="password"]', config.PASSWORD)
            await page.click('[data-testid="login-form-submit-button"]')
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"  Login step error (may be OK): {e}")

        print("[3] Opening Aviator…")
        await page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")

        # Dismiss cookie banner
        try:
            await page.click('button.btn-primary', timeout=4000)
            print("  Cookie banner dismissed.")
        except Exception:
            pass

        # Wait for the Spribe game iframe
        print("  Waiting for Spribe game iframe…")
        try:
            await page.wait_for_selector('iframe[src*="spribegaming.com"]', timeout=15_000)
            print("  Aviator iframe found!")
        except Exception:
            print("  WARNING: Aviator iframe not found within 15s")

        await page.wait_for_timeout(4000)  # let game JS initialise
        await page.screenshot(path="screenshot_main.png", full_page=False)
        print("  Screenshot saved → screenshot_main.png")

        # ── Show all frames ─────────────────────────────────────────────────
        frames = page.frames
        print(f"\n[4] Frames detected: {len(frames)}")
        for i, f in enumerate(frames):
            print(f"  Frame[{i}] url={f.url[:80]}")

        # ── Focus: dump Aviator game frame elements ─────────────────────────
        aviator_frame = None
        for f in frames:
            if "spribegaming.com" in f.url or "aviator-next" in f.url:
                aviator_frame = f
                break

        if aviator_frame:
            print(f"\n─── AVIATOR GAME FRAME ({aviator_frame.url[:60]}) ───")
            try:
                buttons = await aviator_frame.query_selector_all("button")
                for btn in buttons:
                    txt = (await btn.inner_text()).strip()[:60]
                    cls = await btn.get_attribute("class") or ""
                    did = await btn.get_attribute("data-testid") or ""
                    print(f"  <button> text={txt!r:35s} class={cls[:45]!r} testid={did!r}")

                inputs = await aviator_frame.query_selector_all("input")
                for inp in inputs:
                    typ  = await inp.get_attribute("type") or "text"
                    name = await inp.get_attribute("name") or ""
                    ph   = await inp.get_attribute("placeholder") or ""
                    cls  = await inp.get_attribute("class") or ""
                    print(f"  <input>  type={typ!r:8s} name={name!r:20s} placeholder={ph!r:25s} class={cls[:35]!r}")

                # Also dump any div with "bet" or "cashout" or "multiplier" in class
                special = await aviator_frame.query_selector_all(
                    '[class*="bet"], [class*="cashout"], [class*="multiplier"], [class*="coefficient"]'
                )
                print(f"\n  Special elements (bet/cashout/multiplier): {len(special)}")
                for el in special[:20]:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    cls = await el.get_attribute("class") or ""
                    txt = (await el.inner_text()).strip()[:40]
                    print(f"    <{tag}> class={cls[:50]!r} text={txt!r}")
            except Exception as e:
                print(f"  Error querying game frame: {e}")
        else:
            print("\nNo Aviator game frame found — check login or URL.")

        print("\n[5] Browser stays open for 60s — use DevTools to inspect.")
        await asyncio.sleep(60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
