'use strict';

const byId = (id) => document.getElementById(id);
const descriptions = {
  normal: 'A clean run through the queue to a successful charge.',
  transient: 'The processor fails temporarily. The worker retries automatically with the same payment identity.',
  timeout_after_charge: 'The processor records a charge, then loses its response. A retry recovers the original charge.',
  declined: 'A permanent decline stops processing. FlowGuard does not retry a declined payment.',
  outage: 'The processor remains unavailable. Bounded retries move the payment to the recovery queue.'
};
const statusLabels = {pending: 'Queued', processing: 'Processing', retrying: 'Retrying', succeeded: 'Succeeded', dead_letter: 'Needs replay', declined: 'Declined'};
let lastRequest = null;
let currentPayments = [];
let selectedPayment = null;
let previousPayments = '';
let previousEvents = '';
let detailSignature = '';
let loading = false;
let sending = false;

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function paymentLabel(payment) {
  return `PAY-${String(payment.id).slice(0, 8).toUpperCase()}`;
}

function formatMoney(payment) {
  return new Intl.NumberFormat(undefined, {style: 'currency', currency: payment.currency}).format(payment.amount_minor / 100);
}

function newKey() {
  byId('idempotency-key').value = `demo-${crypto.randomUUID()}`;
}

function feedback(message, error = false) {
  byId('form-feedback').textContent = message;
  byId('form-feedback').classList.toggle('error', error);
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(8000)});
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('The service returned an unreadable response. Please try again.');
  }
  if (!response.ok) throw new Error(data.error || `Request failed with status ${response.status}.`);
  return data;
}

function renderPayments(payments) {
  currentPayments = payments;
  const signature = JSON.stringify(payments);
  if (signature === previousPayments) return;
  previousPayments = signature;
  const rows = document.createDocumentFragment();
  for (const payment of payments) {
    const row = element('tr');
    const identity = element('td');
    identity.append(element('span', 'payment-id', paymentLabel(payment)), element('span', 'payment-key', payment.idempotency_key));
    const amount = element('td', 'payment-amount', formatMoney(payment));
    const state = element('td');
    state.append(element('span', `status ${payment.status}`, statusLabels[payment.status] || payment.status));
    const attempts = element('td', 'attempts', String(payment.attempts));
    const action = element('td');
    const button = element('button', 'detail-button', '↗');
    button.type = 'button';
    button.setAttribute('aria-label', `View details for ${paymentLabel(payment)}`);
    button.setAttribute('aria-expanded', String(selectedPayment === payment.id));
    button.setAttribute('aria-controls', 'payment-detail');
    button.addEventListener('click', () => {
      selectedPayment = selectedPayment === payment.id ? null : payment.id;
      detailSignature = '';
      document.querySelectorAll('.detail-button').forEach((item) => item.setAttribute('aria-expanded', 'false'));
      button.setAttribute('aria-expanded', String(selectedPayment === payment.id));
      renderDetail();
    });
    action.append(button);
    row.append(identity, amount, state, attempts, action);
    rows.append(row);
  }
  byId('payment-rows').replaceChildren(rows);
  byId('payments-empty').hidden = payments.length > 0;
  byId('record-count').textContent = `${payments.length} ${payments.length === 1 ? 'record' : 'records'}`;
  renderDetail();
}

function renderDetail() {
  const panel = byId('payment-detail');
  const payment = currentPayments.find((item) => item.id === selectedPayment);
  panel.hidden = !payment;
  if (!payment) return;
  const signature = JSON.stringify(payment);
  if (signature === detailSignature) return;
  detailSignature = signature;
  const heading = element('h3', '', `${paymentLabel(payment)} · ${statusLabels[payment.status] || payment.status}`);
  const definition = element('dl');
  const fields = [
    ['Identity', payment.idempotency_key],
    ['Scenario', byId('scenario').querySelector(`option[value="${payment.scenario}"]`)?.textContent || payment.scenario],
    ['Processor charge', payment.processor_charge_id || 'Not confirmed'],
    ['Created', new Date(payment.created_at * 1000).toLocaleString()]
  ];
  if (payment.last_error) fields.push(['Last result', payment.last_error]);
  if (payment.status === 'retrying' && payment.next_attempt_at) fields.push(['Next attempt', new Date(payment.next_attempt_at * 1000).toLocaleTimeString()]);
  for (const [name, value] of fields) definition.append(element('dt', '', name), element('dd', '', value));
  panel.replaceChildren(heading, definition);
  if (payment.status === 'dead_letter') {
    const hint = element('p', '', payment.scenario === 'outage' ? 'This scenario injects a permanent outage. Replay preserves its identity and repeats the fault.' : 'Replay keeps this payment identity. Restore the processor before replaying.');
    const replay = element('button', 'button button-light', 'Replay payment');
    replay.type = 'button';
    replay.addEventListener('click', async () => {
      replay.disabled = true;
      try {
        await api(`/api/payments/${encodeURIComponent(payment.id)}/replay`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        feedback(`${paymentLabel(payment)} is back in the queue with its original identity.`);
        await refresh();
      } catch (error) {
        feedback(error.message, true);
      } finally {
        replay.disabled = false;
      }
    });
    panel.append(hint, replay);
  }
}

function renderEvents(events) {
  const signature = JSON.stringify(events);
  if (signature === previousEvents) return;
  previousEvents = signature;
  const fragment = document.createDocumentFragment();
  for (const event of events.slice(0, 30)) {
    const item = element('li', 'event');
    const dot = element('span', 'event-dot');
    dot.setAttribute('aria-hidden', 'true');
    const body = element('div', 'event-message', event.message);
    body.append(element('span', 'event-payment', `${String(event.payment_id || 'system').slice(0, 12)} · ${event.kind}`));
    const time = element('time', '', new Date(event.created_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}));
    time.dateTime = new Date(event.created_at * 1000).toISOString();
    item.append(dot, body, time);
    fragment.append(item);
  }
  byId('events').replaceChildren(fragment);
  byId('activity-empty').hidden = events.length > 0;
}

function renderOverview(data) {
  const metrics = data.metrics;
  byId('total').textContent = metrics.payments_total;
  byId('nav-count').textContent = metrics.payments_total;
  byId('succeeded').textContent = metrics.succeeded;
  byId('duplicates').textContent = metrics.duplicate_requests;
  byId('attention').textContent = metrics.dead_letter;
  byId('attention-hint').textContent = metrics.dead_letter ? `${metrics.dead_letter} ${metrics.dead_letter === 1 ? 'payment' : 'payments'} awaiting replay` : 'No payments awaiting replay';
  byId('queue-count').textContent = metrics.pending + metrics.retrying;
  byId('processing-count').textContent = metrics.processing;
  const processorAvailable = data.processor.available;
  const integrity = byId('integrity-title').closest('.integrity');
  integrity.classList.toggle('warning', !processorAvailable || metrics.duplicate_charges > 0);
  byId('integrity-title').textContent = processorAvailable ? 'Charge integrity' : 'Processor unavailable';
  byId('integrity-detail').textContent = processorAvailable ? `${metrics.processor_charges} processor ${metrics.processor_charges === 1 ? 'charge' : 'charges'} recorded. ${metrics.duplicate_requests} duplicate requests safely resolved.` : 'The queue preserves payments until the processor returns.';
  byId('integrity-value').textContent = processorAvailable ? `${metrics.duplicate_charges} duplicate charges` : 'Awaiting evidence';
  renderPayments(data.payments);
  renderEvents(data.events);
}

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const data = await api('/api/overview');
    renderOverview(data);
    byId('connection').replaceChildren(element('i'), document.createTextNode('Live connection'));
    byId('connection').classList.remove('offline');
    byId('connection-error').hidden = true;
  } catch {
    byId('connection').replaceChildren(element('i'), document.createTextNode('Reconnecting'));
    byId('connection').classList.add('offline');
    byId('connection-error').textContent = 'The dashboard cannot reach FlowGuard. Displayed records may be out of date. Reconnecting automatically.';
    byId('connection-error').hidden = false;
  } finally {
    loading = false;
  }
}

async function sendPayment(payload, repeated = false) {
  if (sending) return;
  sending = true;
  byId('send-payment').disabled = true;
  byId('repeat-payment').disabled = true;
  feedback(repeated ? 'Repeating the original request…' : 'Sending payment to the queue…');
  try {
    const result = await api('/api/payments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    lastRequest = Object.freeze({...payload});
    feedback(result.replayed ? 'Duplicate prevented. This request resolved to the original payment.' : `${paymentLabel(result.payment)} accepted. Follow its progress in Payments.`);
    await refresh();
  } catch (error) {
    feedback(error.message, true);
  } finally {
    sending = false;
    byId('send-payment').disabled = false;
    byId('repeat-payment').disabled = !lastRequest;
  }
}

byId('payment-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const raw = byId('amount').value.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(raw)) {
    feedback('Enter an amount with up to two decimal places.', true);
    byId('amount').focus();
    return;
  }
  const [whole, fraction = ''] = raw.split('.');
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  if (!Number.isSafeInteger(amount) || amount <= 0 || amount > 100000000) {
    feedback('Enter an amount between 0.01 and 1,000,000.00.', true);
    byId('amount').focus();
    return;
  }
  sendPayment({idempotency_key: byId('idempotency-key').value.trim(), amount_minor: amount, currency: byId('currency').value, scenario: byId('scenario').value});
});
byId('repeat-payment').addEventListener('click', () => { if (lastRequest) sendPayment(lastRequest, true); });
byId('new-key').addEventListener('click', () => { newKey(); feedback('New identity generated. Your next request will create a new payment.'); });
byId('scenario').addEventListener('change', () => { byId('scenario-description').textContent = descriptions[byId('scenario').value]; });
byId('try-lost-response').addEventListener('click', () => {
  byId('scenario').value = 'timeout_after_charge';
  byId('scenario-description').textContent = descriptions.timeout_after_charge;
  newKey();
  byId('create-payment').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
  byId('scenario').focus({preventScroll: true});
  feedback('Lost response selected. Send this payment, then repeat the request to inspect charge protection.');
});
document.querySelectorAll('.nav-item[href^="#"]').forEach((item) => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((link) => {
      link.classList.remove('active');
      link.removeAttribute('aria-current');
    });
    item.classList.add('active');
    item.setAttribute('aria-current', 'page');
    byId('current-view').textContent = item.getAttribute('href') === '#payments' ? 'Payments' : item.getAttribute('href') === '#activity' ? 'Event stream' : 'Overview';
  });
});
newKey();
refresh();
setInterval(refresh, 1500);
