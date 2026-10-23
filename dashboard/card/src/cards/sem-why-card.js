/**
 * SEM Why? card (#1084) — why SEM changed a device's command.
 *
 * One row per recorded command change: time, a round device icon, the
 * device and its old and new command, the reason in plain words (#1174:
 * the sentence for the record's reason code, in the user's language; the
 * internal text is its tooltip and the fallback), and a small line of
 * what SEM decided on. Rows SEM only would
 * have sent (observer mode) carry a tag. Chips filter by device.
 *
 * Everyday shows the newest few; the rest sit in a fold. All settings
 * opens the fold. The rows come from the websocket command
 * solar_energy_management/decision_log (the whole ring buffer, newest
 * first), read once on load and again when the newest decision on the
 * state sensors moves.
 *
 * Config:
 *   type: custom:sem-why-card
 *   entity_prefix: sensor.sem_   # default
 */

import { SEMLitBase, html, css, nothing, semGlassCss } from '../base/sem-lit-base.js';
import { semDefineCard } from '../base/sem-shared.js';
import { getAllSettings, setAllSettings, VIEW_MODE_EVENT } from '../util/view-mode.js';
import {
    WHY_KINDS, changeLabel, chipKey, deviceName, earlierKey, filterRecords,
    inputChips, lastDecisionStamp, plainReason, rowTime, splitRows, whyChips,
} from '../util/why-rows.js';

export const WS_DECISION_LOG = 'solar_energy_management/decision_log';
/** After a failed read, wait this long before the next try. */
export const WHY_RETRY_MS = 30000;

class SEMWhyCard extends SEMLitBase {
    static get properties() {
        return {
            ...super.properties,
            _records: { state: true },
            _chip: { state: true },
            _allSettings: { state: true },
            _foldOpen: { state: true },
            _error: { state: true },
        };
    }

    constructor() {
        super();
        this._records = null;   // null = not loaded yet
        this._chip = '';        // '' = All
        this._allSettings = false;
        this._foldOpen = null;  // null = the view's default
        this._error = false;
        this._loading = false;
        this._stamp = null;        // the stamp the sensors show now
        this._loadedStamp = undefined; // the stamp the last good read was for
        this._failedAt = 0;
        this._boundViewMode = (e) => this._applyViewMode(
            e && e.detail && typeof e.detail.all === 'boolean' ? e.detail.all : getAllSettings());
    }

    setConfig(config) {
        super.setConfig(config || {});
        this._prefix = this._config.entity_prefix || 'sensor.sem_';
    }

    connectedCallback() {
        super.connectedCallback();
        this._applyViewMode(getAllSettings());
        window.addEventListener(VIEW_MODE_EVENT, this._boundViewMode);
    }

    disconnectedCallback() {
        super.disconnectedCallback();
        window.removeEventListener(VIEW_MODE_EVENT, this._boundViewMode);
    }

    set hass(hass) {
        super.hass = hass;
        // Read again only when the newest decision on the sensors moves.
        this._stamp = lastDecisionStamp(hass?.states, this._prefix || 'sensor.sem_');
        this._maybeLoad();
    }

    get hass() { return this._hass; }

    /**
     * Load when the wanted stamp is not the one last read. One read at a
     * time: a stamp that moves during a read is read right after it. A
     * failed read waits WHY_RETRY_MS, then the next hass update tries again.
     */
    _maybeLoad(now = Date.now()) {
        if (this._loading || !this._hass?.callWS) return;
        if (this._stamp === this._loadedStamp) return;
        if (this._failedAt && now - this._failedAt < WHY_RETRY_MS) return;
        this._load(this._stamp);
    }

    async _load(forStamp = this._stamp) {
        if (this._loading || !this._hass?.callWS) return;
        this._loading = true;
        let ok = false;
        try {
            const res = await this._hass.callWS({ type: WS_DECISION_LOG });
            this._records = Array.isArray(res?.records) ? res.records : [];
            this._error = false;
            this._loadedStamp = forStamp;
            this._failedAt = 0;
            ok = true;
        } catch (_err) {
            if (this._records === null) this._records = [];
            this._error = true;
            this._failedAt = Date.now();
        } finally {
            this._loading = false;
        }
        this.requestUpdate();
        // the stamp moved while this read was out: read again now
        if (ok && this._stamp !== this._loadedStamp) this._maybeLoad();
    }

    // ── The view: Everyday or All settings ──────────────────────────────
    _applyViewMode(all) {
        if (all === this._allSettings) return;
        this._allSettings = all;
        this._foldOpen = null;   // the fold back to the view's default
    }

    _setView(all) {
        setAllSettings(all);
        this._applyViewMode(all);
    }

    _isFoldOpen() {
        return typeof this._foldOpen === 'boolean' ? this._foldOpen : this._allSettings;
    }

    _toggleFold() {
        this._foldOpen = !this._isFoldOpen();
    }

    _setChip(key) {
        this._chip = key;
    }

    _fill(key, n) {
        return (this._t(key) || '').split('{n}').join(String(n));
    }

    // ── Rendering ───────────────────────────────────────────────────────
    _renderRow(rec, many) {
        const t = (k) => this._t(k);
        const kind = WHY_KINDS[rec.device_kind] || WHY_KINDS.load;
        const tz = this._hass?.config?.time_zone;
        const lang = this._hass?.language || 'en';
        const currency = this._hass?.config?.currency || '';
        const inputs = inputChips(rec.inputs, t, currency);
        // (#1174) the plain sentence for the code; the internal text is
        // its tooltip, and the row's text when the code has no sentence
        const plain = plainReason(rec, t);
        const why = plain || rec.text;
        return html`
            <div class="row" data-key=${chipKey(rec)}>
                <div class="time">${rowTime(rec.time, lang, new Date(), tz)}</div>
                <div class="ic" style="background:${kind.color}">
                    <ha-icon icon=${kind.icon} style="--mdc-icon-size:16px"></ha-icon>
                </div>
                <div class="body">
                    <div class="what">
                        <span class="name">${deviceName(rec, t, many[rec.device_kind] > 1)}</span>
                        <span class="change">${changeLabel(rec, t)}</span>
                        ${rec.observer ? html`<span class="tag">${t('why_observer')}</span>` : nothing}
                    </div>
                    ${why ? html`<div class="why" title=${plain ? rec.text || '' : ''}>${why}</div>` : nothing}
                    ${inputs.length ? html`
                        <div class="in">
                            ${inputs.map((c) => html`<span>${c.icon ? html`<ha-icon icon=${c.icon}
                                style="--mdc-icon-size:13px;color:${c.color}"></ha-icon>` : nothing}${c.text}</span>`)}
                        </div>` : nothing}
                </div>
            </div>`;
    }

    render() {
        if (!this._hass || !this._config) return nothing;
        const t = (k) => this._t(k);
        const all = this._records || [];
        const chips = whyChips(all, t);
        // a chip whose device left the log falls back to All
        const chip = chips.some((c) => c.key === this._chip) ? this._chip : '';
        const many = {};
        for (const c of chips) many[c.kind] = (many[c.kind] || 0) + 1;
        const rows = filterRecords(all, chip);
        const { shown, earlier } = splitRows(rows);
        const open = this._isFoldOpen();
        const tz = this._hass?.config?.time_zone;

        return html`
            <ha-card>
                <div class="wrap">
                    <div class="hd">
                        <span class="title">${t('why_title')}</span>
                        <div class="view-switch" role="group" aria-label=${t('ev_view_label')}>
                            <button class="view-seg ${this._allSettings ? '' : 'on'}" type="button"
                                    aria-pressed=${this._allSettings ? 'false' : 'true'}
                                    @click=${() => this._setView(false)}>${t('ev_view_everyday')}</button>
                            <button class="view-seg ${this._allSettings ? 'on' : ''}" type="button"
                                    aria-pressed=${this._allSettings ? 'true' : 'false'}
                                    @click=${() => this._setView(true)}>${t('ev_view_all')}</button>
                        </div>
                    </div>
                    ${chips.length ? html`
                        <div class="chips" role="group">
                            <button type="button" class="chip ${chip ? '' : 'on'}"
                                    aria-pressed=${chip ? 'false' : 'true'}
                                    @click=${() => this._setChip('')}>${t('why_all')}</button>
                            ${chips.map((c) => html`
                                <button type="button" class="chip ${chip === c.key ? 'on' : ''}"
                                        aria-pressed=${chip === c.key ? 'true' : 'false'}
                                        @click=${() => this._setChip(c.key)}>${c.name}</button>`)}
                        </div>` : nothing}
                    ${this._error ? html`<div class="note">${t('why_load_error')}</div>` : nothing}
                    ${this._records !== null && !rows.length && !this._error
                        ? html`<div class="note">${t('why_empty')}</div>` : nothing}
                    <div class="rows">
                        ${shown.map((r) => this._renderRow(r, many))}
                    </div>
                    ${earlier.length ? html`
                        <div class="fold ${open ? 'open' : ''}">
                            <button class="fold-btn" type="button"
                                    aria-expanded=${open ? 'true' : 'false'}
                                    @click=${(e) => { e.stopPropagation(); this._toggleFold(); }}>
                                <span class="fold-label">${this._fill(earlierKey(earlier, new Date(), tz), earlier.length)}</span>
                                <ha-icon icon=${open ? 'mdi:chevron-up' : 'mdi:chevron-down'}
                                         style="--mdc-icon-size:16px"></ha-icon>
                            </button>
                            ${open ? html`<div class="fold-body rows">
                                ${earlier.map((r) => this._renderRow(r, many))}
                            </div>` : nothing}
                        </div>` : nothing}
                </div>
            </ha-card>`;
    }

    static get styles() {
        return [semGlassCss, css`
            :host { display: block; }
            ha-card { display: block; }
            .wrap { padding: 14px 16px 6px; }
            .hd {
                display: flex; align-items: center; justify-content: space-between;
                gap: 8px; margin-bottom: 10px;
            }
            .title { font-size: 16px; font-weight: 600; }
            .view-switch { display: flex; flex-shrink: 0; }
            .view-seg {
                font: inherit; font-size: 11px; font-weight: 600;
                padding: 3px 10px; min-height: 24px;
                border: 1px solid var(--divider-color, rgba(255,255,255,0.12));
                background: transparent; cursor: pointer;
                color: var(--secondary-text-color, #999);
            }
            .view-seg:first-child { border-radius: 8px 0 0 8px; }
            .view-seg:last-child { border-radius: 0 8px 8px 0; border-left: none; }
            .view-seg.on { background: rgba(141,200,146,0.18); color: #8DC892; }
            .view-seg:focus-visible, .fold-btn:focus-visible, .chip:focus-visible {
                outline: 2px solid #8DC892; outline-offset: 1px;
            }
            .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
            .chip {
                font: inherit; font-size: 12px; padding: 4px 10px; min-height: 28px;
                border-radius: 14px; cursor: pointer; background: transparent;
                border: 1px solid var(--divider-color, rgba(255,255,255,0.12));
                color: var(--secondary-text-color, #999);
            }
            .chip.on { background: rgba(255,255,255,0.12); color: var(--primary-text-color); }
            .row {
                display: grid; grid-template-columns: 46px 28px 1fr; gap: 8px;
                padding: 9px 0; border-top: 1px solid rgba(255,255,255,0.06);
            }
            .time {
                font-size: 12px; padding-top: 4px; font-variant-numeric: tabular-nums;
                color: var(--secondary-text-color, #999);
            }
            .ic {
                width: 26px; height: 26px; border-radius: 50%;
                display: grid; place-items: center; color: #111;
            }
            .body { min-width: 0; }
            .what { line-height: 1.35; }
            .name { font-weight: 600; }
            .change { color: var(--secondary-text-color, #999); margin-left: 4px; }
            .tag {
                display: inline-block; font-size: 10.5px; font-weight: 600;
                border: 1px solid var(--divider-color, rgba(255,255,255,0.12));
                border-radius: 6px; padding: 0 5px; margin-left: 6px;
                color: var(--secondary-text-color, #999); vertical-align: 1px;
            }
            .why { font-size: 13px; line-height: 1.4; opacity: 0.9; }
            .in {
                display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 3px;
                font-size: 11.5px; color: var(--secondary-text-color, #999);
            }
            .in > span { display: inline-flex; align-items: center; gap: 3px; }
            .note {
                font-size: 13px; padding: 8px 0;
                color: var(--secondary-text-color, #999);
            }
            .fold-btn {
                display: flex; align-items: center; justify-content: space-between;
                width: 100%; min-height: 32px; padding: 4px 0;
                font: inherit; font-size: 11px; font-weight: 600;
                text-transform: uppercase; letter-spacing: 0.05em;
                color: var(--secondary-text-color, #999);
                background: none; border: none; cursor: pointer;
                border-top: 1px solid rgba(255,255,255,0.06);
            }
            .fold-btn:hover { color: var(--primary-text-color, #e0e0e0); }
        `];
    }
}

semDefineCard('sem-why-card', SEMWhyCard, {
    type: 'sem-why-card',
    name: 'SEM Why?',
    description: 'Why SEM changed a device: each command change, its reason and the numbers behind it',
    documentationURL:
        'https://github.com/traktore-org/sem-community/blob/develop/docs/DASHBOARD_GUIDE.md#sem-why-card',
});
