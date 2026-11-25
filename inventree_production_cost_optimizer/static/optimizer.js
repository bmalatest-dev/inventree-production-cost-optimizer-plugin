// Minimal, dependency-free frontend for the InvenTree UserInterfaceMixin panel.
function node(tag, text, parent, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  if (parent) parent.appendChild(element);
  return element;
}

function money(value) {
  return value == null ? '—' : Number(value).toLocaleString('en-CA', {
    style: 'currency', currency: 'CAD', maximumFractionDigits: 2
  });
}

function table(parent, headers, rows) {
  const wrap = node('div', '', parent);
  wrap.style.overflowX = 'auto';
  const tbl = node('table', '', wrap);
  Object.assign(tbl.style, { width: '100%', borderCollapse: 'collapse', marginTop: '8px' });
  const heading = node('tr', '', tbl);
  headers.forEach(header => {
    const cell = node('th', header, heading);
    Object.assign(cell.style, { textAlign: 'left', padding: '8px', borderBottom: '1px solid #aaa' });
  });
  rows.forEach(row => {
    const tr = node('tr', '', tbl);
    row.forEach(value => {
      const cell = node('td', value, tr);
      Object.assign(cell.style, { padding: '7px 8px', borderBottom: '1px solid #ddd' });
    });
  });
  return tbl;
}

function escapeCsv(value) {
  const str = value == null ? '' : String(value);
  return /[",\n]/.test(str) ? `"${str.replaceAll('"', '""')}"` : str;
}

function exportCsv(result) {
  const lines = [['Build Qty', 'Complete Estimate', 'Known Purchasing CAD', 'Known CAD per Unit', 'Marginal CAD', 'Warnings']];
  result.scenarios.forEach(s => lines.push([s.quantity, s.complete, s.known_spend_cad,
    s.known_spend_per_unit_cad, s.incremental_next_unit_cad, s.warnings.join('; ')]));
  lines.push([]);
  lines.push(['Build Qty', 'Part ID', 'IPN', 'Part', 'Shortage', 'CAD Unit Cost', 'CAD Line Cost', 'Source']);
  result.scenarios.forEach(s => s.purchase_lines.forEach(p => lines.push([
    s.quantity, p.part_id, p.part.ipn || '', p.part.name || '', p.shortage,
    p.unit_cost_cad, p.purchase_cost_cad, p.source
  ])));
  const blob = new Blob([lines.map(row => row.map(escapeCsv).join(',')).join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = node('a', '', document.body);
  link.href = url;
  link.download = 'production-cost-optimizer.csv';
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function renderOptimizer(target, data) {
  target.replaceChildren();
  const api = data?.api;
  const part = Number(data?.context?.part_id ?? data?.id);
  if (!api || !part) {
    node('p', 'Part or API context not available', target);
    return;
  }
  const box = node('div', '', target);
  Object.assign(box.style, { display: 'flex', flexDirection: 'column', gap: '10px', maxWidth: '1100px' });
  node('h3', `Production planning: ${data?.context?.part_name || part}`, box);
  node('p', 'New units to build; finished-goods stock does not reduce the target. CAD estimates only. No inventory or order records are modified.', box);
  const controls = node('div', '', box);
  Object.assign(controls.style, { display: 'flex', gap: '12px', flexWrap: 'wrap', alignItems: 'end' });
  function input(label, defaultValue) {
    const group = node('label', '', controls);
    Object.assign(group.style, { display: 'flex', flexDirection: 'column', gap: '4px' });
    node('span', label, group);
    const field = node('input', '', group);
    field.type = 'number'; field.min = '1'; field.max = '200'; field.value = defaultValue;
    field.style.width = '110px';
    return field;
  }
  const min = input('Minimum quantity', '10');
  const max = input('Maximum quantity', '40');
  const run = node('button', 'Calculate', controls);
  run.type = 'button';
  const csv = node('button', 'Download CSV', controls);
  csv.type = 'button'; csv.disabled = true;
  const print = node('button', 'Print / Save PDF', controls);
  print.type = 'button'; print.disabled = true;
  const message = node('p', '', box);
  const output = node('div', '', box);
  let current = null;
  csv.addEventListener('click', () => current && exportCsv(current));
  print.addEventListener('click', () => window.print());

  run.addEventListener('click', async () => {
    const a = Number(min.value), b = Number(max.value);
    if (!Number.isInteger(a) || !Number.isInteger(b) || a < 1 || b > 200 || a > b) {
      message.textContent = 'Enter an integer range between 1 and 200.';
      return;
    }
    run.disabled = true; csv.disabled = true; print.disabled = true;
    message.textContent = 'Calculating recursive BOM scenarios…';
    output.replaceChildren();
    try {
      const response = await api.post('/api/action/', {
        action: 'production_cost_optimizer', data: { part, minimum: a, maximum: b }
      });
      const result = response.data?.result || response.data;
      if (!Array.isArray(result?.scenarios)) throw new Error('Unexpected response structure');
      current = result;
      const scenarios = result.scenarios;
      const complete = scenarios.filter(s => s.complete);
      const best = complete.length ? complete.reduce((x, y) =>
        y.known_spend_per_unit_cad < x.known_spend_per_unit_cad ? y : x) : null;
      message.textContent = best
        ? `Lowest fully priced additional spend per unit: ${best.quantity} units (${money(best.known_spend_per_unit_cad)}/unit).`
        : 'No fully priced scenario: figures are known-cost lower bounds, not complete quotations.';
      table(output, ['Build qty', 'Estimate complete?', 'Known new spend', 'Known spend / unit', 'Next-unit increase'],
        scenarios.map(s => [s.quantity, s.complete ? 'Yes' : 'No', money(s.known_spend_cad),
          money(s.known_spend_per_unit_cad), money(s.incremental_next_unit_cad)]));
      node('h4', 'Purchasing breakdown', output);
      const selector = node('select', '', output);
      scenarios.forEach(s => {
        const option = node('option', `${s.quantity} units`, selector);
        option.value = String(s.quantity);
      });
      const details = node('div', '', output);
      function show() {
        details.replaceChildren();
        const s = scenarios.find(x => x.quantity === Number(selector.value));
        table(details, ['Part', 'Shortage', 'Unit CAD', 'New spend CAD', 'Price source'],
          s.purchase_lines.map(p => [p.part.ipn || p.part.name || p.part_id, p.shortage,
            money(p.unit_cost_cad), money(p.purchase_cost_cad), p.source]));
        const warnings = [...s.warnings];
        if (s.missing_price_part_ids.length) warnings.push(`Missing CAD prices for part IDs: ${s.missing_price_part_ids.join(', ')}`);
        warnings.forEach(w => node('p', `⚠ ${w}`, details));
      }
      selector.addEventListener('change', show);
      selector.value = String(best?.quantity || scenarios[0].quantity);
      show();
      node('h4', 'Current limitations', output);
      result.limitations.forEach(item => node('p', `• ${item}`, output));
      csv.disabled = false; print.disabled = false;
    } catch (error) {
      current = null;
      message.textContent = `Analysis failed: ${error?.response?.data?.detail || error?.message || 'Unknown error'}`;
    } finally {
      run.disabled = false;
    }
  });
}
