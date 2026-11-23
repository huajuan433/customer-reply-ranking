// 验证页面在三种环境下的行为：真后端 / 静态预览+服务在跑(该自动跳转) / 啥都没有(该禁用按钮)
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('output/客户回复排行榜.html', 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function makeEl(id) {
  return {
    id, _text: '', _html: '', style: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){},
    set textContent(v){ this._text = v; }, get textContent(){ return this._text; },
    set innerHTML(v){ this._html = v; },  get innerHTML(){ return this._html; }
  };
}

function runCase(name, fetchImpl) {
  const els = {};
  const ctx = {
    document: { getElementById: id => els[id] || (els[id] = makeEl(id)) },
    window: {}, console,
    setTimeout: fn => fn(),          // 立即执行，便于断言跳转
    encodeURIComponent, Date, Math,
    fetch: fetchImpl,
    location: { _href: '' , set href(v){ this._href = v; }, get href(){ return this._href; } }
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return new Promise(resolve => {
    setImmediate(() => {
      const warn = els['envwarn'] || {};
      const listHtml = (els['list'] || {})._html || '';
      const rows = listHtml.match(/class="row"/g) || [];
      const total = (vm.runInContext('DATA', ctx) || []).length;
      resolve({
        name,
        warnShown: warn.style && warn.style.display === 'block',
        warnText: warn._html || '',
        buttonsDisabled: listHtml.includes('disabled'),
        goto: ctx.location._href || '',
        rows: rows.length,
        total: total
      });
    });
  });
}

const cases = [
  runCase('真后端在跑', async () => ({ ok: true, json: async () => ({ ok: true }) })),

  runCase('静态预览 + 正式服务在', async (url) => {
    if (url === '/ping') return { ok: true, json: async () => { throw new Error('不是JSON'); } };
    if (String(url).includes('8765/ping')) return { ok: true, json: async () => ({ ok: true }) };
    return { ok: false, json: async () => ({}) };
  }),

  runCase('静态预览 + 服务没开', async () => ({ ok: false, json: async () => ({}) }))
];

Promise.all(cases).then(results => {
  let pass = 0, fail = 0;
  const check = (cond, msg) => { if (cond) { pass++; console.log('PASS', msg); } else { fail++; console.log('FAIL', msg); } };

  const [a, b, c] = results;
  console.log('--- 场景1：真后端 ---');
  check(a.rows > 0 && a.rows === a.total, `渲染 ${a.rows} 行客户（数据共 ${a.total}）`);
  check(!a.warnShown, '不显示红条');
  check(!a.buttonsDisabled, '按钮可用');

  console.log('--- 场景2：静态预览 + 正式服务在跑 ---');
  check(b.warnShown, '显示提示');
  check(/正在跳转/.test(b.warnText), '提示"正在跳转"');
  check(b.goto.includes('127.0.0.1:8765'), `自动跳转到 ${b.goto || '(空)'}`);

  console.log('--- 场景3：静态预览 + 服务没开 ---');
  check(c.warnShown, '显示红条');
  check(c.buttonsDisabled, '按钮被禁用');
  check(!c.goto, '不跳转');

  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
});
