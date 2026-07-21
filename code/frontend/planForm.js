// Financial-planning form model — defaults, goal templates, the client-side
// validation mirror, and the request-config builder (LP-015 C2, Plan 203 Ph3;
// Risk Profile replaces instrument_params D-P208-5/6/7).
//
// This module is pure (no React) so it is trivially unit-testable and shared by
// the hook + section components. It mirrors three backend sources:
//   - v3 streamlit_app._init_session_state  → DEFAULT_* skeletons (D-P203-8)
//   - v3 streamlit_app.render_goals branching → progressive disclosure (D-P203-5)
//   - backend/app/planning/validation.py     → validateConfig (D-P203-6)
// The server's validate_plan_config remains the authoritative backstop (422).

// ── Picklists (mirror v3 streamlit_app module-level lists) ──────────────────
export const GOAL_TYPES = ['Non-Negotiable', 'Semi-Negotiable', 'Negotiable'];
export const GOAL_NATURES = ['Non-replenishing', 'Replenishing'];
export const GOAL_STRUCTURES = ['Lumpsum', 'Recurring'];
export const GOAL_START_MODES = ['Fixed', 'At retirement'];
export const GOAL_END_MODES = ['Occurrences', 'Fixed date', 'Lifetime'];
export const INVESTMENT_END_MODES = ['At retirement', 'Fixed'];
export const RECURRING_FREQUENCIES = ['Monthly', 'Quarterly', 'Half-Yearly', 'Annual'];
export const STEPUP_FREQUENCIES = ['Annual', 'Half-Yearly', 'Quarterly', 'Monthly'];

// Non-replenishing recurring span cap — mirrors
// backend/app/planning/validation.py MAX_NONREPLENISHING_SPAN_MONTHS (D-P208-1).
// Span = first-to-last occurrence gap in months:
//   Occurrences mode  → (occ - 1) * freq_months
//   Fixed-date mode   → calendar month diff start → end
//   Lifetime mode     → unconditional violation (non-replenishing Lifetime disallowed)
export const MAX_NONREPLENISHING_SPAN_MONTHS = 48;

const FREQ_TO_MONTHS = { Annual: 12, Quarterly: 3, 'Half-Yearly': 6, Monthly: 1 };

// ── Date helpers (ISO yyyy-mm-dd, the wire + <input type=date> format) ──────
export function todayISO() {
    // Local date is fine here — the advisor's "today" for the plan start.
    return new Date().toISOString().slice(0, 10);
}

export function addYearsISO(iso, years) {
    const d = new Date(iso + 'T00:00:00Z');
    d.setUTCFullYear(d.getUTCFullYear() + years);
    return d.toISOString().slice(0, 10);
}

export function addDaysISO(iso, days) {
    const d = new Date(iso + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
}

// Month-grid invariant (Plan 223, D-P223-2/3): the lowest input grain is a month,
// and the engine always assumes the 1st. Snap any ISO date string to `YYYY-MM-01`
// (drops the day). null/undefined/invalid pass through unchanged. This is the FE
// mirror of backend schemas._snap_to_month_start and is DEFENSIVE on load: legacy
// plans saved under the old day-precision engine carry day != 1, and must still
// open without crashing (D-P223-8b).
export function snapMonthStartISO(iso) {
    if (!iso || typeof iso !== 'string') return iso;
    const m = iso.match(/^(\d{4})-(\d{2})/);
    if (!m) return iso;
    return `${m[1]}-${m[2]}-01`;
}

// ── Default form state (mirror v3 _init_session_state) ──────────────────────
// One salary investment stream + one Retirement Income goal (D-P203-8).
export function makeDefaultStream(index, today) {
    // Month-grid invariant (Plan 223): all default dates snap to the 1st of the
    // month. The step-up anchor used to default to "yesterday"; on the month grid
    // the backend resets the default anchor to current_date anyway (D-P223-4), so
    // we anchor it to the 1st here for FE/BE consistency.
    const start = snapMonthStartISO(today);
    return {
        name: index === 0 ? 'Salary' : `Stream ${index + 1}`,
        amount: index === 0 ? 100000 : 50000,
        start_date: start,
        end_date_mode: 'At retirement',
        end_date: snapMonthStartISO(addYearsISO(start, index === 0 ? 30 : 20)),
        step_up_percent: 10.0,
        step_up_frequency: 'Annual',
        step_up_date: start,
    };
}

export function makeDefaultGoal(index, today) {
    // Plain generic goal skeleton (matches v3 render_goals "Add goal" default).
    return {
        name: `Goal ${index + 1}`,
        description: '',
        type: 'Non-Negotiable',
        nature: 'Non-replenishing',
        structure: 'Lumpsum',
        start_date_mode: 'Fixed',
        start_date: addYearsISO(today, 15),
        amount: 1000000,
        frequency: 'Annual',
        end_mode: 'Occurrences',
        occurrences: 1,
        end_date: null,
        inflation_percent: 6.0,
    };
}

function defaultRetirementIncomeGoal(today) {
    return {
        name: 'Retirement Income',
        description: 'Monthly income post-retirement',
        type: 'Non-Negotiable',
        nature: 'Replenishing',
        structure: 'Recurring',
        start_date_mode: 'At retirement',
        start_date: addYearsISO(today, 30),
        amount: 75000,
        frequency: 'Monthly',
        end_mode: 'Lifetime',
        occurrences: 360,
        end_date: null,
        inflation_percent: 6.0,
    };
}

export function makeDefaultForm(today = todayISO()) {
    // Month-grid invariant (Plan 223, D-P223-3): current_date snaps to the 1st of
    // the current month; every derived default date inherits day=1 from it.
    today = snapMonthStartISO(today);
    return {
        current_date: today,
        current_age: 30,
        target_lifetime: 90,
        current_corpus: 10000000,
        risk_profile: 'Balanced',
        investment_streams: [makeDefaultStream(0, today)],
        goals: [defaultRetirementIncomeGoal(today)],
        one_time_investments: [],
    };
}

// Risk-profile picklist + Core Corpus return map (D-P208-4/5).
// Mirrors backend/app/planning/schemas.py RISK_PROFILE_CORE_RETURNS.
export const RISK_PROFILES = [
    'Very Conservative',
    'Conservative',
    'Balanced',
    'Aggressive',
    'Very Aggressive',
];

// Maps profile name -> display string for the "engine will use X%" hint line.
export const RISK_PROFILE_RETURNS = {
    'Very Conservative': '8%',
    Conservative: '10%',
    Balanced: '12%',
    Aggressive: '13.5%',
    'Very Aggressive': '15%',
};

// Fixed pool-bucket returns shown next to the Core Corpus hint (Plan 210).
// Display mirror of backend engine._DEFAULT_INSTRUMENT_PARAMS (debt 0.06,
// hybrid 0.10) — these do NOT vary with the risk profile (D-P208-4).
export const FIXED_BUCKET_RETURNS = { debt: '6%', hybrid: '10%' };

// ── Goal templates (D-P203-4) ───────────────────────────────────────────────
// Each template pre-fills nature/structure/type/frequency/inflation; the advisor
// fills amount + timing. 'Custom' = the full generic form.
export const GOAL_TEMPLATES = [
    { key: 'retirement_income', label: 'Retirement Income' },
    { key: 'child_education', label: 'Child Education' },
    { key: 'marriage', label: 'Marriage' },
    { key: 'home_purchase', label: 'Home Purchase' },
    { key: 'custom', label: 'Custom' },
];

export function makeGoalFromTemplate(templateKey, index, today) {
    const base = makeDefaultGoal(index, today);
    switch (templateKey) {
        case 'retirement_income':
            return {
                ...base,
                name: 'Retirement Income',
                description: 'Monthly income post-retirement',
                nature: 'Replenishing',
                structure: 'Recurring',
                start_date_mode: 'At retirement',
                amount: 75000,
                frequency: 'Monthly',
                end_mode: 'Lifetime',
                occurrences: 360,
                end_date: null,
                inflation_percent: 6.0,
            };
        case 'child_education':
            return {
                ...base,
                name: 'Child Education',
                description: 'Annual education fees',
                nature: 'Non-replenishing',
                structure: 'Recurring',
                type: 'Non-Negotiable',
                start_date_mode: 'Fixed',
                start_date: addYearsISO(today, 12),
                amount: 1500000,
                frequency: 'Annual',
                end_mode: 'Occurrences',
                occurrences: 4,
                inflation_percent: 8.0,
            };
        case 'marriage':
            return {
                ...base,
                name: 'Marriage',
                description: 'Wedding expenses',
                nature: 'Non-replenishing',
                structure: 'Lumpsum',
                type: 'Semi-Negotiable',
                start_date_mode: 'Fixed',
                start_date: addYearsISO(today, 20),
                amount: 3000000,
                inflation_percent: 7.0,
            };
        case 'home_purchase':
            return {
                ...base,
                name: 'Home Purchase',
                description: 'Down payment / purchase',
                nature: 'Non-replenishing',
                structure: 'Lumpsum',
                type: 'Negotiable',
                start_date_mode: 'Fixed',
                start_date: addYearsISO(today, 8),
                amount: 5000000,
                inflation_percent: 6.0,
            };
        case 'custom':
        default:
            return base;
    }
}

// ── Progressive disclosure: normalise a goal after a field change (D-P203-5) ─
// Mirrors v3 render_goals branching so the engine never sees a stale field.
export function normaliseGoal(goal) {
    const g = { ...goal };
    // Replenishing goals are always a recurring payout; type is never read.
    if (g.nature === 'Replenishing') {
        g.structure = 'Recurring';
    }
    if (g.structure === 'Lumpsum') {
        // Lumpsum hides frequency/occurrences/end-mode.
        g.frequency = null;
        g.end_mode = null;
        g.occurrences = 1;
        g.end_date = null;
    } else {
        // Recurring needs a frequency + a resolved end-mode.
        if (!RECURRING_FREQUENCIES.includes(g.frequency)) g.frequency = 'Monthly';
        if (!GOAL_END_MODES.includes(g.end_mode)) g.end_mode = 'Occurrences';
        if (g.end_mode === 'Occurrences') {
            g.end_date = null;
            if (!g.occurrences || g.occurrences < 1) g.occurrences = 1;
        } else if (g.end_mode === 'Fixed date') {
            if (!g.occurrences) g.occurrences = 1;
        } else {
            // Lifetime → no count, no end-date.
            g.end_date = null;
        }
    }
    return g;
}

// ── Client-side validation (mirror backend validate_plan_config) — D-P203-6 ──
// Returns { spanMonths, isLifetime } for a NON-replenishing recurring goal.
// Mirrors backend validation._nonreplenishing_span_months exactly (D-P208-1).
function nonReplenishingSpanMonths(goal) {
    if (goal.structure !== 'Recurring') return { spanMonths: 0, isLifetime: false };
    const endMode = goal.end_mode || 'Occurrences';
    const freqMonths = FREQ_TO_MONTHS[goal.frequency];

    if (endMode === 'Lifetime') {
        return { spanMonths: MAX_NONREPLENISHING_SPAN_MONTHS + 1, isLifetime: true };
    }
    if (freqMonths === undefined) {
        // Invalid frequency — let the frequency-error path handle it.
        return { spanMonths: 0, isLifetime: false };
    }
    if (endMode === 'Occurrences') {
        const occ = Number(goal.occurrences || 1);
        return { spanMonths: Math.max(0, (occ - 1) * freqMonths), isLifetime: false };
    }
    if (endMode === 'Fixed date') {
        if (!goal.start_date || !goal.end_date) return { spanMonths: 0, isLifetime: false };
        const start = new Date(goal.start_date + 'T00:00:00Z');
        const end = new Date(goal.end_date + 'T00:00:00Z');
        if (end < start) return { spanMonths: 0, isLifetime: false };
        const monthsSpan =
            (end.getUTCFullYear() - start.getUTCFullYear()) * 12 +
            (end.getUTCMonth() - start.getUTCMonth());
        return { spanMonths: monthsSpan, isLifetime: false };
    }
    return { spanMonths: 0, isLifetime: false };
}

function isNum(v) {
    return v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v));
}

/**
 * Validate the whole form. Returns a keyed error map:
 *   { personal: [..], streams: {idx: [..]}, goals: {idx: [..]}, oneTime: {idx: [..]} }
 * Empty objects/arrays mean no errors. `hasErrors(map)` is the Run gate.
 */
export function validateConfig(form) {
    const errors = { personal: [], streams: {}, goals: {}, oneTime: {} };

    // Personal & corpus
    if (!isNum(form.current_corpus)) {
        errors.personal.push('Current corpus must be a number');
    } else if (Number(form.current_corpus) < 0) {
        errors.personal.push('Current corpus must be ≥ 0');
    }
    if (!isNum(form.current_age) || !isNum(form.target_lifetime)) {
        errors.personal.push('Current age and target lifetime must be numbers');
    } else if (Number(form.target_lifetime) <= Number(form.current_age)) {
        errors.personal.push('Target lifetime must be greater than current age');
    }

    // Investment streams
    (form.investment_streams || []).forEach((s, i) => {
        const e = [];
        if (!isNum(s.amount)) e.push('Amount must be a number');
        else if (Number(s.amount) < 0) e.push('Amount must be ≥ 0');
        if (s.end_date_mode === 'Fixed') {
            if (!s.end_date) e.push('Fixed end mode requires an end date');
            else if (s.start_date && new Date(s.end_date) < new Date(s.start_date)) {
                e.push('End date must be on or after start date');
            }
        }
        if (e.length) errors.streams[i] = e;
    });

    // Goals
    (form.goals || []).forEach((g, i) => {
        const e = [];
        if (!isNum(g.amount)) e.push('Amount must be a number');
        else if (Number(g.amount) < 0) e.push('Amount must be ≥ 0');
        if (g.structure === 'Recurring') {
            if (!RECURRING_FREQUENCIES.includes(g.frequency)) {
                e.push('Recurring goal needs a valid frequency');
            }
            const endMode = g.end_mode || 'Occurrences';
            if (endMode === 'Occurrences') {
                if (!isNum(g.occurrences) || Number(g.occurrences) < 1) {
                    e.push('Number of payments must be ≥ 1');
                }
            } else if (endMode === 'Fixed date') {
                if (!g.end_date) e.push('Fixed-date end requires an end date');
                else if (g.start_date && new Date(g.end_date) < new Date(g.start_date)) {
                    e.push('End date must be on or after start date');
                }
            }
            // Non-replenishing recurring span cap (D-P208-1).
            if (g.nature !== 'Replenishing') {
                const { spanMonths, isLifetime } = nonReplenishingSpanMonths(g);
                if (isLifetime) {
                    e.push(
                        `Non-replenishing recurring goal with Lifetime end mode ` +
                        `spans more than 4 years; shorten it or model it as a Replenishing goal.`
                    );
                } else if (spanMonths > MAX_NONREPLENISHING_SPAN_MONTHS) {
                    e.push(
                        `Non-replenishing recurring goal spans ${spanMonths} months ` +
                        `(more than 4 years); shorten it or model it as a Replenishing goal.`
                    );
                }
            }
        }
        if (e.length) errors.goals[i] = e;
    });

    // One-time investments
    (form.one_time_investments || []).forEach((w, i) => {
        const e = [];
        if (w.amount !== null && w.amount !== undefined && w.amount !== '') {
            if (!isNum(w.amount)) e.push('Amount must be a number');
            else if (Number(w.amount) < 0) e.push('Amount must be ≥ 0');
        }
        if (e.length) errors.oneTime[i] = e;
    });

    return errors;
}

export function hasErrors(errorMap) {
    if (!errorMap) return false;
    if (errorMap.personal && errorMap.personal.length) return true;
    for (const key of ['streams', 'goals', 'oneTime']) {
        const group = errorMap[key] || {};
        if (Object.keys(group).length) return true;
    }
    return false;
}

// ── Build the request body the API expects (PlanSimulateRequest) ────────────
// client_name / m3_id are injected server-side (D-P203-11) — not sent here.
// instrument_params removed (D-P208-5/7); risk_profile drives core_corpus.return
// via the backend RISK_PROFILE_CORE_RETURNS mapping (D-P208-4).
// Month-grid invariant (Plan 223, D-P223-2): buildConfig ALWAYS emits day=1
// dates (the 1st of the chosen month+year). The MonthYearField already produces
// `YYYY-MM-01`, but we snap defensively here too so any path that still carries a
// day-precision string (e.g. a not-yet-migrated draft) is normalised on the wire.
// The backend re-snaps at the Pydantic boundary; this is the FE mirror.
export function buildConfig(form) {
    return {
        current_date: snapMonthStartISO(form.current_date),
        current_age: Number(form.current_age),
        target_lifetime: Number(form.target_lifetime),
        current_corpus: Number(form.current_corpus),
        risk_profile: form.risk_profile || 'Balanced',
        investment_streams: (form.investment_streams || []).map((s) => ({
            name: s.name,
            amount: Number(s.amount),
            start_date: snapMonthStartISO(s.start_date),
            end_date_mode: s.end_date_mode,
            end_date: s.end_date_mode === 'Fixed' ? snapMonthStartISO(s.end_date) : null,
            step_up_percent: Number(s.step_up_percent),
            step_up_frequency: s.step_up_frequency,
            step_up_date: s.step_up_date ? snapMonthStartISO(s.step_up_date) : null,
        })),
        goals: (form.goals || []).map((raw) => {
            const g = normaliseGoal(raw);
            return {
                name: g.name,
                description: g.description || '',
                type: g.type,
                nature: g.nature,
                structure: g.structure,
                start_date_mode: g.start_date_mode,
                start_date:
                    g.start_date_mode === 'At retirement'
                        ? null
                        : snapMonthStartISO(g.start_date),
                amount: Number(g.amount),
                frequency: g.frequency,
                occurrences: g.occurrences != null ? Number(g.occurrences) : null,
                end_mode: g.end_mode,
                end_date: snapMonthStartISO(g.end_date),
                inflation_percent: Number(g.inflation_percent),
            };
        }),
        one_time_investments: (form.one_time_investments || []).map((w) => ({
            name: w.name,
            date: snapMonthStartISO(w.date),
            amount: Number(w.amount),
        })),
    };
}

// -- Reverse of buildConfig: seed a form from a stored inputs_json (C4, Plan 206)
// The stored inputs are a PlanSimulateRequest dump (ISO date strings).
// risk_profile: missing or unrecognised → 'Balanced' (D-P208-6).
// Legacy instrument_params in stored inputs are silently ignored (D-P208-6).
//
// Month-grid invariant (Plan 223, D-P223-8b): saved plans now exist, and a plan
// saved under the old day-precision engine carries dates with day != 1. Every
// stored date is DEFENSIVELY snapped to `YYYY-MM-01` via snapMonthStartISO so the
// MonthYearField pickers read the month/year cleanly and a legacy plan still opens
// without crashing. This drops the day; the day is meaningless on the month grid.
export function formFromInputs(inputs) {
    const base = makeDefaultForm();
    if (!inputs || typeof inputs !== 'object') return base;

    const currentDate = snapMonthStartISO(inputs.current_date) ?? base.current_date;

    const streams = Array.isArray(inputs.investment_streams)
        ? inputs.investment_streams.map((s, i) => {
            const def = makeDefaultStream(i, currentDate || todayISO());
            return {
                ...def,
                name: s.name ?? def.name,
                amount: s.amount ?? def.amount,
                start_date: snapMonthStartISO(s.start_date) ?? def.start_date,
                end_date_mode: s.end_date_mode ?? def.end_date_mode,
                end_date: snapMonthStartISO(s.end_date) ?? null,
                step_up_percent: s.step_up_percent ?? def.step_up_percent,
                step_up_frequency: s.step_up_frequency ?? def.step_up_frequency,
                step_up_date: snapMonthStartISO(s.step_up_date) ?? def.step_up_date,
            };
        })
        : base.investment_streams;

    const goals = Array.isArray(inputs.goals)
        ? inputs.goals.map((g) =>
            normaliseGoal({
                name: g.name ?? '',
                description: g.description ?? '',
                type: g.type ?? 'Non-Negotiable',
                nature: g.nature ?? 'Non-replenishing',
                structure: g.structure ?? 'Lumpsum',
                // A null start_date means "At retirement" was selected.
                start_date_mode:
                    g.start_date_mode ?? (g.start_date ? 'Fixed' : 'At retirement'),
                start_date: snapMonthStartISO(g.start_date) ?? null,
                amount: g.amount ?? 0,
                frequency: g.frequency ?? 'Monthly',
                occurrences: g.occurrences ?? 1,
                end_mode: g.end_mode ?? 'Occurrences',
                end_date: snapMonthStartISO(g.end_date) ?? null,
                inflation_percent: g.inflation_percent ?? 6.0,
            })
        )
        : base.goals;

    const oneTime = Array.isArray(inputs.one_time_investments)
        ? inputs.one_time_investments.map((w) => ({
            name: w.name ?? '',
            date: snapMonthStartISO(w.date) ?? (currentDate || todayISO()),
            amount: w.amount ?? 0,
        }))
        : [];

    // Resolve risk_profile: use the stored value if it is a known profile;
    // fall back to 'Balanced' for missing or legacy inputs (D-P208-6).
    const rp = RISK_PROFILES.includes(inputs.risk_profile)
        ? inputs.risk_profile
        : 'Balanced';

    return {
        ...base,
        current_date: currentDate,
        current_age: inputs.current_age ?? base.current_age,
        target_lifetime: inputs.target_lifetime ?? base.target_lifetime,
        current_corpus: inputs.current_corpus ?? base.current_corpus,
        risk_profile: rp,
        investment_streams: streams,
        goals,
        one_time_investments: oneTime,
    };
}
