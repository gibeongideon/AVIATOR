"""
Inspector — test every possible way to set the cashout input value.
Usage: python inspector.py
"""
import asyncio
from playwright.async_api import async_playwright
import config


async def find_frame(page, timeout_s=30):
    for _ in range(timeout_s * 2):
        for f in page.frames:
            if "spribegaming.com" in f.url or "aviator-next" in f.url:
                try:
                    inputs = await f.query_selector_all("input")
                    if inputs:
                        return f
                except Exception:
                    pass
        await asyncio.sleep(0.5)
    return None


async def read_cashout_inputs(frame):
    result = []
    for inp in await frame.query_selector_all('input'):
        ph  = await inp.get_attribute("placeholder") or ""
        vis = await inp.is_visible()
        val = await inp.input_value()
        if vis:
            result.append((ph, val))
    print(f"  Visible inputs: {result}")
    return result


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False, slow_mo=50, args=["--start-maximized"]
        )
        ctx  = await browser.new_context(no_viewport=True)
        page = await ctx.new_page()

        print("[1] Login…")
        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.fill('input[name="user"]', config.USERNAME)
        await page.fill('input[name="password"]', config.PASSWORD)
        await page.click('[data-testid="login-form-submit-button"]')
        await page.wait_for_timeout(3000)

        print("[2] Aviator…")
        await page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        try:
            await page.click('button.btn-primary', timeout=4000)
        except Exception:
            pass

        frame = await find_frame(page, timeout_s=30)
        if not frame:
            print("Frame not found!")
            await asyncio.sleep(10)
            return

        print(f"  Frame: {frame.url[:70]}")
        await page.wait_for_timeout(2000)

        # ── Step 1: click Auto tab on Panel 1 ───────────────────────────────
        print("\n[3] Clicking Auto tab on Panel 1…")
        auto_tabs = [t for t in await frame.query_selector_all('button.tab')
                     if (await t.inner_text()).strip() == "Auto"]
        print(f"  Found {len(auto_tabs)} Auto tabs")
        await auto_tabs[0].click()
        await asyncio.sleep(0.8)

        # ── Step 2: enable Auto Cash Out toggle on Panel 1 ──────────────────
        print("[4] Enabling Auto Cash Out toggle…")
        switchers = await frame.query_selector_all('.cash-out-switcher')
        if switchers:
            toggle = await switchers[0].query_selector('.input-switch')
            cls = await toggle.get_attribute("class") or ""
            print(f"  Toggle class: {cls!r}")
            if "off" in cls:
                await toggle.click()
                await asyncio.sleep(0.8)
                cls2 = await toggle.get_attribute("class") or ""
                print(f"  Toggle class after click: {cls2!r}")

        print("\n  State after enabling toggle:")
        await read_cashout_inputs(frame)

        # ── Step 3: find cashout input and try every method ──────────────────
        cashout_inputs = []
        for inp in await frame.query_selector_all('input[placeholder=""]'):
            if await inp.is_visible():
                cashout_inputs.append(inp)

        print(f"\n  Found {len(cashout_inputs)} visible cashout input(s)")
        if not cashout_inputs:
            print("  ERROR: no visible cashout input found")
            await asyncio.sleep(60)
            return

        target_inp = cashout_inputs[0]

        # Method 1: ctrl+a + type + Tab
        print("\n[5a] Method 1: ctrl+a → type → Tab")
        await target_inp.click()
        await asyncio.sleep(0.1)
        await target_inp.press("Control+a")
        await asyncio.sleep(0.1)
        await target_inp.type("6.00", delay=80)
        await asyncio.sleep(0.2)
        val_before_tab = await target_inp.input_value()
        print(f"  Value before Tab: {val_before_tab!r}")
        await target_inp.press("Tab")
        await asyncio.sleep(0.5)
        val_after_tab = await target_inp.input_value()
        print(f"  Value after Tab:  {val_after_tab!r}  ← did it stick?")

        # Method 2: ctrl+a + type + Enter
        print("\n[5b] Method 2: ctrl+a → type → Enter")
        await target_inp.click()
        await asyncio.sleep(0.1)
        await target_inp.press("Control+a")
        await asyncio.sleep(0.1)
        await target_inp.type("6.00", delay=80)
        await asyncio.sleep(0.2)
        await target_inp.press("Enter")
        await asyncio.sleep(0.5)
        val2 = await target_inp.input_value()
        print(f"  Value after Enter: {val2!r}")

        # Method 3: fill + blur click elsewhere
        print("\n[5c] Method 3: fill → click elsewhere to blur")
        await target_inp.fill("6.00")
        await asyncio.sleep(0.1)
        # click somewhere neutral to blur
        await frame.evaluate("document.activeElement && document.activeElement.blur()")
        await asyncio.sleep(0.5)
        val3 = await target_inp.input_value()
        print(f"  Value after fill+blur: {val3!r}")

        # Method 4: JS angular setValue
        print("\n[5d] Method 4: Angular setValue via __ngContext__")
        await frame.evaluate("""
            (el, val) => {
                // Try Ivy context
                const ctx = el.__ngContext__;
                if (ctx) {
                    for (let i = 0; i < ctx.length; i++) {
                        const c = ctx[i];
                        if (c && c.control && typeof c.control.setValue === 'function') {
                            c.control.setValue(parseFloat(val));
                            c.control.markAsDirty();
                            c.control.updateValueAndValidity();
                            break;
                        }
                    }
                }
                // Also native setter
                const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                s.call(el, val);
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur',   {bubbles: true}));
            }
        """, target_inp, "6.00")
        await asyncio.sleep(0.5)
        val4 = await target_inp.input_value()
        print(f"  Value after Angular setValue: {val4!r}")

        # Method 5: use minus button to set to minimum, then type
        print("\n[5e] Method 5: check for spinner +/- buttons near cashout input")
        spinner_btns = await frame.evaluate("""
            (inp) => {
                const wrapper = inp.closest('.cashout-spinner-wrapper') ||
                                inp.closest('.cashout-spinner') ||
                                inp.parentElement;
                if (!wrapper) return 'no wrapper';
                const btns = wrapper.querySelectorAll('button, .btn');
                return Array.from(btns).map(b => ({
                    cls: b.className,
                    txt: b.innerText
                }));
            }
        """, target_inp)
        print(f"  Spinner buttons near input: {spinner_btns}")

        print("\n[6] Final input state:")
        await read_cashout_inputs(frame)

        await page.screenshot(path="screenshot_cashout_test.png", full_page=False)
        print("\n  Screenshot → screenshot_cashout_test.png")
        print("Browser stays open 90s — check the UI manually.")
        await asyncio.sleep(90)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
