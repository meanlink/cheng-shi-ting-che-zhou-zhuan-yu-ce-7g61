import {api, state} from '../api.js';
import {badge, empty} from '../components.js';
import {esc, fmt} from '../utils.js';

const data = {receipts: [], claims: [], diversions: []};
const utcDay = () => new Date().toISOString().slice(0, 10);

const claimStatus = {
    held: ['占用中', 'watch'],
    released: ['已释放', 'critical'],
    settled: ['已结案', 'good'],
};

function select(name, options, placeholder) {
    return `<select name="${name}" required><option value="">${esc(placeholder)}</option>${options.map(([value, label]) =>
        `<option value="${esc(value)}">${esc(label)}</option>`).join('')}</select>`;
}

function note(name, placeholder) {
    return `<textarea name="${name}" required minlength="4" maxlength="500" rows="2" placeholder="${esc(placeholder)}"></textarea>`;
}

function formPanel(title, body, submit, extra = '') {
    return `<section class="panel operation-form"><h2>${title}</h2><form class="stack" data-handover-form="${extra}">
        ${body}<div class="form-status" role="status"></div><button class="button">${submit}</button></form></section>`;
}

function record(title, status, subtitle, details, message, controls) {
    const [label, tone] = status ? claimStatus[status] : [null, ''];
    return `<article class="panel operation-record"><div class="operation-record-head"><div><h3>${esc(title)}</h3>
        <p class="muted">${esc(subtitle)}</p></div>${label ? badge(label, tone) : ''}</div>
        <dl class="operation-metrics">${details.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>
        ${message ? `<div class="operation-message">${message}</div>` : ''}
        ${controls ? `<div class="operation-controls">${controls}</div>` : ''}</article>`;
}

function eligibleDiversions(receipt) {
    return data.diversions.filter(row => row.status === 'active' &&
        row.target_lot_id === receipt.lot_id && row.service_day === receipt.service_day);
}

function slipCard(row, analyst) {
    const options = eligibleDiversions(row);
    const claims = data.claims.filter(item => item.receipt_id === row.id);
    const claimRows = claims.length ? claims.map(item =>
        `<tr><td class="code">${esc(item.receipt_no)}</td><td>#${item.diversion_id} · ${esc(item.source_name)}</td>
        <td>${fmt(item.claimed_vehicles)}</td><td>${claimStatus[item.status][0]}</td>
        <td><small>${item.release_note ? esc(item.release_note) : '—'}</small></td></tr>`).join('') :
        `<tr><td colspan="5">${empty('还没有分流单认领车数。')}</td></tr>`;
    const claimForm = analyst ? (options.length ? formPanel('按分流单认领车数', `
        <label>分流单${select('diversion_id', options.map(item => [item.id,
            `#${item.id} · ${item.source_name} → ${item.target_name} · 预留 ${item.reserved_spaces} 位`]), '选择当日同接收场分流单')}</label>
        <label>认领回执号<input name="receipt_no" required minlength="3" maxlength="48" placeholder="如 RC-2607-01"></label>
        <label>认领车数<input name="claimed_vehicles" type="number" min="1" max="${row.remaining_vehicles}" step="1" required></label>`,
        '提交认领', `claim|${row.id}`) :
        `<div class="panel operation-form">${empty('该接收场当天没有已启用的分流单可认领。')}</div>`) : '';
    const correctForm = analyst ? formPanel('补正交班总数', `
        <label>新实收总数<input name="total_vehicles" type="number" min="${row.occupied_total}" step="1"
            value="${row.total_vehicles}" required></label>
        <p class="muted">新总数低于当前已认领占用 ${fmt(row.occupied_total)} 辆（含结案份额）时将被退回，需先由主管退回相关复盘释放份额。</p>`,
        '保存补正', `correct|${row.id}`) : '';
    return record(`${row.lot_name} · ${row.service_day}`, null,
        `${row.reference} · 交班单号唯一`,
        [['交班实收总数（辆）', fmt(row.total_vehicles)], ['累计已认领（辆）', fmt(row.claimed_total)],
            ['占用中 / 结案（辆）', `${fmt(row.held_total)} / ${fmt(row.occupied_total - row.held_total)}`],
            ['剩余可领（辆）', fmt(row.remaining_vehicles)]],
        `清点说明 · ${esc(row.reason)}<div class="table-wrap" style="margin-top:.7rem"><table><thead><tr>
        <th>回执号</th><th>分流单</th><th>车数</th><th>状态</th><th>释放依据</th></tr></thead><tbody>${claimRows}</tbody></table></div>`,
        '') + claimForm + correctForm;
}

function slipsPage(analyst) {
    const today = utcDay();
    const lots = state.lots.filter(lot => lot.status === 'active');
    const register = analyst ? formPanel('登记接收场交班单', `
        <label>接收停车场${select('lot_code', lots.map(lot => [lot.code, `${lot.name} · ${lot.capacity} 位`]), '选择接收场')}</label>
        <label>运营日（UTC）<input name="service_day" type="date" max="${today}" value="${today}" required></label>
        <label>交班单号<input name="reference" required minlength="4" maxlength="80" placeholder="如 HD-PARK12-2607"></label>
        <label>实收车总数<input name="total_vehicles" type="number" min="0" step="1" required></label>
        <label>清点说明${note('reason', '记录当班门岗、交班批次和清点口径')}</label>`,
        '登记交班单', 'register') : '';
    const records = data.receipts.map(row => slipCard(row, analyst)).join('') ||
        empty('还没有交班单。分析师登记接收场当天的交班单号与实收总数后，分流单即可按单认领。');
    return `<section class="page operations"><div class="page-head"><div>
        <h1>交班认领</h1><p class="muted">同一接收场、同一 UTC 运营日只有一份交班清点，多张分流单按单认领车数；并发提交不会超领，回执重试不重复扣减。</p></div></div>
        <div class="operation-layout has-form"><section class="operation-register"><h2>交班单与认领台账
        <small>${data.receipts.length} 张交班单 · ${data.claims.length} 笔认领</small></h2>${records}</section>${register}</div></section>`;
}

export async function showHandover(shell, isCurrent) {
    shell('<section class="page" role="status">正在读取交班认领台账…</section>');
    try {
        const [receipts, claims, diversions] = await Promise.all([
            api('/handover/receipts'), api('/handover/claims'), api('/operations/diversions')]);
        if (!isCurrent()) return;
        data.receipts = receipts;
        data.claims = claims;
        data.diversions = diversions;
        shell(slipsPage(state.user.role === 'analyst'));
        bindHandover(shell, isCurrent);
    } catch (error) {
        if (!isCurrent()) return;
        shell(`<section class="page"><p class="form-status error" role="alert">${esc(error.message)}</p>
            <button class="icon-button" data-handover-retry>重试</button></section>`);
        document.querySelector('[data-handover-retry]').onclick = () => showHandover(shell, isCurrent);
    }
}

function bindHandover(shell, isCurrent) {
    document.querySelectorAll('[data-handover-form]').forEach(form => {
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const button = form.querySelector('button:not([type="button"])');
            const statusEl = form.querySelector('.form-status');
            const body = Object.fromEntries(new FormData(form));
            const [kind, id] = form.dataset.handoverForm.split('|');
            for (const key of ['total_vehicles', 'diversion_id', 'claimed_vehicles']) {
                if (key in body) body[key] = Number(body[key]);
            }
            let path;
            if (kind === 'register') {
                path = '/handover/receipts';
            } else if (kind === 'correct') {
                path = `/handover/receipts/${id}/correct`;
                body.total_vehicles = Number(body.total_vehicles);
            } else {
                body.receipt_id = Number(id);
                path = '/handover/claims';
            }
            button.disabled = true;
            statusEl.textContent = '';
            statusEl.classList.remove('error');
            try {
                await api(path, {method: 'POST', body});
                await showHandover(shell, isCurrent);
            } catch (error) {
                statusEl.textContent = error.message;
                statusEl.classList.add('error');
                button.disabled = false;
            }
        });
    });
}
