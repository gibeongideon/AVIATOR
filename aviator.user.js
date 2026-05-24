// ==UserScript==
// @name         Aviator Strategy Bot (PROD-V2-REAL)
// @namespace    https://aviator.dafeapp.com
// @version      2.2.0
// @description  Two-panel bot — smart recovery, 50 KES base bets, 10% chunk cap, drawdown protection, MIN_TRIGGER_CRASH gate, low-zone & follow modes
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
    const IN_GAME      = window.location.hostname.includes('spribegaming.com');
    const IN_SPORTPESA = window.location.hostname.includes('sportpesa.com');

    if (!IN_GAME && !IN_SPORTPESA) return;

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

    // ── Default config (mirrors config.py) ───────────────────────────────────
    const DEFAULTS = {
        // Bet sizing
        BET_AMOUNT:                50,
        P2_BET_AMOUNT:             50,
        // Cashout targets
        PANEL1_CASHOUT:            2.5,
        PANEL2_CASHOUT:            3.5,
        // P1 recovery
        RECOVERY_ENABLED:          true,
        RECOVERY_PROFIT_TARGET:    25,
        RECOVERY_SCOPE:            'smart',       // individual | combined | percentage | smart
        RECOVERY_PERCENTAGE:       50,            // % of deficit per win (percentage scope)
        RECOVERY_STEPS:            2,             // rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
        // P1 assist P2
        P1_ASSIST_P2_ENABLED:      false,
        P1_ASSIST_PERCENTAGE:      100,
        P1_ASSIST_TRIGGER_MAX:     1.4,
        P1_ASSIST_CASHOUT:         1.4,
        // P2 recovery
        P2_RECOVERY_ENABLED:       true,
        P2_RECOVERY_PROFIT_TARGET: 25,
        P2_RECOVERY_SCOPE:         'combined',    // individual | combined | percentage | smart
        P2_RECOVERY_PERCENTAGE:    100,
        P2_RECOVERY_STEPS:         2,
        // P2 assist P1
        P2_ASSIST_P1_ENABLED:      false,
        P2_ASSIST_PERCENTAGE:      100,
        // Burst safety
        BURST_COOLDOWN:            0,
        TRIGGER_LOSS_COOLDOWN:     0,
        STOP_ON_CONSECUTIVE_LOSSES: 0,
        // Chunk cap
        RECOVERY_CHUNK_CAP:        0,             // fixed KES cap (0 = disabled)
        RECOVERY_CHUNK_CAP_PCT:    10,            // % of INITIAL_BALANCE (0 = use fixed KES above)
        INITIAL_BALANCE:           30000,
        RECOVERY_DEFICIT_CAP:      0,             // pause P1 triggers above this deficit (0 = off)
        // Global trigger gate
        MIN_TRIGGER_CRASH:         1.22,          // skip ALL triggers if prev crash < this (0 = off)
        // P1 low-zone recovery
        P1_LOW_ZONE_ENABLED:       false,
        P1_LOW_ZONE_MAX:           1.4,
        P1_LOW_ZONE_CASHOUT:       1.5,
        P1_LOW_ZONE_PERCENTAGE:    50,
        // Follow (idle-fill)
        P1_FOLLOW_P2:              false,
        P2_FOLLOW_P1:              false,
        // P1 trigger
        P1_TRIGGER_MULT:           2.5,
        P1_TRIGGER_MULT_MAX:       0,             // 0 = Infinity (no upper bound)
        // P2 trigger
        P2_LOW_STREAK_MIN:         1.4,
        P2_LOW_STREAK_MAX:         3.5,
        P2_TRIGGER_MULT_MAX:       0,             // reserved, not used in P2 high trigger
        // Global session guards
        STOP_ON_PROFIT:            3000,
        STOP_ON_LOSS:              0,             // 0 = disabled; set negative to enable (e.g. -500)
        STOP_ON_DRAWDOWN_PCT:      50,            // stop if PnL drops X% from peak (0 = off)
        // Auto-restart
        AUTO_RESTART_SESSION:      true,
        RESTART_DELAY:             10,            // seconds between sessions
    };

    // ── Named strategy presets ────────────────────────────────────────────────
    const STRATEGIES = {
        BASIC: { ...DEFAULTS },
        V1:    { ...DEFAULTS, RECOVERY_CHUNK_CAP_PCT: 10, RECOVERY_CHUNK_CAP: 0 },
    };
    STRATEGIES.AI = { ...STRATEGIES.BASIC };

    // ── AI unlock codes ───────────────────────────────────────────────────────
    const AI_UNLOCK_CODES = ['AVAI-2026-GOLD', 'AVAI-2026-PRO', 'AVAI-2026-VIP'];
    let aiUnlocked = !!GM_getValue('av_ai_unlocked', false);

    let cfg = { ...DEFAULTS };
    let activeStrategy = 'BASIC';
    try {
        const saved = GM_getValue('aviator_cfg_v2real', null);
        if (saved) {
            const parsed = JSON.parse(saved);
            activeStrategy = parsed._strategy || 'CUSTOM';
            if (activeStrategy === 'ORIG') activeStrategy = 'BASIC';
            if (activeStrategy === 'AI' && !aiUnlocked) activeStrategy = 'BASIC';
            delete parsed._strategy;
            cfg = { ...DEFAULTS, ...parsed };
        }
    } catch (_) {}

    function saveConfig() {
        GM_setValue('aviator_cfg_v2real', JSON.stringify({ ...cfg, _strategy: activeStrategy }));
    }

    function effectiveChunkCap() {
        if (cfg.RECOVERY_CHUNK_CAP_PCT > 0 && cfg.INITIAL_BALANCE > 0) {
            return Math.round(cfg.INITIAL_BALANCE * cfg.RECOVERY_CHUNK_CAP_PCT / 100 * 100) / 100;
        }
        return cfg.RECOVERY_CHUNK_CAP;
    }

    function applyStrategy(name) {
        if (STRATEGIES[name]) Object.assign(cfg, STRATEGIES[name]);
        activeStrategy = name;
        updateCfgFields();
        updateCfgReadonly();
        updateStrategyButtons();
        saveConfig();
        log(`Strategy: ${name}`);
    }

    function updateCfgFields() {
        const numKeys = [
            'BET_AMOUNT', 'P2_BET_AMOUNT', 'PANEL1_CASHOUT', 'PANEL2_CASHOUT',
            'RECOVERY_PROFIT_TARGET', 'RECOVERY_PERCENTAGE', 'RECOVERY_STEPS',
            'P2_RECOVERY_PROFIT_TARGET', 'P2_RECOVERY_PERCENTAGE', 'P2_RECOVERY_STEPS',
            'P1_TRIGGER_MULT', 'P1_TRIGGER_MULT_MAX',
            'P2_LOW_STREAK_MIN', 'P2_LOW_STREAK_MAX',
            'P1_ASSIST_TRIGGER_MAX', 'P1_ASSIST_CASHOUT', 'P1_ASSIST_PERCENTAGE',
            'P2_ASSIST_PERCENTAGE',
            'MIN_TRIGGER_CRASH',
            'P1_LOW_ZONE_MAX', 'P1_LOW_ZONE_CASHOUT', 'P1_LOW_ZONE_PERCENTAGE',
            'RECOVERY_CHUNK_CAP', 'RECOVERY_CHUNK_CAP_PCT', 'INITIAL_BALANCE',
            'RECOVERY_DEFICIT_CAP',
            'STOP_ON_PROFIT', 'STOP_ON_LOSS', 'STOP_ON_DRAWDOWN_PCT',
            'BURST_COOLDOWN', 'TRIGGER_LOSS_COOLDOWN', 'STOP_ON_CONSECUTIVE_LOSSES',
            'RESTART_DELAY',
        ];
        for (const k of numKeys) {
            const el = document.getElementById(`avcfg-${k}`);
            if (el) el.value = cfg[k];
        }
        for (const k of ['RECOVERY_SCOPE', 'P2_RECOVERY_SCOPE']) {
            const el = document.getElementById(`avcfg-${k}`);
            if (el) el.value = cfg[k];
        }
        for (const k of [
            'RECOVERY_ENABLED', 'P1_ASSIST_P2_ENABLED', 'P2_RECOVERY_ENABLED',
            'P2_ASSIST_P1_ENABLED', 'P1_LOW_ZONE_ENABLED', 'P1_FOLLOW_P2', 'P2_FOLLOW_P1',
            'AUTO_RESTART_SESSION',
        ]) {
            const el = document.getElementById(`avcfg-${k}`);
            if (el) el.checked = !!cfg[k];
        }
    }

    function updateStrategyButtons() {
        document.querySelectorAll('.av-strat-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.strat === activeStrategy);
        });
    }

    function updateCfgReadonly() {
        const cfgBox = document.getElementById('av-cfg');
        if (!cfgBox) return;
        cfgBox.classList.toggle('av-cfg-locked', activeStrategy !== 'CUSTOM');
    }

    function updateAiButton() {
        const btn = document.getElementById('av-ai-btn');
        if (!btn) return;
        if (aiUnlocked) {
            btn.classList.remove('av-strat-locked');
            btn.title = 'AI Strategy (80% win rate) — unlocked';
        } else {
            btn.classList.add('av-strat-locked');
            btn.title = 'Paid — WhatsApp +254752516673';
        }
    }

    // ── Session state ─────────────────────────────────────────────────────────
    const state = {
        running:         false,
        status:          'stopped',
        p1Deficit:       0,
        p2Deficit:       0,
        cumulativePnl:   0,
        peakPnl:         0,
        highestPnl:      0,
        lowestPnl:       0,
        pendingBet:      0,   // KES currently at risk (deducted from balance immediately on place)
        lifetimePnl:     0,   // cumulative P&L across all auto-restarted sessions
        sessionCount:    0,   // number of completed sessions (incremented on each restart)
        totalRounds:     0,
        totalWins:       0,
        totalLosses:     0,
        p1Bet:           cfg.BET_AMOUNT,
        p2Bet:           cfg.P2_BET_AMOUNT,
        p1Plan:          [],
        p1AssistPlan:    [],
        p1LowZonePlan:   [],
        p1FollowPlan:    [],
        p2Plan:          [],
        p1Cooldown:      0,
        p2Cooldown:      0,
        p1ConsecLosses:  0,
        p2ConsecLosses:  0,
        p1Step:          0,   // percentage-recovery step counter
        p2Step:          0,
        csvRows:         [],
        logs:            [],
    };

    // ── DOM selectors ─────────────────────────────────────────────────────────
    const SEL = {
        betInputs:      'input[placeholder="1"], input[placeholder="0.1"]',
        cashoutSpinner: '.cashout-spinner-wrapper input, .cashout-spinner input',
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
    function setAngularInput(el, value) {
        if (!el) return;
        const str = String(value);
        el.focus();
        el.select();
        const inserted = document.execCommand('insertText', false, str);
        if (!inserted) {
            const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(el, str);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
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
    function calcP1Bet(p1Def, p2Def, step = 0, extraRisk = 0) {
        if (!cfg.RECOVERY_ENABLED) return cfg.BET_AMOUNT;
        let target;
        if (cfg.RECOVERY_SCOPE === 'individual') {
            target = p1Def > 0 ? p1Def
                : (cfg.P1_ASSIST_P2_ENABLED && p2Def > 0 ? p2Def * cfg.P1_ASSIST_PERCENTAGE / 100 : 0);
        } else if (cfg.RECOVERY_SCOPE === 'percentage') {
            const total    = p1Def + p2Def;
            const maxSteps = cfg.RECOVERY_STEPS > 0 ? cfg.RECOVERY_STEPS : 1;
            const isLast   = (step + 1) >= maxSteps;
            target = isLast ? total : total * cfg.RECOVERY_PERCENTAGE / 100;
        } else {
            target = p1Def + p2Def;   // combined / smart
        }
        if (target <= 0) return cfg.BET_AMOUNT;
        const cap = effectiveChunkCap();
        if (cap > 0 && target > cap) target = cap;
        const net = Math.max(0.01, cfg.PANEL1_CASHOUT - 1);
        return Math.max(cfg.BET_AMOUNT, Math.round((target + extraRisk + cfg.RECOVERY_PROFIT_TARGET) / net * 100) / 100);
    }

    function calcP2Bet(p1Def, p2Def, step = 0, extraRisk = 0) {
        if (!cfg.P2_RECOVERY_ENABLED) return cfg.P2_BET_AMOUNT;
        if (p1Def > 0 && cfg.P2_ASSIST_P1_ENABLED) {
            const assistTarget = p1Def * cfg.P2_ASSIST_PERCENTAGE / 100;
            const net = Math.max(0.01, cfg.PANEL2_CASHOUT - 1);
            return Math.max(cfg.P2_BET_AMOUNT, Math.round((assistTarget + extraRisk + cfg.P2_RECOVERY_PROFIT_TARGET) / net * 100) / 100);
        }
        let target;
        if (cfg.P2_RECOVERY_SCOPE === 'percentage') {
            const maxSteps = cfg.P2_RECOVERY_STEPS > 0 ? cfg.P2_RECOVERY_STEPS : 1;
            const isLast   = (step + 1) >= maxSteps;
            target = isLast ? p2Def : p2Def * cfg.P2_RECOVERY_PERCENTAGE / 100;
        } else if (cfg.P2_RECOVERY_SCOPE === 'combined') {
            target = p1Def + p2Def;
        } else {
            target = p2Def;
        }
        if (target <= 0) return cfg.P2_BET_AMOUNT;
        const cap = effectiveChunkCap();
        if (cap > 0 && target > cap) target = cap;
        const net = Math.max(0.01, cfg.PANEL2_CASHOUT - 1);
        return Math.max(cfg.P2_BET_AMOUNT, Math.round((target + extraRisk + cfg.P2_RECOVERY_PROFIT_TARGET) / net * 100) / 100);
    }

    function calcP1AssistBet(p2Def) {
        if (!cfg.P1_ASSIST_P2_ENABLED || p2Def <= 0) return cfg.BET_AMOUNT;
        const target = p2Def * cfg.P1_ASSIST_PERCENTAGE / 100;
        const cap = effectiveChunkCap();
        const capped = cap > 0 && target > cap ? cap : target;
        const net = Math.max(0.01, cfg.P1_ASSIST_CASHOUT - 1);
        return Math.max(cfg.BET_AMOUNT, Math.round((capped + cfg.RECOVERY_PROFIT_TARGET) / net * 100) / 100);
    }

    // ── Panel management ──────────────────────────────────────────────────────

    // Cycle the auto-cashout toggle OFF → ON so Angular re-registers the value.
    // The toggle can silently drop the value after page events; cycling forces a fresh binding.
    async function cycleCashoutToggle(idx) {
        const switchers = [...document.querySelectorAll('.cash-out-switcher')];
        if (idx >= switchers.length) return;
        const toggle = switchers[idx].querySelector('.input-switch');
        if (!toggle) return;
        if (!toggle.className.includes('off')) {
            toggle.click();               // turn OFF
            await sleep(250);
        }
        if (toggle.className.includes('off')) {
            toggle.click();               // turn ON
            await sleep(350);
        }
    }

    // Lightweight pre-bet refresh: cycle toggle + re-write cashout value.
    // Call this just before placing each bet to guarantee the value is live.
    async function refreshCashout(idx, cashout) {
        await cycleCashoutToggle(idx);
        const spinners = [...document.querySelectorAll(SEL.cashoutSpinner)]
            .filter(el => el.offsetParent !== null);
        if (idx < spinners.length) {
            setAngularInput(spinners[idx], cashout);
            await sleep(150);
        }
    }

    async function setupPanel(idx, cashout, betAmt) {
        const autoTabs = [...document.querySelectorAll(SEL.autoTab)]
            .filter(t => t.innerText.trim() === 'Auto');
        if (idx < autoTabs.length) {
            const tab = autoTabs[idx];
            if (!tab.className.includes('active')) { tab.click(); await sleep(400); }
        }
        // Always cycle the toggle so Angular receives a fresh binding
        await cycleCashoutToggle(idx);
        const spinners = [...document.querySelectorAll(SEL.cashoutSpinner)]
            .filter(el => el.offsetParent !== null);
        if (idx < spinners.length) { setAngularInput(spinners[idx], cashout); await sleep(200); }
        const betInputs = [...document.querySelectorAll(SEL.betInputs)];
        if (idx < betInputs.length) { setAngularInput(betInputs[idx], betAmt); await sleep(150); }
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
            `P&L ${state.cumulativePnl >= 0 ? '+' : ''}${state.cumulativePnl.toFixed(2)}`,
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
        const ts = new Date().toTimeString().slice(0, 8);
        console.log('[AviatorBot]', msg);
        state.logs.push(`${ts}  ${msg}`);
        if (state.logs.length > 60) state.logs.shift();
        updateLog();
    }

    // ── Global stop check — returns reason string, or null to keep running ────
    function shouldStop() {
        if (state.cumulativePnl > state.peakPnl) state.peakPnl = state.cumulativePnl;

        if (cfg.STOP_ON_LOSS < 0 && state.cumulativePnl <= cfg.STOP_ON_LOSS) {
            return `Stop-loss hit: ${state.cumulativePnl.toFixed(2)}`;
        }
        if (cfg.STOP_ON_DRAWDOWN_PCT > 0 && cfg.STOP_ON_PROFIT > 0) {
            if (state.peakPnl >= cfg.STOP_ON_PROFIT) {
                const allowed  = state.peakPnl * cfg.STOP_ON_DRAWDOWN_PCT / 100;
                const drawdown = state.peakPnl - state.cumulativePnl;
                if (drawdown >= allowed) {
                    return `Drawdown limit — peak ${state.peakPnl.toFixed(2)}, now ${state.cumulativePnl.toFixed(2)}`;
                }
            }
        } else if (cfg.STOP_ON_PROFIT > 0 && state.cumulativePnl >= cfg.STOP_ON_PROFIT) {
            return `Take-profit hit: ${state.cumulativePnl.toFixed(2)}`;
        }
        return null;
    }

    // ── Session reset (mirrors _reset_session in bot.py) ──────────────────────
    // Rolls up this session's P&L into lifetimePnl and resets all per-session state.
    // totalRounds / wins / losses / highestPnl / lowestPnl are preserved across sessions.
    function resetSession() {
        state.lifetimePnl   = Math.round((state.lifetimePnl + state.cumulativePnl) * 100) / 100;
        state.sessionCount++;
        state.cumulativePnl  = 0;
        state.peakPnl        = 0;
        state.p1Deficit      = 0;
        state.p2Deficit      = 0;
        state.pendingBet     = 0;
        state.p1Plan         = [];
        state.p1AssistPlan   = [];
        state.p1LowZonePlan  = [];
        state.p1FollowPlan   = [];
        state.p2Plan         = [];
        state.p1Cooldown     = 0;
        state.p2Cooldown     = 0;
        state.p1ConsecLosses = 0;
        state.p2ConsecLosses = 0;
        state.p1Step         = 0;
        state.p2Step         = 0;
        state.p1Bet          = cfg.BET_AMOUNT;
        state.p2Bet          = cfg.P2_BET_AMOUNT;
        const sign = v => v >= 0 ? '+' : '';
        log(`Session ${state.sessionCount} complete — lifetime P&L: ${sign(state.lifetimePnl)}${state.lifetimePnl.toFixed(2)}`);
    }

    // ── Bot control ───────────────────────────────────────────────────────────
    function startBot() {
        if (state.running) return;
        state.running        = true;
        state.status         = 'watching';
        state.p1Deficit      = 0;
        state.p2Deficit      = 0;
        state.cumulativePnl  = 0;
        state.peakPnl        = 0;
        state.highestPnl     = 0;
        state.lowestPnl      = 0;
        state.totalRounds    = 0;
        state.totalWins      = 0;
        state.totalLosses    = 0;
        state.p1Bet          = cfg.BET_AMOUNT;
        state.p2Bet          = cfg.P2_BET_AMOUNT;
        state.p1Plan         = [];
        state.p1AssistPlan   = [];
        state.p1LowZonePlan  = [];
        state.p1FollowPlan   = [];
        state.p2Plan         = [];
        state.p1Cooldown     = 0;
        state.p2Cooldown     = 0;
        state.p1ConsecLosses = 0;
        state.p2ConsecLosses = 0;
        state.p1Step         = 0;
        state.p2Step         = 0;
        state.pendingBet     = 0;
        state.lifetimePnl    = 0;
        state.sessionCount   = 0;
        state.csvRows        = [];
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
        log(`P&L: ${state.cumulativePnl >= 0 ? '+' : ''}${state.cumulativePnl.toFixed(2)}`);
        log(`High: +${state.highestPnl.toFixed(2)}  Low: ${state.lowestPnl.toFixed(2)}`);
    }

    // ── Main strategy loop ────────────────────────────────────────────────────
    async function strategyLoop() {
        // Outer loop: re-enters after each auto-restart
        while (state.running) {
        await sleep(1500);
        await setupPanels();
        log(`Panels ready — P1 ${cfg.PANEL1_CASHOUT}x | P2 ${cfg.PANEL2_CASHOUT}x`);

        let history = getCrashHistory();
        let autoStopReason = null;

        while (state.running) {

            const stopReason = shouldStop();
            if (stopReason) { autoStopReason = stopReason; break; }

            state.status = 'watching';
            updatePnlDisplay();

            const betOpen = await waitForBetPhase(3000);
            if (!state.running) break;

            if (!betOpen) {
                history = await waitForRoundEnd(history, 90000);
                if (!state.running) break;
                processTriggers(history[0], history);
                continue;
            }

            // ── Decide what each panel does this round ────────────────────────
            const p1Scheduled   = state.p1Plan.length       ? state.p1Plan.shift()       : false;
            const p1AssistStep  = state.p1AssistPlan.length  ? state.p1AssistPlan.shift() : false;
            const p1LowZoneStep = state.p1LowZonePlan.length ? state.p1LowZonePlan.shift(): false;
            const p1FollowStep  = state.p1FollowPlan.length  ? state.p1FollowPlan.shift() : false;
            const p2Scheduled   = state.p2Plan.length        ? state.p2Plan.shift()       : false;

            const p1WasAssisting = p1AssistStep  && cfg.P1_ASSIST_P2_ENABLED && state.p2Deficit > 0;
            const p1LowZoneThis  = p1LowZoneStep && cfg.P1_LOW_ZONE_ENABLED  && state.p1Deficit > 0;
            const p1FollowThis   = p1FollowStep  && !p1WasAssisting && !p1LowZoneThis;
            const p1This         = p1Scheduled   || p1WasAssisting || p1LowZoneThis || p1FollowThis;

            const p2AssistThis = (
                p1Scheduled && state.p1Deficit > 0 &&
                cfg.P2_ASSIST_P1_ENABLED && cfg.P2_RECOVERY_ENABLED
            );
            const p2This = p2Scheduled || p2AssistThis;

            const p1RecoveryLeads = (
                p1This && !p1WasAssisting &&
                cfg.RECOVERY_ENABLED &&
                cfg.RECOVERY_SCOPE !== 'individual' &&
                !p1LowZoneThis &&
                (state.p1Deficit > 0 || state.p2Deficit > 0)
            );
            const p2Suppressed = p2This && p1RecoveryLeads;

            let p1CashoutThis = cfg.PANEL1_CASHOUT;
            if (p1WasAssisting)  p1CashoutThis = cfg.P1_ASSIST_CASHOUT;
            else if (p1LowZoneThis) p1CashoutThis = cfg.P1_LOW_ZONE_CASHOUT;

            // ── Set bet sizes ─────────────────────────────────────────────────
            if (p1This) {
                let bet;
                if (p1WasAssisting) {
                    bet = calcP1AssistBet(state.p2Deficit);
                    if (bet !== state.p1Bet) {
                        state.p1Bet = bet;
                        await setupPanel(0, cfg.P1_ASSIST_CASHOUT, bet);
                    }
                } else if (p1LowZoneThis) {
                    const lzTarget = state.p1Deficit * cfg.P1_LOW_ZONE_PERCENTAGE / 100;
                    const lzNet    = Math.max(0.01, cfg.P1_LOW_ZONE_CASHOUT - 1);
                    bet = lzTarget > 0
                        ? Math.max(cfg.BET_AMOUNT, Math.round((lzTarget + cfg.RECOVERY_PROFIT_TARGET) / lzNet * 100) / 100)
                        : cfg.BET_AMOUNT;
                    if (bet !== state.p1Bet) {
                        state.p1Bet = bet;
                        await setupPanel(0, cfg.P1_LOW_ZONE_CASHOUT, bet);
                    }
                } else if (p1FollowThis) {
                    bet = cfg.BET_AMOUNT;
                    if (bet !== state.p1Bet) { state.p1Bet = bet; await setP1Bet(bet); }
                } else {
                    const p2ExtraRisk = p2Suppressed ? cfg.P2_BET_AMOUNT : 0;
                    bet = calcP1Bet(state.p1Deficit, state.p2Deficit, state.p1Step, p2ExtraRisk);
                    if (bet !== state.p1Bet) { state.p1Bet = bet; await setP1Bet(bet); }
                }
            }

            if (p2This) {
                const bet = p2Suppressed
                    ? cfg.P2_BET_AMOUNT
                    : calcP2Bet(state.p1Deficit, state.p2Deficit, state.p2Step);
                if (bet !== state.p2Bet) { state.p2Bet = bet; await setP2Bet(bet); }
            }

            const prevHistory = getCrashHistory();

            // ── Refresh auto-cashout just before betting ──────────────────────
            // Cycle each active panel's toggle so Angular keeps the cashout value live.
            if (p1This) await refreshCashout(0, p1CashoutThis);
            if (p2This) await refreshCashout(1, cfg.PANEL2_CASHOUT);

            // ── Place bets ────────────────────────────────────────────────────
            if (p1This || p2This) {
                // Deduct bets from displayed balance immediately — mirrors what the site does
                state.pendingBet = (p1This ? state.p1Bet : 0) + (p2This ? state.p2Bet : 0);
                state.status = 'betting';
                updatePnlDisplay();
                placeBets(p1This, p2This);
            }

            // ── Wait for round end ────────────────────────────────────────────
            history = await waitForRoundEnd(prevHistory, 90000);
            if (!state.running) break;

            const crashMult = history[0];

            // ── Process round result ──────────────────────────────────────────
            if (p1This || p2This) {
                const p1BetUsed = p1This  ? state.p1Bet : 0;
                const p2BetUsed = p2This  ? state.p2Bet : 0;
                const roundPnl  = calcRoundPnl(crashMult, p1BetUsed, p2BetUsed, p1CashoutThis);

                state.cumulativePnl = Math.round((state.cumulativePnl + roundPnl) * 100) / 100;
                state.pendingBet    = 0;   // bets are settled — balance is live again
                state.peakPnl    = Math.max(state.peakPnl,    state.cumulativePnl);
                state.highestPnl = Math.max(state.highestPnl, state.cumulativePnl);
                state.lowestPnl  = Math.min(state.lowestPnl,  state.cumulativePnl);
                state.totalRounds++;
                if (roundPnl > 0) state.totalWins++; else state.totalLosses++;
                recordCSV(crashMult, roundPnl);

                const sign = v => v >= 0 ? '+' : '';
                log(`#${state.totalRounds} crash=${crashMult.toFixed(2)}x  round=${sign(roundPnl)}${roundPnl.toFixed(2)}  total=${sign(state.cumulativePnl)}${state.cumulativePnl.toFixed(2)}`);

                // ── P1 result ─────────────────────────────────────────────────
                if (p1This) {
                    const p1Won = crashMult >= p1CashoutThis;
                    if (p1Won) {
                        if (p1FollowThis) {
                            log(`P1 FOLLOW WIN ${crashMult.toFixed(2)}x — base bet won alongside P2`);
                        } else if (p1LowZoneThis) {
                            const gain = Math.round(p1BetUsed * (p1CashoutThis - 1) * 100) / 100;
                            state.p1Deficit = Math.max(0, Math.round((state.p1Deficit - gain) * 100) / 100);
                            log(`P1 LOW ZONE WIN ${crashMult.toFixed(2)}x @ ${p1CashoutThis}x — recovered ${gain.toFixed(2)}, deficit ${state.p1Deficit.toFixed(2)}`);
                        } else if (p1WasAssisting) {
                            const gain = Math.round(p1BetUsed * (p1CashoutThis - 1) * 100) / 100;
                            state.p2Deficit = Math.max(0, Math.round((state.p2Deficit - gain) * 100) / 100);
                            log(`P1 ASSIST WIN ${crashMult.toFixed(2)}x — P2 deficit → ${state.p2Deficit.toFixed(2)}`);
                            try { await setupPanel(0, cfg.PANEL1_CASHOUT, cfg.BET_AMOUNT); } catch (_) {}
                        } else if (cfg.RECOVERY_SCOPE === 'percentage') {
                            const total    = state.p1Deficit + state.p2Deficit;
                            const maxSteps = cfg.RECOVERY_STEPS > 0 ? cfg.RECOVERY_STEPS : 1;
                            const isLast   = (state.p1Step + 1) >= maxSteps;
                            const target   = isLast ? total : total * cfg.RECOVERY_PERCENTAGE / 100;
                            const newComb  = Math.max(0, Math.round((total - target) * 100) / 100);
                            state.p1Deficit = newComb;
                            state.p2Deficit = 0;
                            log(`P1 WIN ${crashMult.toFixed(2)}x — ${isLast ? 'full' : cfg.RECOVERY_PERCENTAGE + '%'} recovery, ${newComb.toFixed(2)} remaining`);
                        } else {
                            const coversP2  = cfg.RECOVERY_SCOPE === 'combined' || cfg.RECOVERY_SCOPE === 'smart';
                            const totalDef  = state.p1Deficit + (coversP2 ? state.p2Deficit : 0);
                            const chunk     = effectiveChunkCap() > 0 ? Math.min(totalDef, effectiveChunkCap()) : totalDef;
                            const leftover  = Math.max(0, Math.round((totalDef - chunk) * 100) / 100);
                            if (leftover > 0) log(`P1 WIN ${crashMult.toFixed(2)}x — recovered ${chunk.toFixed(2)}, ${leftover.toFixed(2)} deferred`);
                            state.p1Deficit = leftover;
                            if (coversP2) state.p2Deficit = 0;
                        }
                        state.p1ConsecLosses = 0;
                        state.p1Plan = []; state.p1AssistPlan = []; state.p1LowZonePlan = []; state.p1FollowPlan = [];
                        state.p1Cooldown = cfg.BURST_COOLDOWN;
                        state.p1Step     = 0;
                        state.p1Bet      = cfg.BET_AMOUNT;
                        try { await setP1Bet(cfg.BET_AMOUNT); } catch (_) {}
                    } else {
                        // All P1 loss types add to p1Deficit
                        state.p1Deficit = Math.round((state.p1Deficit + p1BetUsed) * 100) / 100;
                        if (p1WasAssisting) {
                            log(`P1 ASSIST LOSS ${crashMult.toFixed(2)}x — P1 takes debt → P1 deficit ${state.p1Deficit.toFixed(2)}`);
                            try { await setupPanel(0, cfg.PANEL1_CASHOUT, cfg.BET_AMOUNT); } catch (_) {}
                        } else if (p1LowZoneThis) {
                            log(`P1 LOW ZONE LOSS ${crashMult.toFixed(2)}x — deficit ${state.p1Deficit.toFixed(2)}`);
                        } else if (p1FollowThis) {
                            log(`P1 FOLLOW LOSS ${crashMult.toFixed(2)}x — deficit ${state.p1Deficit.toFixed(2)}`);
                        }
                        state.p1ConsecLosses++;
                        if (cfg.STOP_ON_CONSECUTIVE_LOSSES > 0 && state.p1ConsecLosses >= cfg.STOP_ON_CONSECUTIVE_LOSSES) {
                            autoStopReason = `P1 consecutive loss limit (${state.p1ConsecLosses}) hit`;
                            break;
                        }
                        if (!state.p1Plan.length) {
                            state.p1Cooldown    = cfg.BURST_COOLDOWN + (p1WasAssisting ? 0 : cfg.TRIGGER_LOSS_COOLDOWN);
                            state.p1LowZonePlan = [];
                            state.p1FollowPlan  = [];
                            state.p1Bet = cfg.BET_AMOUNT;
                            try { await setP1Bet(cfg.BET_AMOUNT); } catch (_) {}
                        }
                    }
                    // Advance p1Step for percentage scope
                    if (cfg.RECOVERY_SCOPE === 'percentage') {
                        const total = state.p1Deficit + state.p2Deficit;
                        if (total <= 0) {
                            state.p1Step = 0;
                        } else {
                            const maxS = cfg.RECOVERY_STEPS > 0 ? cfg.RECOVERY_STEPS : 1;
                            state.p1Step = (state.p1Step + 1) >= maxS ? 0 : state.p1Step + 1;
                        }
                    }
                }

                // ── P2 result ─────────────────────────────────────────────────
                if (p2This) {
                    const p2Won = crashMult >= cfg.PANEL2_CASHOUT;
                    if (p2Won) {
                        if (p2AssistThis && !p2Suppressed) {
                            // P2 assists P1 — subtract gain from p1Deficit
                            const gain = Math.round(p2BetUsed * (cfg.PANEL2_CASHOUT - 1) * 100) / 100;
                            state.p1Deficit = Math.max(0, Math.round((state.p1Deficit - gain) * 100) / 100);
                            log(`P2 ASSIST WIN ${crashMult.toFixed(2)}x — P1 deficit → ${state.p1Deficit.toFixed(2)}`);
                        } else if (!p2Suppressed) {
                            if (cfg.P2_RECOVERY_SCOPE === 'percentage') {
                                const maxSteps = cfg.P2_RECOVERY_STEPS > 0 ? cfg.P2_RECOVERY_STEPS : 1;
                                const isLast   = (state.p2Step + 1) >= maxSteps;
                                const target   = isLast ? state.p2Deficit : state.p2Deficit * cfg.P2_RECOVERY_PERCENTAGE / 100;
                                state.p2Deficit = Math.max(0, Math.round((state.p2Deficit - target) * 100) / 100);
                            } else if (cfg.P2_RECOVERY_SCOPE === 'combined') {
                                const total    = state.p1Deficit + state.p2Deficit;
                                const cap      = effectiveChunkCap();
                                const chunk    = cap > 0 ? Math.min(total, cap) : total;
                                const leftover = Math.max(0, Math.round((total - chunk) * 100) / 100);
                                state.p1Deficit = leftover;
                                state.p2Deficit = 0;
                            } else {
                                state.p2Deficit = 0;
                            }
                        } else {
                            log(`P2 NORMAL WIN ${crashMult.toFixed(2)}x — P1 recovery had priority; P2 deficit remains ${state.p2Deficit.toFixed(2)}`);
                        }
                        state.p2ConsecLosses = 0;
                        state.p2Plan      = [];
                        state.p2Cooldown  = cfg.BURST_COOLDOWN;
                        state.p2Step      = 0;
                        state.p2Bet       = cfg.P2_BET_AMOUNT;
                        try { await setP2Bet(cfg.P2_BET_AMOUNT); } catch (_) {}
                    } else {
                        if (!p2Suppressed) {
                            if (p2AssistThis) {
                                state.p2Deficit = Math.round((state.p2Deficit + p2BetUsed) * 100) / 100;
                                log(`P2 ASSIST LOSS ${crashMult.toFixed(2)}x — P2 deficit ${state.p2Deficit.toFixed(2)}`);
                            } else {
                                state.p2Deficit = Math.round((state.p2Deficit + state.p2Bet) * 100) / 100;
                            }
                        }
                        state.p2ConsecLosses++;
                        if (cfg.STOP_ON_CONSECUTIVE_LOSSES > 0 && state.p2ConsecLosses >= cfg.STOP_ON_CONSECUTIVE_LOSSES) {
                            autoStopReason = `P2 consecutive loss limit (${state.p2ConsecLosses}) hit`;
                            break;
                        }
                        if (!state.p2Plan.length) {
                            state.p2Cooldown = cfg.BURST_COOLDOWN;
                            state.p2Bet = cfg.P2_BET_AMOUNT;
                            try { await setP2Bet(cfg.P2_BET_AMOUNT); } catch (_) {}
                        }
                        // Advance p2Step for percentage scope
                        if (cfg.P2_RECOVERY_SCOPE === 'percentage') {
                            const total = state.p1Deficit + state.p2Deficit;
                            if (total <= 0) {
                                state.p2Step = 0;
                            } else {
                                const maxS = cfg.P2_RECOVERY_STEPS > 0 ? cfg.P2_RECOVERY_STEPS : 1;
                                state.p2Step = (state.p2Step + 1) >= maxS ? 0 : state.p2Step + 1;
                            }
                        }
                    }
                }

                // P1 priority recovery win — clears all deficits for combined/smart scope
                if (p1RecoveryLeads && crashMult >= cfg.PANEL1_CASHOUT) {
                    log(`P1 PRIORITY RECOVERY WIN ${crashMult.toFixed(2)}x — all deficits cleared`);
                    state.p1Deficit = 0;
                    state.p2Deficit = 0;
                    state.p1Step    = 0;
                    state.p2Step    = 0;
                }

            } else {
                recordCSV(crashMult, 0);
            }

            processTriggers(crashMult, history);
            updatePnlDisplay();
        }
        // ── End of inner loop ─────────────────────────────────────────────────

        state.pendingBet = 0;
        updatePnlDisplay();

        if (autoStopReason) {
            printSummary();
            if (cfg.AUTO_RESTART_SESSION) {
                log(`Session ended: ${autoStopReason}`);
                log(`Auto-restart in ${cfg.RESTART_DELAY}s... (session ${state.sessionCount + 1})`);
                resetSession();
                await sleep(cfg.RESTART_DELAY * 1000);
                if (state.running) { updateUI(); continue; }   // re-enter outer loop
            } else {
                stopBot(autoStopReason);
            }
        }

        break;   // user-stopped or no auto-restart — exit outer loop
        }
        // ── End of outer restart loop ─────────────────────────────────────────

        state.status = 'stopped';
        updateUI();
        log('Loop exited');
    }

    // ── Trigger evaluation ────────────────────────────────────────────────────
    function processTriggers(crashMult, history) {
        const minCrash = cfg.MIN_TRIGGER_CRASH || 0;
        if (minCrash > 0 && crashMult < minCrash) {
            log(`GATE: crash ${crashMult.toFixed(2)}x < MIN_TRIGGER_CRASH ${minCrash.toFixed(2)}x — all triggers skipped`);
            return;
        }

        // ── P1 triggers ───────────────────────────────────────────────────────
        if (!state.p1Plan.length) {
            if (state.p1Cooldown > 0) {
                state.p1Cooldown--;
            } else {
                const combinedDef = state.p1Deficit + state.p2Deficit;
                const capActive   = cfg.RECOVERY_DEFICIT_CAP > 0 && combinedDef >= cfg.RECOVERY_DEFICIT_CAP;
                const p1MultMax   = cfg.P1_TRIGGER_MULT_MAX > 0 ? cfg.P1_TRIGGER_MULT_MAX : Infinity;

                const p1TrigHigh   = !capActive && crashMult > cfg.P1_TRIGGER_MULT && crashMult <= p1MultMax;
                const p1TrigAssist = cfg.P1_ASSIST_P2_ENABLED && state.p2Deficit > 0 && crashMult <= cfg.P1_ASSIST_TRIGGER_MAX;
                const p1TrigLowZone = (
                    cfg.P1_LOW_ZONE_ENABLED &&
                    state.p1Deficit > 0 &&
                    crashMult <= cfg.P1_LOW_ZONE_MAX
                );

                const p1Triggered = p1TrigHigh || p1TrigAssist || p1TrigLowZone;
                if (p1Triggered) {
                    state.p1Plan        = [true];
                    state.p1AssistPlan  = [p1TrigAssist];
                    state.p1LowZonePlan = [p1TrigLowZone && !p1TrigAssist && !p1TrigHigh];
                    const reason = p1TrigAssist ? `[ASSIST crash≤${cfg.P1_ASSIST_TRIGGER_MAX}x]`
                        : p1TrigLowZone         ? `[LOW ZONE crash≤${cfg.P1_LOW_ZONE_MAX}x]`
                        : `[HIGH crash ${crashMult.toFixed(2)}x > ${cfg.P1_TRIGGER_MULT}x]`;
                    log(`P1 trigger: crash=${crashMult.toFixed(2)}x ${reason}`);
                }
            }
        }

        // ── P2 triggers ───────────────────────────────────────────────────────
        if (!state.p2Plan.length) {
            if (state.p2Cooldown > 0) {
                state.p2Cooldown--;
            } else {
                const p2Trig = crashMult > cfg.P2_LOW_STREAK_MIN && crashMult < cfg.P2_LOW_STREAK_MAX;
                if (p2Trig) {
                    state.p2Plan = [true];
                    log(`P2 trigger: crash=${crashMult.toFixed(2)}x in (${cfg.P2_LOW_STREAK_MIN}x, ${cfg.P2_LOW_STREAK_MAX}x)`);
                }
            }
        }

        // ── Follow (idle-fill) ────────────────────────────────────────────────
        if (state.p1Plan.length && !state.p2Plan.length && cfg.P2_FOLLOW_P1) {
            state.p2Plan = [true];
            log(`P2 FOLLOW P1 — base bet at ${cfg.PANEL2_CASHOUT}x alongside P1`);
        }
        if (state.p2Plan.length && !state.p1Plan.length && cfg.P1_FOLLOW_P2) {
            state.p1Plan       = [true];
            state.p1FollowPlan = [true];
            log(`P1 FOLLOW P2 — base bet at ${cfg.PANEL1_CASHOUT}x alongside P2`);
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // ── UI ────────────────────────────────────────────────────────────────────
    // ─────────────────────────────────────────────────────────────────────────

    GM_addStyle(`
        #av-panel {
            position: fixed; top: 10px; right: 10px; z-index: 999999;
            width: 260px; min-width: 200px; min-height: 120px;
            background: rgba(10,12,18,0.93);
            border: 1px solid #2a2d3a;
            border-radius: 10px;
            font: 12px/1.5 'Segoe UI', monospace;
            color: #d0d4e0;
            box-shadow: 0 4px 24px rgba(0,0,0,0.6);
            user-select: none;
            display: flex; flex-direction: column;
            resize: both; overflow: hidden;
        }
        #av-scroll { flex: 1; overflow-y: auto; min-height: 0; }
        #av-scroll::-webkit-scrollbar { width: 4px; }
        #av-scroll::-webkit-scrollbar-thumb { background: #2a2d3a; border-radius: 2px; }
        #av-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 8px 12px; border-bottom: 1px solid #2a2d3a; cursor: move;
        }
        #av-header .av-title { font-weight: 700; font-size: 13px; color: #fff; letter-spacing: .5px; }
        #av-status-dot { width: 9px; height: 9px; border-radius: 50%; background: #555; flex-shrink: 0; }
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
            text-align: center; font-size: 22px; font-weight: 800;
            padding: 6px 0 2px; letter-spacing: .5px;
        }
        #av-pnl-big.pos     { color: #00e676; }
        #av-pnl-big.neg     { color: #ff5252; }
        #av-pnl-big.betting { color: #ffd600; }
        #av-pending-row { text-align: center; font-size: 10px; color: #ffd600; min-height: 14px; margin-bottom: 2px; }
        .av-divider { border: none; border-top: 1px solid #2a2d3a; margin: 8px 0; }
        #av-btn-row { display: flex; gap: 6px; margin-top: 8px; }
        .av-btn {
            flex: 1; padding: 7px 0; border: none; border-radius: 6px;
            cursor: pointer; font: 700 12px 'Segoe UI', monospace; transition: opacity .15s;
        }
        .av-btn:hover { opacity: .85; }
        #av-start-btn  { background: #00e676; color: #000; }
        #av-stop-btn   { background: #ff5252; color: #fff; }
        #av-export-btn { background: #2a2d3a; color: #d0d4e0; flex: 0 0 32px; font-size: 16px; }
        #av-log-toggle, #av-cfg-toggle {
            width: 100%; background: none; border: none;
            border-top: 1px solid #2a2d3a; color: #7a7f96;
            font: 11px monospace; padding: 5px 12px; text-align: left; cursor: pointer;
        }
        #av-log-toggle:hover, #av-cfg-toggle:hover { color: #d0d4e0; }
        #av-log {
            overflow-y: auto;
            padding: 6px 12px 8px; font: 10px/1.6 monospace; color: #7a7f96;
            border-top: 1px solid #1a1d26; display: none;
        }
        #av-log .av-log-line { white-space: pre-wrap; word-break: break-all; }
        #av-log .av-log-line:last-child { color: #d0d4e0; }
        #av-strategy-section { padding: 8px 12px 10px; border-top: 1px solid #2a2d3a; }
        #av-cfg { padding: 8px 12px 10px; border-top: 1px solid #1a1d26; display: none; }
        .av-cfg-locked input, .av-cfg-locked select, .av-cfg-locked input[type=checkbox] {
            opacity: 0.45; pointer-events: none; cursor: default;
        }
        .av-cfg-locked #av-cfg-save { display: none; }
        .av-cfg-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; gap: 6px; }
        .av-cfg-row label { color: #7a7f96; font-size: 10px; white-space: nowrap; }
        .av-cfg-row input[type=number], .av-cfg-row select {
            background: #1a1d26; border: 1px solid #2a2d3a; border-radius: 4px;
            color: #d0d4e0; font: 11px monospace; padding: 2px 5px; width: 80px; text-align: right;
        }
        .av-cfg-row input[type=checkbox] { width: auto; cursor: pointer; accent-color: #00e676; }
        #av-strategy-row { display: flex; gap: 4px; margin-bottom: 10px; }
        .av-strat-btn {
            flex: 1; padding: 5px 0; border: 1px solid #2a2d3a; border-radius: 5px;
            cursor: pointer; font: 700 10px monospace; background: #1a1d26;
            color: #7a7f96; transition: all .15s;
        }
        .av-strat-btn:hover { color: #d0d4e0; border-color: #555; }
        .av-strat-btn.active { background: #1a2e1a; color: #00e676; border-color: #00e676; }
        @keyframes av-ai-glow {
            0%, 100% { opacity: .45; border-color: #2a2d3a; color: #888; box-shadow: none; }
            50%       { opacity: 1;   border-color: #ffd600; color: #ffd600; box-shadow: 0 0 8px #ffd600; }
        }
        .av-strat-btn.av-strat-locked { animation: av-ai-glow 1.8s ease-in-out infinite; cursor: pointer; }
        .av-strat-btn.av-strat-locked:hover {
            animation: none; opacity: 1;
            border-color: #ffd600; color: #ffd600; box-shadow: 0 0 10px #ffd600;
        }
        .av-cfg-section-title {
            font: 700 9px monospace; color: #7a7f96; letter-spacing: 1px;
            text-transform: uppercase; margin: 8px 0 5px;
        }
        #av-cfg-save {
            width: 100%; margin-top: 4px; padding: 5px;
            background: #2a2d3a; border: none; border-radius: 5px;
            color: #00e676; font: 700 11px monospace; cursor: pointer;
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
            <div id="av-scroll">
            <div id="av-body">
                <div id="av-pnl-big" class="neu">0.00</div>
                <div id="av-pending-row"></div>
                <hr class="av-divider">
                <div class="av-row"><span class="av-label">Net P&amp;L</span><span class="av-val" id="av-net-pnl">+0.00</span></div>
                <div class="av-row"><span class="av-label">Peak balance</span><span class="av-val" id="av-high">0.00</span></div>
                <div class="av-row"><span class="av-label">Low balance</span><span class="av-val" id="av-low">0.00</span></div>
                <div class="av-row"><span class="av-label">Rounds / Win%</span><span class="av-val" id="av-rounds">0 / 0%</span></div>
                <div class="av-row"><span class="av-label">P1 deficit</span><span class="av-val" id="av-p1def">0.00</span></div>
                <div class="av-row"><span class="av-label">P2 deficit</span><span class="av-val" id="av-p2def">0.00</span></div>
                <div class="av-row"><span class="av-label">Sessions</span><span class="av-val" id="av-sessions">0</span></div>
                <div class="av-row"><span class="av-label">Lifetime P&amp;L</span><span class="av-val" id="av-lifetime-pnl">+0.00</span></div>
                <div id="av-btn-row">
                    <button class="av-btn" id="av-start-btn">START</button>
                    <button class="av-btn" id="av-stop-btn" disabled style="opacity:.4">STOP</button>
                    <button class="av-btn" id="av-export-btn" title="Export CSV">💾</button>
                </div>
            </div>
            <div id="av-strategy-section">
                <div class="av-cfg-section-title">Strategy</div>
                <div id="av-strategy-row">
                    <button class="av-strat-btn" data-strat="BASIC" title="50 KES base, smart recovery, 10% chunk cap">BASIC</button>
                    <button class="av-strat-btn" data-strat="V1" title="50 KES base, 10% chunk cap (V1 preset)">V1</button>
                    <button class="av-strat-btn" data-strat="CUSTOM" title="Edit values manually">Custom</button>
                    <button class="av-strat-btn av-strat-locked" id="av-ai-btn" data-strat="AI" title="Paid — WhatsApp +254752516673">AI ✨</button>
                </div>
            </div>
            <button id="av-cfg-toggle">⚙ Settings ▸</button>
            <div id="av-cfg">
                <div class="av-cfg-section-title">Bet sizing</div>
                ${cfgRow('BET_AMOUNT',             'P1 base bet (KES)')}
                ${cfgRow('P2_BET_AMOUNT',          'P2 base bet (KES)')}
                ${cfgRow('PANEL1_CASHOUT',         'P1 cashout')}
                ${cfgRow('PANEL2_CASHOUT',         'P2 cashout')}

                <div class="av-cfg-section-title">P1 recovery</div>
                ${cfgCheck('RECOVERY_ENABLED',     'P1 recovery enabled')}
                ${cfgSelect('RECOVERY_SCOPE',      'P1 scope', ['smart','combined','individual','percentage'])}
                ${cfgRow('RECOVERY_PROFIT_TARGET', 'P1 profit target (KES)')}
                ${cfgRow('RECOVERY_PERCENTAGE',    'P1 % recovery per win')}
                ${cfgRow('RECOVERY_STEPS',         'P1 % recovery steps')}

                <div class="av-cfg-section-title">P2 recovery</div>
                ${cfgCheck('P2_RECOVERY_ENABLED',  'P2 recovery enabled')}
                ${cfgSelect('P2_RECOVERY_SCOPE',   'P2 scope', ['combined','individual','percentage'])}
                ${cfgRow('P2_RECOVERY_PROFIT_TARGET', 'P2 profit target (KES)')}
                ${cfgRow('P2_RECOVERY_PERCENTAGE', 'P2 % recovery per win')}
                ${cfgRow('P2_RECOVERY_STEPS',      'P2 % recovery steps')}

                <div class="av-cfg-section-title">P1 assist P2</div>
                ${cfgCheck('P1_ASSIST_P2_ENABLED', 'P1 assists P2')}
                ${cfgRow('P1_ASSIST_TRIGGER_MAX',  'P1 assist trigger (crash ≤)')}
                ${cfgRow('P1_ASSIST_CASHOUT',      'P1 assist cashout')}
                ${cfgRow('P1_ASSIST_PERCENTAGE',   'P1 assist % of P2 deficit')}

                <div class="av-cfg-section-title">P2 assist P1</div>
                ${cfgCheck('P2_ASSIST_P1_ENABLED', 'P2 assists P1')}
                ${cfgRow('P2_ASSIST_PERCENTAGE',   'P2 assist % of P1 deficit')}

                <div class="av-cfg-section-title">Triggers</div>
                ${cfgRow('P1_TRIGGER_MULT',        'P1 trigger (crash >)')}
                ${cfgRow('P1_TRIGGER_MULT_MAX',    'P1 trigger max (0=∞)')}
                ${cfgRow('P2_LOW_STREAK_MIN',      'P2 trigger min (crash >)')}
                ${cfgRow('P2_LOW_STREAK_MAX',      'P2 trigger max (crash <)')}
                ${cfgRow('MIN_TRIGGER_CRASH',      'Min trigger gate (0=off)')}

                <div class="av-cfg-section-title">Low-zone recovery</div>
                ${cfgCheck('P1_LOW_ZONE_ENABLED',  'P1 low-zone enabled')}
                ${cfgRow('P1_LOW_ZONE_MAX',        'Low-zone upper bound')}
                ${cfgRow('P1_LOW_ZONE_CASHOUT',    'Low-zone cashout')}
                ${cfgRow('P1_LOW_ZONE_PERCENTAGE', 'Low-zone % of P1 deficit')}

                <div class="av-cfg-section-title">Follow (idle-fill)</div>
                ${cfgCheck('P1_FOLLOW_P2',         'P1 follows P2 trigger')}
                ${cfgCheck('P2_FOLLOW_P1',         'P2 follows P1 trigger')}

                <div class="av-cfg-section-title">Chunk cap</div>
                ${cfgRow('INITIAL_BALANCE',        'Initial bankroll (KES)')}
                ${cfgRow('RECOVERY_CHUNK_CAP_PCT', 'Chunk cap % of bankroll (0=use fixed)')}
                ${cfgRow('RECOVERY_CHUNK_CAP',     'Chunk cap fixed KES (0=full)')}
                ${cfgRow('RECOVERY_DEFICIT_CAP',   'Deficit gate — pause P1 (0=off)')}

                <div class="av-cfg-section-title">Session guards</div>
                ${cfgRow('STOP_ON_PROFIT',         'Take profit (KES)')}
                ${cfgRow('STOP_ON_LOSS',           'Stop loss (KES, negative or 0)')}
                ${cfgRow('STOP_ON_DRAWDOWN_PCT',   'Drawdown % from peak (0=off)')}
                ${cfgRow('STOP_ON_CONSECUTIVE_LOSSES', 'Max consec losses (0=off)')}
                ${cfgRow('BURST_COOLDOWN',         'Burst cooldown (rounds)')}
                ${cfgRow('TRIGGER_LOSS_COOLDOWN',  'Trigger-loss cooldown')}

                <div class="av-cfg-section-title">Auto-restart</div>
                ${cfgCheck('AUTO_RESTART_SESSION', 'Auto-restart after stop')}
                ${cfgRow('RESTART_DELAY',          'Restart delay (seconds)')}

                <button id="av-cfg-save">Save config</button>
            </div>
            <button id="av-log-toggle">📋 Log ▸</button>
            <div id="av-log"></div>
            </div>
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

    function cfgCheck(key, label) {
        return `<div class="av-cfg-row">
            <label>${label}</label>
            <input type="checkbox" id="avcfg-${key}" ${cfg[key] ? 'checked' : ''}>
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
            cfgToggle.textContent = open ? '⚙ Settings ▸' : '⚙ Settings ▾';
        });

        const logToggle = document.getElementById('av-log-toggle');
        const logBox    = document.getElementById('av-log');
        logToggle.addEventListener('click', () => {
            const open = logBox.style.display === 'block';
            logBox.style.display = open ? 'none' : 'block';
            logToggle.textContent = open ? '📋 Log ▸' : '📋 Log ▾';
        });

        document.querySelectorAll('.av-strat-btn:not(.av-strat-locked)').forEach(btn => {
            btn.addEventListener('click', () => applyStrategy(btn.dataset.strat));
        });

        document.getElementById('av-ai-btn').addEventListener('click', () => {
            if (aiUnlocked) { applyStrategy('AI'); return; }
            const code = prompt('🔒 AI Strategy (80% win rate)\n\nEnter your unlock code:\n(WhatsApp +254752516673 to purchase)');
            if (!code) return;
            if (AI_UNLOCK_CODES.includes(code.trim().toUpperCase())) {
                aiUnlocked = true;
                GM_setValue('av_ai_unlocked', true);
                updateAiButton();
                applyStrategy('AI');
                log('AI Strategy unlocked!');
            } else {
                alert('Invalid code.\nWhatsApp +254752516673 to get a license.');
            }
        });

        document.querySelectorAll('#av-cfg input, #av-cfg select').forEach(el => {
            el.addEventListener('change', () => {
                if (activeStrategy !== 'CUSTOM') { activeStrategy = 'CUSTOM'; updateStrategyButtons(); }
            });
        });

        document.getElementById('av-cfg-save').addEventListener('click', () => {
            const numKeys = [
                'BET_AMOUNT', 'P2_BET_AMOUNT', 'PANEL1_CASHOUT', 'PANEL2_CASHOUT',
                'RECOVERY_PROFIT_TARGET', 'RECOVERY_PERCENTAGE', 'RECOVERY_STEPS',
                'P2_RECOVERY_PROFIT_TARGET', 'P2_RECOVERY_PERCENTAGE', 'P2_RECOVERY_STEPS',
                'P1_TRIGGER_MULT', 'P1_TRIGGER_MULT_MAX',
                'P1_ASSIST_TRIGGER_MAX', 'P1_ASSIST_CASHOUT', 'P1_ASSIST_PERCENTAGE',
                'P2_ASSIST_PERCENTAGE',
                'P2_LOW_STREAK_MIN', 'P2_LOW_STREAK_MAX',
                'MIN_TRIGGER_CRASH',
                'P1_LOW_ZONE_MAX', 'P1_LOW_ZONE_CASHOUT', 'P1_LOW_ZONE_PERCENTAGE',
                'RECOVERY_CHUNK_CAP', 'RECOVERY_CHUNK_CAP_PCT', 'INITIAL_BALANCE',
                'RECOVERY_DEFICIT_CAP',
                'STOP_ON_PROFIT', 'STOP_ON_LOSS', 'STOP_ON_DRAWDOWN_PCT',
                'BURST_COOLDOWN', 'TRIGGER_LOSS_COOLDOWN', 'STOP_ON_CONSECUTIVE_LOSSES',
                'RESTART_DELAY',
            ];
            const boolKeys = [
                'RECOVERY_ENABLED', 'P1_ASSIST_P2_ENABLED', 'P2_RECOVERY_ENABLED',
                'P2_ASSIST_P1_ENABLED', 'P1_LOW_ZONE_ENABLED', 'P1_FOLLOW_P2', 'P2_FOLLOW_P1',
                'AUTO_RESTART_SESSION',
            ];
            const strKeys = ['RECOVERY_SCOPE', 'P2_RECOVERY_SCOPE'];

            for (const k of numKeys) {
                const el = document.getElementById(`avcfg-${k}`);
                if (el) cfg[k] = parseFloat(el.value) || 0;
            }
            for (const k of boolKeys) {
                const el = document.getElementById(`avcfg-${k}`);
                if (el) cfg[k] = el.checked;
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
        const dot      = document.getElementById('av-status-dot');
        const label    = document.getElementById('av-status-label');
        const startBtn = document.getElementById('av-start-btn');
        const stopBtn  = document.getElementById('av-stop-btn');
        if (!dot) return;
        dot.className = `${state.status}`;
        label.textContent = state.status.toUpperCase();
        startBtn.disabled = state.running;
        startBtn.style.opacity = state.running ? '.4' : '1';
        stopBtn.disabled = !state.running;
        stopBtn.style.opacity = !state.running ? '.4' : '1';
        updatePnlDisplay();
    }

    function updatePnlDisplay() {
        const pnlBig = document.getElementById('av-pnl-big');
        if (!pnlBig) return;

        const initial  = cfg.INITIAL_BALANCE > 0 ? cfg.INITIAL_BALANCE : 0;
        const pnl      = state.cumulativePnl;
        const pending  = state.pendingBet;
        // Live balance: initial capital + settled P&L − bets currently at risk
        const balance  = initial + pnl - pending;

        // Big balance number
        pnlBig.textContent = balance.toLocaleString('en-KE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (pending > 0) {
            pnlBig.className = 'betting';           // orange = bet deducted, waiting for result
        } else {
            pnlBig.className = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
        }

        // Pending indicator row (empty when no bet active)
        const pendRow = document.getElementById('av-pending-row');
        if (pendRow) {
            pendRow.textContent = pending > 0 ? `▼ ${pending.toFixed(2)} at risk` : '';
        }

        const netEl = document.getElementById('av-net-pnl');
        const high  = document.getElementById('av-high');
        const low   = document.getElementById('av-low');
        const rnd   = document.getElementById('av-rounds');
        const p1d   = document.getElementById('av-p1def');
        const p2d   = document.getElementById('av-p2def');
        if (!high) return;

        // Net P&L row (session delta, always visible)
        if (netEl) {
            netEl.textContent = `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
            netEl.className   = 'av-val' + (pnl > 0 ? ' pos' : pnl < 0 ? ' neg' : '');
        }

        // Peak and low expressed as absolute balance
        const peakBal = initial + state.highestPnl;
        const lowBal  = initial + state.lowestPnl;
        high.textContent = peakBal.toLocaleString('en-KE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        high.className   = 'av-val' + (state.highestPnl > 0 ? ' pos' : '');
        low.textContent  = lowBal.toLocaleString('en-KE',  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        low.className    = 'av-val' + (state.lowestPnl < 0 ? ' neg' : '');

        const rate = state.totalRounds ? Math.round(state.totalWins / state.totalRounds * 100) : 0;
        rnd.textContent = `${state.totalRounds} / ${rate}%`;
        p1d.textContent = state.p1Deficit.toFixed(2);
        p1d.className   = 'av-val' + (state.p1Deficit > 0 ? ' neg' : '');
        p2d.textContent = state.p2Deficit.toFixed(2);
        p2d.className   = 'av-val' + (state.p2Deficit > 0 ? ' neg' : '');

        const sessEl    = document.getElementById('av-sessions');
        const ltEl      = document.getElementById('av-lifetime-pnl');
        if (sessEl) sessEl.textContent = state.sessionCount;
        if (ltEl) {
            const lt = state.lifetimePnl + state.cumulativePnl;
            ltEl.textContent = `${lt >= 0 ? '+' : ''}${lt.toFixed(2)}`;
            ltEl.className   = 'av-val' + (lt > 0 ? ' pos' : lt < 0 ? ' neg' : '');
        }
    }

    function updateLog() {
        const logBox = document.getElementById('av-log');
        if (!logBox) return;
        logBox.innerHTML = state.logs.slice(-20).map(l => `<div class="av-log-line">${l}</div>`).join('');
        logBox.scrollTop = logBox.scrollHeight;
    }

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

    async function init() {
        await waitForElement(`${SEL.betBtn}, ${SEL.history}`, 60000);
        await sleep(1000);
        createPanel();
        updateUI();
        updateStrategyButtons();
        updateCfgReadonly();
        updateAiButton();
        log('Aviator Bot v2.1 ready — press START');
    }

    init();

})();
