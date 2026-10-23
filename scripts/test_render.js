// 用 jsdom 渲染 index.html：fetch 指向真实 data/ 目录下的 JSON 文件
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const htmlPath = process.argv[2] || path.join(ROOT, 'web', 'index.html');
const DATA_DIR = process.argv[3] || path.join(ROOT, 'data');

const html = fs.readFileSync(htmlPath, 'utf8');

const dom = new JSDOM(html, {
  runScripts: 'outside-only',
  url: 'https://example.com/radar/',
  pretendToBeVisual: true,
});
const w = dom.window;
const errors = [];
w.addEventListener('error', (e) => errors.push('window error: ' + e.message));

w.fetch = (p) => {
  const name = String(p).replace(/^.*\//, '');
  const file = path.join(DATA_DIR, name);
  if (!fs.existsSync(file)) {
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error('404 ' + name)) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(fs.readFileSync(file, 'utf8'))) });
};

const origErr = console.error;
w.console.error = (...a) => { errors.push('console.error: ' + a.join(' ')); origErr.apply(console, a); };

[...w.document.querySelectorAll('script')].forEach((s) => {
  try { w.eval(s.textContent); } catch (e) { errors.push('eval error: ' + e.message); }
});

const snap = (d) => ({
  items: d.querySelectorAll('.item').length,
  cols: d.querySelectorAll('.col').length,
  chips: [...d.querySelectorAll('.date-chip')].map((c) => c.textContent),
  sources: [...d.querySelectorAll('.col-name')].map((b) => b.textContent),
  total: d.getElementById('statTotal').textContent,
  srcCount: d.getElementById('statSrc').textContent,
  catBlocks: [...d.querySelectorAll('.cat-block .cat-name')].map((e) => e.textContent),
  catTabs: [...d.querySelectorAll('.cat-tab')].map((e) => e.textContent),
  catSummaries: d.querySelectorAll('.cat-summary').length,
  digestCards: [...d.querySelectorAll('.digest-card')].map((e) => ({
    cat: e.querySelector('.d-cat .nm') ? e.querySelector('.d-cat .nm').textContent : null,
    cnt: e.querySelector('.d-cat .cnt') ? e.querySelector('.d-cat .cnt').textContent : null,
    top: e.querySelector('.d-top') ? e.querySelector('.d-top').textContent.slice(0, 70) : null,
    kws: [...e.querySelectorAll('.kw')].map((k) => k.textContent),
  })),
  digestHead: d.querySelector('.digest-head .lab') ? d.querySelector('.digest-head .lab').textContent : null,
  notes: d.querySelectorAll('.it-note').length,
  scenes: d.querySelectorAll('.it-scene').length,
  sampleNote: d.querySelector('.it-note') ? d.querySelector('.it-note').textContent.slice(0, 80) : null,
  sampleScene: d.querySelector('.it-scene') ? d.querySelector('.it-scene').textContent.slice(0, 60) : null,
  colSummaries: d.querySelectorAll('.col-summary').length,
  rangeNote: d.getElementById('rangeNote').textContent,
  prevDisabled: d.getElementById('prevDay').disabled,
  nextDisabled: d.getElementById('nextDay').disabled,
  firstTitle: d.querySelector('.it-title') ? d.querySelector('.it-title').textContent : null,
  firstLink: d.querySelector('.item') ? d.querySelector('.item').getAttribute('href') : null,
  boards: [...d.querySelectorAll('.badge')].map((b) => b.textContent),
});

setTimeout(() => {
  const d = w.document;
  const first = snap(d);

  // 模拟用日期选择器跳到没有数据的一天
  const pick = d.getElementById('datePick');
  pick.value = '2026-10-01';
  pick.dispatchEvent(new w.Event('change'));

  setTimeout(() => {
    const emptyState = d.querySelector('.state') ? d.querySelector('.state').textContent : null;

    // 再跳回有数据的一天
    pick.value = '2026-10-21';
    pick.dispatchEvent(new w.Event('change'));

    setTimeout(() => {
      console.log(JSON.stringify({
        first,
        emptyState,
        back: {
          items: d.querySelectorAll('.item').length,
          catSummaries: d.querySelectorAll('.cat-summary').length,
          colSummaries: d.querySelectorAll('.col-summary').length,
          activeChip: d.querySelector('.date-chip.on') ? d.querySelector('.date-chip.on').textContent : null,
        },
        errors,
      }, null, 1));
      process.exit(errors.length ? 1 : 0);
    }, 300);
  }, 300);
}, 400);
