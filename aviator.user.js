// ==UserScript==
// @name         Aviator Strategy Bot
// @namespace    https://aviator.dafeapp.com
// @version      1.0.0
// @description  Two-panel automated strategy bot for SportPesa Aviator — runs in your browser, no server needed
// @author       Aviator Bot
// @match        *://*.spribegaming.com/*
// @match        *://ke.sportpesa.com/en/casino/aviator*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // ── Context guard ─────────────────────────────────────────────────────────
    // This script runs both in the SportPesa parent page and the Spribe iframe.
    // Bot logic only runs in the Spribe game iframe.
    const IN_GAME = window.location.hostname.includes('spribegaming.com');
    const IN_SPORTPESA = window.location.hostname.includes('sportpesa.com');

    if (!IN_GAME && !IN_SPORTPESA) return;

    // If we're in the SportPesa parent page, just show a small badge indicating
    // the bot is ready. The actual bot runs inside the Spribe iframe below.
    if (IN_SPORTPESA && !IN_GAME) {
        waitForElement('iframe', 60000).then(() => {
            const badge = document.createElement('div');
            badge.id = 'aviator-badge';
            badge.textContent = '🤖 Aviator Bot active — see the game panel';
            badge.style.cssText = [
                'position:fixed', 'top:10px', 'right:10px', 'z-index:99999',
                'background:rgba(0,0,0,0.75)', 'color:#00e676', 'padding:6px 12px',
                'border-radius:6px', 'font:12px monospace', 'pointer-events:none',
            ].join(';');
            document.body.appendChild(badge);
        });
        return;
    }

    // ── Default config (mirrors PROD_V2_ORIG config.py) ───────────────────────
    const DEFAULTS = {
        BET_AMOUNT:               1,
        P2_BET_AMOUNT:            1,
        PANEL1_CASHOUT:           2.5,
        PANEL2_CASHOUT:           3.5,
        RECOVERY_PROFIT_TARGET:   1,
        RECOVERY_SCOPE:           'smart',       // individual | combined | smart
        P1_ASSIST_P2_ENABLED:     true,
        P1_ASSIST_PERCENTAGE:     100,
        P1_ASSIST_TRIGGER_MAX:    1.4,
        P1_ASSIST_CASHOUT:        1.4,
        P2_RECOVERY_ENABLED:      true,
        P2_RECOVERY_PROFIT_TARGET:1,
        P2_RECOVERY_SCOPE:        'combined',    // individual | combined
        P1_TRIGGER_MULT:          2.5,
        P2_LOW_STREAK_MIN:        1.4,
        P2_LOW_STREAK_MAX:        3.5,
        STOP_ON_PROFIT:           500,
        STOP_ON_LOSS:             -10000,
        MAX_RECOVERY_BET:         0,             // 0 = no cap
        MAX_P2_BET:               0,             // 0 = no cap
        MAX_ASSIST_BET:           0,             // 0 = no cap
        RECOVERY_DEFICIT_CAP:     0,             // 0 = disabled
        BURST_COOLDOWN:           0,
        TRIGGER_LOSS_COOLDOWN:    0,
        STOP_ON_CONSECUTIVE_LOSSES: 0,           // 0 = disabled
    };

    let cfg = { ...DEFAULTS };
    try {
        const saved = GM_getValue('aviator_cfg', null);
        if (saved) cfg = { ...DEFAULTS, ...JSON.parse(saved) };
    } catch (_) {}

    function saveConfig() {
        GM_setValue('aviator_cfg', JSON.stringify(cfg));
    }

    // ── Session state ─────────────────────────────────────────────────────────
    const state = {
        running:        false,
        status:         'stopped',   // stopped | watching | betting
        p1Deficit:      0,
        p2Deficit:      0,
        cumulativePnl:  0,
        highestPnl:     0,
        lowestPnl:      0,
        totalRounds:    0,
        totalWins:      0,
        totalLosses:    0,
        p1Bet:          cfg.BET_AMOUNT,
        p2Bet:          cfg.P2_BET_AMOUNT,
        p1Plan:         [],          // [true/false] bets remaining in current burst
        p1AssistPlan:   [],          // parallel plan: is each p1 step an assist?
        p2Plan:         [],
        p1Cooldown:     0,
        p2Cooldown:     0,
        p1ConsecLosses: 0,
        p2ConsecLosses: 0,
        csvRows:        [],
        logs:           [],
        loopAbort:      null,        // AbortController signal
    };

    // ── DOM selectors ─────────────────────────────────────────────────────────
    const SEL = {
        betInputs:      'input[placeholder="1"], input[placeholder="0.1"]',
        cashoutSpinner: '.cashout-spinner-wrapper input, .cashout-spinner input',
        cashoutToggle:  '.cash-out-switcher .input-switch, .cashout-block .input-switch',
        betBtn:         'button.btn-success.bet',
        history:        'div.result-history',
        autoTab:        'button.tab',
    };

    // ── Utilities ─────────────────────────────────────────────────────────────
    const sleep = ms => new Promise(r => setTimeout(r, ms));

    function waitForElement(sel, timeoutMs = 30000) {
        return new Promise(resolve => {
            const el = document.querySelector(sel);
            if (el) return resolve(el);
            const obs = new MutationObserver(() => {
                const found = document.querySelector(sel);
                if (found) { obs.disconnect(); resolve(found); }
            });
            obs.observe(document.body, { childList: true, subtree: true });
            setTimeout(() => { obs.disconnect(); resolve(null); }, timeoutMs);
        });
    }

    // ── Angular reactive-form input setter ────────────────────────────────────
    // Angular's cashout spinner ONLY responds to real keyboard-event paths.
    // execCommand('insertText') goes through the browser's native editing
    // pipeline (same as physical keystrokes) so Angular's (input) handler fires.
    // Native-setter + dispatchEvent alone does NOT update the Angular model for
    // the cashout spinner — verified by P&L diverging from real balance.
    function setAngularInput(el, value) {
        if (!el) return;
        const str = String(value);
        el.focus();
        el.select();   // select all existing text

        // Primary: execCommand fires the full keyboard-event chain Angular needs
        const inserted = document.execCommand('insertText', false, str);

        if (!inserted) {
            // Fallback for browsers that block execCommand (uncommon in Tampermonkey)
            const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(el, str);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Blur/Tab triggers Angular validators and commits the value
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', code: 'Tab', bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        el.blur();
    }

    // ── Crash history ─────────────────────────────────────────────────────────
    function getCrashHistory() {
        const el = document.querySelector(SEL.history);
        if (!el) return [];
        const result = [];
        for (const token of el.innerText.trim().split(/\s+/)) {
            const v = parseFloat(token.replace(/x/i, '').replace(',', '.'));
            if (!isNaN(v) && v > 0) result.push(v);
        }
        return result;
    }

    // ── Bet-phase detection ───────────────────────────────────────────────────
    async function waitForBetPhase(timeoutMs = 4000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (document.querySelectorAll(SEL.betBtn).length >= 1) return true;
            await sleep(250);
        }
        return false;
    }

    async function waitForRoundEnd(prevHistory, timeoutMs = 90000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (!state.running) return prevHistory;
            const hist = getCrashHistory();
            if (hist.length && (!prevHistory.length || hist[0] !== prevHistory[0])) return hist;
            await sleep(250);
        }
        return getCrashHistory();
    }

    // ── Bet sizing ────────────────────────────────────────────────────────────
    function calcP1Bet(p1Def, p2Def) {
        let target;
        if (cfg.RECOVERY_SCOPE === 'individual') {
            target = p1Def > 0 ? p1Def
                : (cfg.P1_ASSIST_P2_ENABLED && p2Def > 0 ? p2Def * cfg.P1_ASSIST_PERCENTAGE / 100 : 0);
        } else {
            target = p1Def + p2Def;   // combined / smart
        }
        if (target <= 0) return cfg.BET_AMOUNT;
        const net = Math.max(0.01, cfg.PANEL1_CASHOUT - 1);
        let bet = Math.max(cfg.BET_AMOUNT, Math.round((target + cfg.RECOVERY_PROFIT_TARGET) / net * 100) / 100);
        if (cfg.MAX_RECOVERY_BET > 0) bet = Math.min(bet, cfg.MAX_RECOVERY_BET);
        return bet;
    }

    function calcP2Bet(p1Def, p2Def) {
        if (!cfg.P2_RECOVERY_ENABLED) return cfg.P2_BET_AMOUNT;
        const target = cfg.P2_RECOVERY_SCOPE === 'combined' ? p1Def + p2Def : p2Def;
        if (target <= 0) return cfg.P2_BET_AMOUNT;
        const net = Math.max(0.01, cfg.PANEL2_CASHOUT - 1);
        let bet = Math.max(cfg.P2_BET_AMOUNT, Math.round((target + cfg.P2_RECOVERY_PROFIT_TARGET) / net * 100) / 100);
        if (cfg.MAX_P2_BET > 0) bet = Math.min(bet, cfg.MAX_P2_BET);
        return bet;
    }

    function calcP1AssistBet(p2Def) {
        if (!cfg.P1_ASSIST_P2_ENABLED || p2Def <= 0) return cfg.BET_AMOUNT;
        const target = p2Def * cfg.P1_ASSIST_PERCENTAGE / 100;
        const net = Math.max(0.01, cfg.P1_ASSIST_CASHOUT - 1);
        let bet = Math.max(cfg.BET_AMOUNT, Math.round((target + cfg.RECOVERY_PROFIT_TARGET) / net * 100) / 100);
        if (cfg.MAX_ASSIST_BET > 0) bet = Math.min(bet, cfg.MAX_ASSIST_BET);
        return bet;
    }

    // ── Panel management ──────────────────────────────────────────────────────
    async function setupPanel(idx, cashout, betAmt) {
        // 1. Click the "Auto" tab if not already active
        const autoTabs = [...document.querySelectorAll(SEL.autoTab)]
            .filter(t => t.innerText.trim() === 'Auto');
        if (idx < autoTabs.length) {
            const tab = autoTabs[idx];
            if (!tab.className.includes('active')) { tab.click(); await sleep(400); }
        }

        // 2. Enable the Auto Cash Out toggle (it has class "off" when disabled)
        // Use only .cash-out-switcher — the compound selector mixed two types and
        // broke index-based lookup (switchers[1] hit P1's .cashout-block instead of P2's toggle).
        const switchers = [...document.querySelectorAll('.cash-out-switcher')];
        if (idx < switchers.length) {
            const toggle = switchers[idx].querySelector('.input-switch');
            if (toggle && toggle.className.includes('off')) { toggle.click(); await sleep(500); }
        }

        // 3. Set the cashout multiplier
        const spinners = [...document.querySelectorAll(SEL.cashoutSpinner)]
            .filter(el => el.offsetParent !== null);   // visible only
        if (idx < spinners.length) {
            setAngularInput(spinners[idx], cashout);
            await sleep(150);
        }

        // 4. Set the bet amount
        const betInputs = [...document.querySelectorAll(SEL.betInputs)];
        if (idx < betInputs.length) {
            setAngularInput(betInputs[idx], betAmt);
            await sleep(150);
        }
    }

    async function setupPanels() {
        await setupPanel(0, cfg.PANEL1_CASHOUT, cfg.BET_AMOUNT);
        await sleep(300);
        await setupPanel(1, cfg.PANEL2_CASHOUT, cfg.P2_BET_AMOUNT);
    }

    async function setP1Bet(amount) {
        const inputs = [...document.querySelectorAll(SEL.betInputs)];
        if (inputs[0]) setAngularInput(inputs[0], amount);
    }

    async function setP2Bet(amount) {
        const inputs = [...document.querySelectorAll(SEL.betInputs)];
        if (inputs[1]) setAngularInput(inputs[1], amount);
    }

    // ── Place bets ────────────────────────────────────────────────────────────
    function placeBets(p1 = true, p2 = true) {
        const btns = document.querySelectorAll(SEL.betBtn);
        if (p1 && btns[0]) btns[0].click();
        if (p2 && btns[1]) btns[1].click();
    }

    // ── P&L calculation ───────────────────────────────────────────────────────
    function calcRoundPnl(crashMult, p1Bet, p2Bet, p1Cashout) {
        const co1 = p1Cashout ?? cfg.PANEL1_CASHOUT;
        const co2 = cfg.PANEL2_CASHOUT;
        let pnl = crashMult >= co1 ? p1Bet * (co1 - 1) : -p1Bet;
        pnl    += crashMult >= co2 ? p2Bet * (co2 - 1) : -p2Bet;
        return Math.round(pnl * 100) / 100;
    }

    // ── CSV ───────────────────────────────────────────────────────────────────
    function recordCSV(crashMult, roundPnl) {
        const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
        state.csvRows.push([
            ts,
            crashMult.toFixed(2),
            roundPnl.toFixed(2),
            state.cumulativePnl.toFixed(2),
            state.cumulativePnl.toFixed(2),
            `P&L ${state.cumulativePnl >= 0 ? '+' : ''}${state.cumulativePnl.toFixed(2)} KES`,
            state.highestPnl.toFixed(2),
            state.lowestPnl.toFixed(2),
        ]);
    }

    function exportCSV() {
        const header = 'timestamp,crash_mult,round_pnl,bankroll_change,total_win,running_balance_after_bet,highest_positive,lowest_negative';
        const body   = state.csvRows.map(r => r.join(',')).join('\n');
        const blob   = new Blob([header + '\n' + body], { type: 'text/csv' });
        const a      = Object.assign(document.createElement('a'), {
            href:     URL.createObjectURL(blob),
            download: `aviator_${new Date().toISOString().slice(0, 10).replace(/-/g, '')}.csv`,
        });
        a.click();
        URL.revokeObjectURL(a.href);
    }

    // ── Logging ───────────────────────────────────────────────────────────────
    function log(msg) {
        const ts  = new Date().toTimeString().slice(0, 8);
        const line = `${ts}  ${msg}`;
        console.log('[AviatorBot]', msg);
        state.logs.push(line);
        if (state.logs.length > 60) state.logs.shift();
        updateLog();
    }

    // ── Bot control ───────────────────────────────────────────────────────────
    function startBot() {
        if (state.running) return;
        state.running       = true;
        state.status        = 'watching';
        state.p1Deficit     = 0;
        state.p2Deficit     = 0;
        state.cumulativePnl = 0;
        state.highestPnl    = 0;
        state.lowestPnl     = 0;
        state.totalRounds   = 0;
        state.totalWins     = 0;
        state.totalLosses   = 0;
        state.p1Bet         = cfg.BET_AMOUNT;
        state.p2Bet         = cfg.P2_BET_AMOUNT;
        state.p1Plan        = [];
        state.p1AssistPlan  = [];
        state.p2Plan        = [];
        state.p1Cooldown    = 0;
        state.p2Cooldown    = 0;
        state.p1ConsecLosses = 0;
        state.p2ConsecLosses = 0;
        state.csvRows       = [];
        log('Bot started');
        updateUI();
        strategyLoop();
    }

    function stopBot(reason = 'Stopped by user') {
        if (!state.running) return;
        state.running = false;
        state.status  = 'stopped';
        log(reason);
        updateUI();
        printSummary();
    }

    function printSummary() {
        const rate = state.totalRounds ? (state.totalWins / state.totalRounds * 100).toFixed(1) : '0.0';
        log('─── SESSION SUMMARY ───');
        log(`Rounds: ${state.totalRounds}  Wins: ${state.totalWins}  Losses: ${state.totalLosses}  Rate: ${rate}%`);
        log(`P&L: ${state.cumulativePnl >= 0 ? '+' : ''}${state.cumulativePnl.toFixed(2)} KES`);
        log(`High: +${state.highestPnl.toFixed(2)}  Low: ${state.lowestPnl.toFixed(2)}`);
    }

    // ── Main strategy loop ────────────────────────────────────────────────────
    async function strategyLoop() {
        await sleep(1500);
        await setupPanels();
        log(`Panels ready — P1 ${cfg.PANEL1_CASHOUT}x | P2 ${cfg.PANEL2_CASHOUT}x`);

        let history = getCrashHistory();

        while (state.running) {

            // ── Global guards ─────────────────────────────────────────────────
            if (state.cumulativePnl >= cfg.STOP_ON_PROFIT) { stopBot(`Take-profit hit: KES ${state.cumulativePnl.toFixed(2)}`); break; }
            if (state.cumulativePnl <= cfg.STOP_ON_LOSS)   { stopBot(`Stop-loss hit: KES ${state.cumulativePnl.toFixed(2)}`);   break; }

            // ── Wait for a bet window ─────────────────────────────────────────
            state.status = 'watching';
            updatePnlDisplay();

            const betOpen = await waitForBetPhase(3000);
            if (!state.running) break;

            if (!betOpen) {
                // Round already in flight — wait for it to end and loop
                history = await waitForRoundEnd(history, 90000);
                if (!state.running) break;
                // Process triggers for the round that just ended
                processTriggers(history[0], history);
                continue;
            }

            // ── Decide what each panel does this round ────────────────────────
            const p1Scheduled   = state.p1Plan.length      ? state.p1Plan.shift()      : false;
            const p1AssistStep  = state.p1AssistPlan.length ? state.p1AssistPlan.shift(): false;
            const p2Scheduled   = state.p2Plan.length      ? state.p2Plan.shift()      : false;

            const p1WasAssisting = p1Scheduled && p1AssistStep && cfg.P1_ASSIST_P2_ENABLED && state.p2Deficit > 0;
            const p1RecoveryLeads = (
                p1Scheduled && !p1WasAssisting &&
                cfg.RECOVERY_SCOPE !== 'individual' &&
                (state.p1Deficit > 0 || state.p2Deficit > 0)
            );
            const p2Suppressed = p2Scheduled && p1RecoveryLeads;
            const p1CashoutThis = p1WasAssisting ? cfg.P1_ASSIST_CASHOUT : cfg.PANEL1_CASHOUT;

            // ── Set bet sizes ─────────────────────────────────────────────────
            if (p1Scheduled) {
                const bet = p1WasAssisting
                    ? calcP1AssistBet(state.p2Deficit)
                    : calcP1Bet(state.p1Deficit, state.p2Deficit);
                if (bet !== state.p1Bet) {
                    state.p1Bet = bet;
                    if (p1WasAssisting) {
                        await setupPanel(0, cfg.P1_ASSIST_CASHOUT, bet);
                    } else {
                        await setP1Bet(bet);
                    }
                }
            }
            if (p2Scheduled) {
                const bet = p2Suppressed ? cfg.P2_BET_AMOUNT : calcP2Bet(state.p1Deficit, state.p2Deficit);
                if (bet !== state.p2Bet) { state.p2Bet = bet; await setP2Bet(bet); }
            }

            const prevHistory = getCrashHistory();

            // ── Place bets ────────────────────────────────────────────────────
            if (p1Scheduled || p2Scheduled) {
                state.status = 'betting';
                updatePnlDisplay();
                placeBets(p1Scheduled, p2Scheduled);
            }

            // ── Wait for round end ────────────────────────────────────────────
            history = await waitForRoundEnd(prevHistory, 90000);
            if (!state.running) break;

            const crashMult = history[0];

            // ── Process round result ──────────────────────────────────────────
            if (p1Scheduled || p2Scheduled) {
                const p1BetUsed = p1Scheduled ? state.p1Bet : 0;
                const p2BetUsed = p2Scheduled ? state.p2Bet : 0;
                const roundPnl  = calcRoundPnl(crashMult, p1BetUsed, p2BetUsed, p1CashoutThis);

                state.cumulativePnl = Math.round((state.cumulativePnl + roundPnl) * 100) / 100;
                state.highestPnl    = Math.max(state.highestPnl, state.cumulativePnl);
                state.lowestPnl     = Math.min(state.lowestPnl,  state.cumulativePnl);
                state.totalRounds++;
                if (roundPnl > 0) state.totalWins++; else state.totalLosses++;
                recordCSV(crashMult, roundPnl);

                log(`#${state.totalRounds} crash=${crashMult.toFixed(2)}x  round=${roundPnl >= 0 ? '+' : ''}${roundPnl.toFixed(2)}  total=${state.cumulativePnl >= 0 ? '+' : ''}${state.cumulativePnl.toFixed(2)} KES`);

                // P1 result
                if (p1Scheduled) {
                    const p1Won = crashMult >= p1CashoutThis;
                    if (p1Won) {
                        if (p1WasAssisting) {
                            const gain = Math.round(p1BetUsed * (p1CashoutThis - 1) * 100) / 100;
                            state.p2Deficit = Math.max(0, Math.round((state.p2Deficit - gain) * 100) / 100);
                            try { await setupPanel(0, cfg.PANEL1_CASHOUT, cfg.BET_AMOUNT); } catch (_) {}
                        } else {
                            state.p1Deficit = 0;
                            if (cfg.RECOVERY_SCOPE === 'combined' || cfg.RECOVERY_SCOPE === 'smart') {
                                state.p2Deficit = 0;
                            }
                        }
                        state.p1ConsecLosses = 0;
                        state.p1Plan = []; state.p1AssistPlan = [];
                        state.p1Cooldown = cfg.BURST_COOLDOWN;
                        state.p1Bet = cfg.BET_AMOUNT;
                        try { await setP1Bet(cfg.BET_AMOUNT); } catch (_) {}
                    } else {
                        state.p1Deficit = Math.round((state.p1Deficit + p1BetUsed) * 100) / 100;
                        state.p1ConsecLosses++;
                        if (cfg.STOP_ON_CONSECUTIVE_LOSSES > 0 && state.p1ConsecLosses >= cfg.STOP_ON_CONSECUTIVE_LOSSES) {
                            stopBot(`P1 consecutive loss limit (${state.p1ConsecLosses}) hit`);
                            break;
                        }
                        if (!state.p1Plan.length) {
                            state.p1Cooldown = cfg.BURST_COOLDOWN + (p1WasAssisting ? 0 : cfg.TRIGGER_LOSS_COOLDOWN);
                            state.p1Bet = cfg.BET_AMOUNT;
                            if (p1WasAssisting) {
                                try { await setupPanel(0, cfg.PANEL1_CASHOUT, cfg.BET_AMOUNT); } catch (_) {}
                            } else {
                                try { await setP1Bet(cfg.BET_AMOUNT); } catch (_) {}
                            }
                        }
                    }
                }

                // P2 result
                if (p2Scheduled) {
                    const p2Won = crashMult >= cfg.PANEL2_CASHOUT;
                    if (p2Won) {
                        if (!p2Suppressed) {
                            if (cfg.P2_RECOVERY_SCOPE === 'combined') {
                                state.p1Deficit = 0; state.p2Deficit = 0;
                            } else {
                                state.p2Deficit = 0;
                            }
                        }
                        state.p2ConsecLosses = 0;
                        state.p2Plan = [];
                        state.p2Cooldown = cfg.BURST_COOLDOWN;
                        state.p2Bet = cfg.P2_BET_AMOUNT;
                        try { await setP2Bet(cfg.P2_BET_AMOUNT); } catch (_) {}
                    } else {
                        if (!p2Suppressed) {
                            state.p2Deficit = Math.round((state.p2Deficit + state.p2Bet) * 100) / 100;
                        }
                        state.p2ConsecLosses++;
                        if (cfg.STOP_ON_CONSECUTIVE_LOSSES > 0 && state.p2ConsecLosses >= cfg.STOP_ON_CONSECUTIVE_LOSSES) {
                            stopBot(`P2 consecutive loss limit (${state.p2ConsecLosses}) hit`);
                            break;
                        }
                        if (!state.p2Plan.length) {
                            state.p2Cooldown = cfg.BURST_COOLDOWN;
                            state.p2Bet = cfg.P2_BET_AMOUNT;
                            try { await setP2Bet(cfg.P2_BET_AMOUNT); } catch (_) {}
                        }
                    }
                }

                // P1 priority recovery win clears everything
                if (p1RecoveryLeads && crashMult >= cfg.PANEL1_CASHOUT) {
                    state.p1Deficit = 0;
                    state.p2Deficit = 0;
                }

            } else {
                // Watch round — still record crash to CSV
                recordCSV(crashMult, 0);
            }

            // ── Check triggers for next round ─────────────────────────────────
            processTriggers(crashMult, history);
            updatePnlDisplay();
        }

        state.status = 'stopped';
        updateUI();
        log('Loop exited');
    }

    // ── Trigger evaluation (called after every round) ─────────────────────────
    function processTriggers(crashMult, history) {
        if (!state.p1Plan.length) {
            if (state.p1Cooldown > 0) {
                state.p1Cooldown--;
            } else {
                const combinedDef = state.p1Deficit + state.p2Deficit;
                const capActive   = cfg.RECOVERY_DEFICIT_CAP > 0 && combinedDef >= cfg.RECOVERY_DEFICIT_CAP;

                const p1TrigHigh   = crashMult > cfg.P1_TRIGGER_MULT && !capActive;
                const p1TrigAssist = cfg.P1_ASSIST_P2_ENABLED && state.p2Deficit > 0 && crashMult <= cfg.P1_ASSIST_TRIGGER_MAX;

                if (p1TrigAssist || p1TrigHigh) {
                    state.p1Plan       = [true];
                    state.p1AssistPlan = [p1TrigAssist];
                    log(`P1 trigger: crash=${crashMult.toFixed(2)}x ${p1TrigAssist ? '[ASSIST]' : '[HIGH]'}`);
                }
            }
        }

        if (!state.p2Plan.length) {
            if (state.p2Cooldown > 0) {
                state.p2Cooldown--;
            } else {
                const p2Trig = crashMult > cfg.P2_LOW_STREAK_MIN && crashMult < cfg.P2_LOW_STREAK_MAX;
                if (p2Trig) {
                    state.p2Plan = [true];
                    log(`P2 trigger: crash=${crashMult.toFixed(2)}x`);
                }
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // ── UI ────────────────────────────────────────────────────────────────────
    // ─────────────────────────────────────────────────────────────────────────

    GM_addStyle(`
        #av-panel {
            position: fixed;
            top: 10px;
            right: 10px;
            z-index: 999999;
            width: 240px;
            background: rgba(10, 12, 18, 0.93);
            border: 1px solid #2a2d3a;
            border-radius: 10px;
            font: 12px/1.5 'Segoe UI', monospace;
            color: #d0d4e0;
            box-shadow: 0 4px 24px rgba(0,0,0,0.6);
            user-select: none;
        }
        #av-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            border-bottom: 1px solid #2a2d3a;
            cursor: move;
        }
        #av-header .av-title { font-weight: 700; font-size: 13px; color: #fff; letter-spacing: .5px; }
        #av-status-dot {
            width: 9px; height: 9px;
            border-radius: 50%;
            background: #555;
            flex-shrink: 0;
        }
        #av-status-dot.watching { background: #ffd600; box-shadow: 0 0 6px #ffd600; }
        #av-status-dot.betting  { background: #00e676; box-shadow: 0 0 6px #00e676; }
        #av-status-dot.stopped  { background: #555; }

        #av-body { padding: 10px 12px; }

        .av-row { display: flex; justify-content: space-between; margin-bottom: 4px; font-size: 11px; }
        .av-label { color: #7a7f96; }
        .av-val   { color: #d0d4e0; font-weight: 600; }
        .av-val.pos { color: #00e676; }
        .av-val.neg { color: #ff5252; }
        .av-val.neu { color: #ffd600; }

        #av-pnl-big {
            text-align: center;
            font-size: 22px;
            font-weight: 800;
            padding: 6px 0 2px;
            letter-spacing: .5px;
        }
        #av-pnl-big.pos { color: #00e676; }
        #av-pnl-big.neg { color: #ff5252; }

        .av-divider { border: none; border-top: 1px solid #2a2d3a; margin: 8px 0; }

        #av-btn-row { display: flex; gap: 6px; margin-top: 8px; }
        .av-btn {
            flex: 1;
            padding: 7px 0;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font: 700 12px 'Segoe UI', monospace;
            transition: opacity .15s;
        }
        .av-btn:hover { opacity: .85; }
        #av-start-btn  { background: #00e676; color: #000; }
        #av-stop-btn   { background: #ff5252; color: #fff; }
        #av-export-btn { background: #2a2d3a; color: #d0d4e0; flex: 0 0 32px; font-size: 16px; }

        #av-log-toggle {
            width: 100%;
            background: none;
            border: none;
            border-top: 1px solid #2a2d3a;
            color: #7a7f96;
            font: 11px monospace;
            padding: 5px 12px;
            text-align: left;
            cursor: pointer;
        }
        #av-log-toggle:hover { color: #d0d4e0; }
        #av-log {
            max-height: 120px;
            overflow-y: auto;
            padding: 6px 12px 8px;
            font: 10px/1.6 monospace;
            color: #7a7f96;
            border-top: 1px solid #1a1d26;
            display: none;
        }
        #av-log .av-log-line { white-space: pre-wrap; word-break: break-all; }
        #av-log .av-log-line:last-child { color: #d0d4e0; }

        #av-cfg-toggle {
            width: 100%;
            background: none;
            border: none;
            border-top: 1px solid #2a2d3a;
            color: #7a7f96;
            font: 11px monospace;
            padding: 5px 12px;
            text-align: left;
            cursor: pointer;
        }
        #av-cfg-toggle:hover { color: #d0d4e0; }
        #av-cfg {
            padding: 8px 12px 10px;
            border-top: 1px solid #1a1d26;
            display: none;
        }
        .av-cfg-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; gap: 6px; }
        .av-cfg-row label { color: #7a7f96; font-size: 10px; white-space: nowrap; }
        .av-cfg-row input, .av-cfg-row select {
            background: #1a1d26;
            border: 1px solid #2a2d3a;
            border-radius: 4px;
            color: #d0d4e0;
            font: 11px monospace;
            padding: 2px 5px;
            width: 80px;
            text-align: right;
        }
        #av-cfg-save {
            width: 100%;
            margin-top: 4px;
            padding: 5px;
            background: #2a2d3a;
            border: none;
            border-radius: 5px;
            color: #00e676;
            font: 700 11px monospace;
            cursor: pointer;
        }
        #av-cfg-save:hover { background: #353848; }
    `);

    function createPanel() {
        const panel = document.createElement('div');
        panel.id = 'av-panel';
        panel.innerHTML = `
            <div id="av-header">
                <span class="av-title">🤖 Aviator Bot</span>
                <div style="display:flex;align-items:center;gap:8px">
                    <span id="av-status-label" style="font-size:10px;color:#7a7f96">STOPPED</span>
                    <div id="av-status-dot" class="stopped"></div>
                </div>
            </div>
            <div id="av-body">
                <div id="av-pnl-big" class="neu">+0.00 KES</div>
                <hr class="av-divider">
                <div class="av-row"><span class="av-label">High</span><span class="av-val" id="av-high">+0.00</span></div>
                <div class="av-row"><span class="av-label">Low</span><span class="av-val" id="av-low">0.00</span></div>
                <div class="av-row"><span class="av-label">Rounds / Win%</span><span class="av-val" id="av-rounds">0 / 0%</span></div>
                <div class="av-row"><span class="av-label">P1 deficit</span><span class="av-val" id="av-p1def">0.00</span></div>
                <div class="av-row"><span class="av-label">P2 deficit</span><span class="av-val" id="av-p2def">0.00</span></div>
                <div id="av-btn-row">
                    <button class="av-btn" id="av-start-btn">START</button>
                    <button class="av-btn" id="av-stop-btn" disabled style="opacity:.4">STOP</button>
                    <button class="av-btn" id="av-export-btn" title="Export CSV">💾</button>
                </div>
            </div>
            <button id="av-cfg-toggle">⚙ Config ▸</button>
            <div id="av-cfg">
                ${cfgRow('BET_AMOUNT',            'P1 base bet (KES)')}
                ${cfgRow('P2_BET_AMOUNT',         'P2 base bet (KES)')}
                ${cfgRow('PANEL1_CASHOUT',        'P1 cashout')}
                ${cfgRow('PANEL2_CASHOUT',        'P2 cashout')}
                ${cfgRow('RECOVERY_PROFIT_TARGET','Recovery profit target')}
                ${cfgSelect('RECOVERY_SCOPE',     'P1 recovery scope', ['smart','combined','individual'])}
                ${cfgSelect('P2_RECOVERY_SCOPE',  'P2 recovery scope', ['individual','combined'])}
                ${cfgRow('P1_TRIGGER_MULT',       'P1 trigger (prev crash >)')}
                ${cfgRow('P2_LOW_STREAK_MIN',     'P2 trigger min')}
                ${cfgRow('P2_LOW_STREAK_MAX',     'P2 trigger max')}
                ${cfgRow('MAX_RECOVERY_BET',      'Max P1 bet (0=off)')}
                ${cfgRow('MAX_P2_BET',            'Max P2 bet (0=off)')}
                ${cfgRow('RECOVERY_DEFICIT_CAP',  'Deficit cap (0=off)')}
                ${cfgRow('STOP_ON_PROFIT',        'Take profit (KES)')}
                ${cfgRow('STOP_ON_LOSS',          'Stop loss (KES)')}
                <button id="av-cfg-save">Save config</button>
            </div>
            <button id="av-log-toggle">📋 Log ▸</button>
            <div id="av-log"></div>
        `;
        document.body.appendChild(panel);
        makeDraggable(panel, document.getElementById('av-header'));
        bindPanelEvents();
    }

    function cfgRow(key, label) {
        return `<div class="av-cfg-row">
            <label>${label}</label>
            <input type="number" id="avcfg-${key}" step="any" value="${cfg[key]}">
        </div>`;
    }

    function cfgSelect(key, label, opts) {
        const options = opts.map(o => `<option value="${o}" ${cfg[key] === o ? 'selected' : ''}>${o}</option>`).join('');
        return `<div class="av-cfg-row">
            <label>${label}</label>
            <select id="avcfg-${key}">${options}</select>
        </div>`;
    }

    function bindPanelEvents() {
        document.getElementById('av-start-btn').addEventListener('click', () => startBot());
        document.getElementById('av-stop-btn').addEventListener('click',  () => stopBot());
        document.getElementById('av-export-btn').addEventListener('click', () => {
            if (state.csvRows.length) exportCSV();
            else alert('No rounds recorded yet.');
        });

        const cfgToggle = document.getElementById('av-cfg-toggle');
        const cfgBox    = document.getElementById('av-cfg');
        cfgToggle.addEventListener('click', () => {
            const open = cfgBox.style.display === 'block';
            cfgBox.style.display = open ? 'none' : 'block';
            cfgToggle.textContent = open ? '⚙ Config ▸' : '⚙ Config ▾';
        });

        const logToggle = document.getElementById('av-log-toggle');
        const logBox    = document.getElementById('av-log');
        logToggle.addEventListener('click', () => {
            const open = logBox.style.display === 'block';
            logBox.style.display = open ? 'none' : 'block';
            logToggle.textContent = open ? '📋 Log ▸' : '📋 Log ▾';
        });

        document.getElementById('av-cfg-save').addEventListener('click', () => {
            const numKeys = [
                'BET_AMOUNT','P2_BET_AMOUNT','PANEL1_CASHOUT','PANEL2_CASHOUT',
                'RECOVERY_PROFIT_TARGET','P1_TRIGGER_MULT','P2_LOW_STREAK_MIN',
                'P2_LOW_STREAK_MAX','MAX_RECOVERY_BET','MAX_P2_BET','MAX_ASSIST_BET',
                'RECOVERY_DEFICIT_CAP','STOP_ON_PROFIT','STOP_ON_LOSS',
                'BURST_COOLDOWN','TRIGGER_LOSS_COOLDOWN','STOP_ON_CONSECUTIVE_LOSSES',
                'P1_ASSIST_PERCENTAGE','P1_ASSIST_TRIGGER_MAX','P1_ASSIST_CASHOUT',
                'P2_RECOVERY_PROFIT_TARGET','P2_RECOVERY_PERCENTAGE',
            ];
            const strKeys = ['RECOVERY_SCOPE','P2_RECOVERY_SCOPE'];
            for (const k of numKeys) {
                const el = document.getElementById(`avcfg-${k}`);
                if (el) cfg[k] = parseFloat(el.value) || 0;
            }
            for (const k of strKeys) {
                const el = document.getElementById(`avcfg-${k}`);
                if (el) cfg[k] = el.value;
            }
            saveConfig();
            log('Config saved');
        });
    }

    function updateUI() {
        const dot   = document.getElementById('av-status-dot');
        const label = document.getElementById('av-status-label');
        const startBtn = document.getElementById('av-start-btn');
        const stopBtn  = document.getElementById('av-stop-btn');
        if (!dot) return;

        dot.className = `${state.status}`;
        label.textContent = state.status.toUpperCase();

        startBtn.disabled = state.running;
        startBtn.style.opacity = state.running ? '.4' : '1';
        stopBtn.disabled  = !state.running;
        stopBtn.style.opacity = !state.running ? '.4' : '1';

        updatePnlDisplay();
    }

    function updatePnlDisplay() {
        const pnlBig = document.getElementById('av-pnl-big');
        if (!pnlBig) return;

        const pnl    = state.cumulativePnl;
        const pnlStr = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} KES`;
        pnlBig.textContent = pnlStr;
        pnlBig.className   = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';

        const high = document.getElementById('av-high');
        const low  = document.getElementById('av-low');
        const rnd  = document.getElementById('av-rounds');
        const p1d  = document.getElementById('av-p1def');
        const p2d  = document.getElementById('av-p2def');
        if (!high) return;

        high.textContent = `+${state.highestPnl.toFixed(2)}`;
        high.className   = 'av-val pos';
        low.textContent  = state.lowestPnl.toFixed(2);
        low.className    = 'av-val' + (state.lowestPnl < 0 ? ' neg' : '');
        const rate = state.totalRounds ? Math.round(state.totalWins / state.totalRounds * 100) : 0;
        rnd.textContent  = `${state.totalRounds} / ${rate}%`;
        p1d.textContent  = state.p1Deficit.toFixed(2);
        p1d.className    = 'av-val' + (state.p1Deficit > 0 ? ' neg' : '');
        p2d.textContent  = state.p2Deficit.toFixed(2);
        p2d.className    = 'av-val' + (state.p2Deficit > 0 ? ' neg' : '');
    }

    function updateLog() {
        const logBox = document.getElementById('av-log');
        if (!logBox) return;
        const last20 = state.logs.slice(-20);
        logBox.innerHTML = last20
            .map(l => `<div class="av-log-line">${l}</div>`)
            .join('');
        logBox.scrollTop = logBox.scrollHeight;
    }

    // ── Draggable panel ───────────────────────────────────────────────────────
    function makeDraggable(el, handle) {
        let ox = 0, oy = 0, mx = 0, my = 0;
        handle.addEventListener('mousedown', e => {
            e.preventDefault();
            ox = el.offsetLeft; oy = el.offsetTop;
            mx = e.clientX;     my = e.clientY;
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',   onUp);
        });
        function onMove(e) {
            el.style.left  = `${ox + e.clientX - mx}px`;
            el.style.top   = `${oy + e.clientY - my}px`;
            el.style.right = 'auto';
        }
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup',   onUp);
        }
    }

    // ── Init ──────────────────────────────────────────────────────────────────
    async function init() {
        // Wait for the Angular app to boot (bet buttons or history appear)
        await waitForElement(`${SEL.betBtn}, ${SEL.history}`, 60000);
        await sleep(1000);
        createPanel();
        updateUI();
        log('Aviator Bot ready — press START');
    }

    init();

})();
