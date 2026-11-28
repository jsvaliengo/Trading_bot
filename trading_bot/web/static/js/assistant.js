/*
 * Assistente da conta: painel lateral que conversa com POST /api/assistant (só leitura).
 * Carregado só com TRADING_BOT_ASSISTANT_ENABLED; o botão aparece só quando a conta exibida tem bot.
 * `fetch` já vem patchado pelo main.js (anexa ?account=N e trata 401).
 */
(function () {
    'use strict';
    const $ = (id) => document.getElementById(id);
    const btn = $('assistant-btn'), panel = $('assistant'), msgs = $('assistant-msgs'), form = $('assistant-form'),
        input = $('assistant-input'), send = $('assistant-send'), err = $('assistant-error'), quota = $('assistant-quota');
    if (!btn || !panel) return;
    const sel = document.getElementById('acct-sel');
    const hasBot = (sel ? sel.selectedOptions[0].dataset.hasBot : document.body.dataset.hasBot) === '1';
    if (!hasBot) return;
    btn.hidden = false;

    const CSRF = document.body.dataset.csrf || '';
    const KEY = 'tradingbot-assistant-conv:' + (sel ? sel.value : '1');
    let conversationId = null, quotaHit = false;
    try { conversationId = parseInt(sessionStorage.getItem(KEY), 10) || null; } catch (e) { /* storage bloqueado */ }

    function bubble(role, text, tools) {
        const el = document.createElement('div');
        el.className = 'assistant-msg ' + role;
        el.textContent = text;
        msgs.appendChild(el);
        if (tools && tools.length) {
            const t = document.createElement('div');
            t.className = 'assistant-tools';
            t.textContent = 'leu: ' + tools.join(', ');
            msgs.appendChild(t);
        }
        msgs.scrollTop = msgs.scrollHeight;
        return el;
    }
    function fail(text) { err.textContent = text; err.hidden = false; }

    async function loadHistory() {
        if (!conversationId) return;
        const r = await fetch('/api/assistant/history?conversation_id=' + conversationId, { credentials: 'include' });
        if (!r.ok) { conversationId = null; return; }
        const d = await r.json();
        msgs.innerHTML = '';
        (d.messages || []).forEach(m => bubble(m.role, m.content, m.tool_calls));
    }

    btn.addEventListener('click', () => { panel.hidden = !panel.hidden; if (!panel.hidden) { loadHistory(); input.focus(); } });
    $('assistant-close').addEventListener('click', () => { panel.hidden = true; });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const message = input.value.trim();
        if (!message || send.disabled) return;
        err.hidden = true;
        bubble('user', message);
        const pending = bubble('assistant pending', 'pensando…');
        input.value = ''; send.disabled = true; input.disabled = true;
        try {
            const r = await fetch('/api/assistant', { method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
                body: JSON.stringify({ message, conversation_id: conversationId }) });
            const d = await r.json().catch(() => ({}));
            pending.remove();
            if (!r.ok) {
                fail(d.error || ({ 429: 'limite diário de mensagens atingido', 409: 'aguarde a resposta anterior', 502: 'assistente indisponível agora' }[r.status] || ('HTTP ' + r.status)));
                if (r.status === 429) quotaHit = true;
                return;
            }
            conversationId = d.conversation_id;
            try { sessionStorage.setItem(KEY, String(conversationId)); } catch (e2) { /* storage bloqueado */ }
            bubble('assistant', d.reply, d.tool_calls);
            if (typeof d.remaining_today === 'number') quota.textContent = d.remaining_today + ' mensagens hoje';
        } catch (e3) {
            pending.remove();
            fail('sem resposta do servidor');
        } finally {
            send.disabled = quotaHit;
            input.disabled = false; input.focus();
        }
    });
})();
