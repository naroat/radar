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
  items: d.querySelectorAll('.row-item').length,
  blocks: d.querySelectorAll('.src-block').length,
  chips: [...d.querySelectorAll('.date-chip')].map((c) => c.textContent),
  sources: [...d.querySelectorAll('.src-name')].map((b) => b.textContent),
  total: d.getElementById('statTotal').textContent,
  totalLab: d.getElementById('statTotalLab').textContent,
  srcCount: d.getElementById('statSrc').textContent,
  catTabs: [...d.querySelectorAll('.cat-tab')].map((e) => e.textContent),
  activeCat: d.querySelector('.cat-tab.on') ? d.querySelector('.cat-tab.on').textContent : null,
  activeView: d.querySelector('.view-btn.on') ? d.querySelector('.view-btn.on').textContent : null,
  digestHead: d.querySelector('.digest-head .lab') ? d.querySelector('.digest-head .lab').textContent : null,
  digestBody: d.querySelector('.digest-body') ? d.querySelector('.digest-body').textContent.slice(0, 80) : null,
  notes: d.querySelectorAll('.ri-note').length,
  scenes: d.querySelectorAll('.ri-scene').length,
  srcSummaries: d.querySelectorAll('.src-summary').length,
  viewHint: d.getElementById('viewHint').textContent,
  prevDisabled: d.getElementById('prevDay').disabled,
  nextDisabled: d.getElementById('nextDay').disabled,
  firstTitle: d.querySelector('.ri-title') ? d.querySelector('.ri-title').textContent : null,
  firstLink: d.querySelector('.row-item') ? d.querySelector('.row-item').getAttribute('href') : null,
  boards: [...d.querySelectorAll('.board-tag')].map((b) => b.textContent),
  moreBtns: d.querySelectorAll('.more-btn').length,
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await wait(500);
  const d = w.document;
  const day = snap(d);

  // 切到周视图
  const weekBtn = [...d.querySelectorAll('.view-btn')].find((b) => b.getAttribute('data-view') === 'week');
  weekBtn.click();
  await wait(400);
  const week = snap(d);

  // 切到月视图
  const monthBtn = [...d.querySelectorAll('.view-btn')].find((b) => b.getAttribute('data-view') === 'month');
  monthBtn.click();
  await wait(500);
  const month = snap(d);

  // 点开第一个「展开其余」按钮
  const moreBtn = d.querySelector('.more-btn');
  if (moreBtn) moreBtn.click();
  await wait(300);
  const expanded = { items: d.querySelectorAll('.row-item').length,
                     moreBtns: d.querySelectorAll('.more-btn').length };

  // 切到第二个分类
  const tabs = [...d.querySelectorAll('.cat-tab')];
  if (tabs[1]) tabs[1].click();
  await wait(300);
  const cat2 = snap(d);

  // 跳到没有数据的一天
  const pick = d.getElementById('datePick');
  pick.value = '2026-10-01';
  pick.dispatchEvent(new w.Event('change'));
  await wait(400);
  const emptyState = d.querySelector('.state') ? d.querySelector('.state').textContent : null;

  // 再跳回有数据的一天
  pick.value = '2026-10-21';
  pick.dispatchEvent(new w.Event('change'));
  await wait(400);
  const back = {
    items: d.querySelectorAll('.row-item').length,
    activeChip: d.querySelector('.date-chip.on') ? d.querySelector('.date-chip.on').textContent : null,
    total: d.getElementById('statTotal').textContent,
  };

  console.log(JSON.stringify({ day, week, month, expanded, cat2, emptyState, back, errors }, null, 1));
  process.exit(errors.length ? 1 : 0);
})();
