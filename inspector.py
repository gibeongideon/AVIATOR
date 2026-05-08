"""
Selector Inspector — focused on Auto cashout UI and crash history.
Usage:  python inspector.py
"""

import asyncio
from playwright.async_api import async_playwright
import config


async def find_spribe_frame(page, timeout_s=20):
    for _ in range(timeout_s * 2):
        for f in page.frames:
            if "spribegaming.com" in f.url or "aviator-next" in f.url:
                return f
        await asyncio.sleep(0.5)
    return None


async def dump_elements(frame, selector, label):
    els = await frame.query_selector_all(selector)
    if not els:
        print(f"  [{label}] — nothing found for: {selector}")
        return
    print(f"\n  ── {label} ({len(els)} found) ──")
    for el in els:
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        cls = (await el.get_attribute("class") or "")[:60]
        txt = (await el.inner_text()).strip()[:50]
        ph  = await el.get_attribute("placeholder") or ""
        did = await el.get_attribute("data-id") or ""
        print(f"    <{tag}> class={cls!r:62s} text={txt!r:30s} placeholder={ph!r} data-id={did!r}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=150,
            args=["--start-maximized"],
        )
        context = await browser.new_context(no_viewport=True)
        page    = await context.new_page()

        # ── Login ────────────────────────────────────────────────────────────
        print("[1] Logging in…")
        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.fill('input[name="user"]', config.USERNAME)
        await page.fill('input[name="password"]', config.PASSWORD)
        await page.click('[data-testid="login-form-submit-button"]')
        await page.wait_for_timeout(3000)

        # ── Open Aviator ─────────────────────────────────────────────────────
        print("[2] Opening Aviator…")
        await page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        try:
            await page.click('button.btn-primary', timeout=4000)
            print("  Cookie dismissed.")
        except Exception:
            pass

        print("[3] Waiting for Spribe game frame…")
        frame = await find_spribe_frame(page, timeout_s=25)
        if not frame:
            print("  ERROR: game frame not found.")
            await asyncio.sleep(10)
            await browser.close()
            return

        print(f"  Frame: {frame.url[:80]}")
        await page.wait_for_timeout(4000)   # let JS boot

        # ── Click the AUTO tab on Panel 1 to reveal auto cashout input ───────
        print("\n[4] Clicking AUTO tab to reveal auto-cashout inputs…")
        try:
            auto_tabs = await frame.query_selector_all('button.tab:has-text("Auto")')
            print(f"  Found {len(auto_tabs)} Auto tabs")
            for tab in auto_tabs:
                await tab.click()
                await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  Auto tab error: {e}")

        await page.wait_for_timeout(1000)
        await page.screenshot(path="screenshot_auto.png", full_page=False)
        print("  Screenshot saved → screenshot_auto.png")

        # ── Dump ALL inputs (before and after clicking Auto) ─────────────────
        print("\n[5] ALL inputs in game frame:")
        inputs = await frame.query_selector_all("input")
        for i, inp in enumerate(inputs):
            typ = await inp.get_attribute("type") or "text"
            cls = (await inp.get_attribute("class") or "")[:50]
            ph  = await inp.get_attribute("placeholder") or ""
            val = await inp.input_value()
            print(f"  input[{i}] type={typ!r} placeholder={ph!r:8s} value={val!r:8s} class={cls!r}")

        # ── Dump ALL buttons ──────────────────────────────────────────────────
        print("\n[6] ALL buttons in game frame:")
        buttons = await frame.query_selector_all("button")
        for btn in buttons:
            txt = (await btn.inner_text()).strip()[:50]
            cls = (await btn.get_attribute("class") or "")[:60]
            print(f"  <button> text={txt!r:35s} class={cls!r}")

        # ── Crash history (multiplier bubbles at top) ─────────────────────────
        print("\n[7] Crash history selectors:")
        history_selectors = [
            '.history',
            '[class*="history"]',
            '[class*="coef"]',
            '[class*="crash"]',
            '.bubble',
            '[class*="bubble"]',
            '[class*="round-history"]',
            '[class*="prev"]',
            'app-history',
            'app-round-history',
        ]
        for sel in history_selectors:
            els = await frame.query_selector_all(sel)
            if els:
                for el in els[:3]:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    cls = (await el.get_attribute("class") or "")[:60]
                    txt = (await el.inner_text()).strip()[:60]
                    print(f"  [{sel}] <{tag}> class={cls!r} text={txt!r}")

        # ── Use JS to find elements containing multiplier numbers ─────────────
        print("\n[8] JS scan for coefficient/history elements:")
        js_result = await frame.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('*').forEach(el => {
                    const cls = el.className || '';
                    if (typeof cls === 'string' && (
                        cls.includes('history') || cls.includes('coef') ||
                        cls.includes('bubble') || cls.includes('crash') ||
                        cls.includes('round') || cls.includes('prev-round')
                    )) {
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            cls: cls.substring(0, 80),
                            text: el.innerText ? el.innerText.substring(0, 60).trim() : ''
                        });
                    }
                });
                return results.slice(0, 30);
            }
        """)
        for item in js_result:
            print(f"  <{item['tag']}> class={item['cls']!r:82s} text={item['text']!r}")

        print("\n[9] Browser stays open 90s — inspect DevTools manually.")
        await asyncio.sleep(90)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
