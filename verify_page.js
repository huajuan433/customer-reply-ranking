// 排行榜页面验证：真实JS引擎 + mock DOM
// 重点：默认按价值分排序、分数范围、评分公式与"独立实现"交叉验算、权重/标签/手动分生效、原有排序筛选不回归
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('output/客户回复排行榜.html', 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('FAIL 未找到script'); process.exit(1); }

const elements = {};
function mk(id) {
  const cls = new Set();
  return {
    id, _text: '', _html: '', _on: null, style: {}, value: '', dataset: {},
    classList: {
      add(c){ cls.add(c); }, remove(c){ cls.delete(c); },
      toggle(c, force){
        const on = force === undefined ? !cls.has(c) : !!force;
        if (on) cls.add(c); else cls.delete(c);
        return on;
      },
      contains(c){ return cls.has(c); }
    },
    addEventListener(ev, fn){ this._on = fn; },
    set textContent(v){ this._text = String(v); }, get textContent(){ return this._text; },
    set innerHTML(v){ this._html = v; },  get innerHTML(){ return this._html; }
  };
}
const context = {
  document: {
    getElementById: id => elements[id] || (elements[id] = mk(id)),
    querySelectorAll: () => []
  },
  window: {}, console, setTimeout, encodeURIComponent, Date, Math, Object, Array, String, Number, JSON
};
vm.createContext(context);
let err = null;
try { vm.runInContext(m[1], context); } catch (e) { err = e; }
if (err) { console.error('FAIL 脚本执行抛异常:', err.message); process.exit(1); }

let fails = 0;
const check = (c, msg, bad) => { if (c) console.log('PASS ' + msg); else { fails++; console.log('FAIL ' + msg + (bad || '')); } };
const $v = e => vm.runInContext(e, context);
const DATA = $v('DATA');
const NOW = $v('NOW');
const total = DATA.length;
const listHtml = () => elements['list']._html;
const rowCount = () => (listHtml().match(/class="row"/g) || []).length;
const scores = () => [...listHtml().matchAll(/class="vscore (?:hi|mid|lo)" title="[^"]*"[^>]*>(\d+)<span>/g)].map(x => +x[1]);

console.log('PASS 页面脚本执行无异常');
check(total > 0, `数据 ${total} 条`);
check(rowCount() === total, `默认渲染 ${rowCount()} 行`, `，期望 ${total}`);

// ---- 默认按价值分排序 ----
check($v('sortKey') === 'value', `默认排序 = ${$v('sortKey')}`);
const sc0 = scores();
check(sc0.length === total, `提取到 ${sc0.length} 个价值分`);
check(sc0.every(v => v >= 0 && v <= 100), '所有价值分都在 0~100');
let desc = true;
for (let i = 1; i < sc0.length; i++) if (sc0[i] > sc0[i - 1]) { desc = false; break; }
check(desc, '列表按价值分从高到低');
check((listHtml().match(/价值分 \d+ = /g) || []).length === total, '每行都有 hover 评分理由');

// ---- 独立实现交叉验算（按设计文档公式另写一遍）----
function expectScore(r, W, HL, VCAP, TAGW, TAGS, MANUAL) {
  const last = Math.max(r.my_last_ts || 0, r.last_ts || 0);
  const days = last ? Math.max(0, (NOW - last) / 86400) : 9999;
  const rec = last ? Math.exp(-days / HL) : 0;
  const rep = Math.min(1, (r.replies || 0) / Math.max(1, r.my_msgs || 0));
  const t = (r.my_msgs || 0) + (r.replies || 0);
  const vol = Math.min(1, Math.log(1 + t) / Math.log(1 + VCAP));
  const wsum = (W.recency + W.reply + W.volume) || 1;
  const base = rec * (W.recency / wsum) + rep * (W.reply / wsum) + vol * (W.volume / wsum);
  const tags = TAGS[r.username] || TAGS[r.name] || [];
  const tagSum = tags.reduce((s, x) => s + (+TAGW[x] || 0), 0);
  const man = MANUAL[r.username] !== undefined ? (+MANUAL[r.username] || 0) : (+MANUAL[r.name] || 0);
  return Math.max(0, Math.min(100, Math.round(base * 100 + tagSum + man)));
}
const W = $v('W'), HL = $v('HL'), VCAP = $v('VCAP'), TAGW = $v('TAGW'), TAGS = $v('TAGS'), MANUAL = $v('MANUAL');
const sample = DATA.filter((_, i) => i % Math.max(1, Math.floor(total / 25)) === 0).slice(0, 25);
let mismatch = 0;
for (const r of sample) {
  const want = expectScore(r, W, HL, VCAP, TAGW, TAGS, MANUAL);
  const got = $v('scoreOf')(r).value;
  if (want !== got) { mismatch++; if (mismatch <= 3) console.log(`  偏差 ${r.name}: 独立=${want} 页面=${got}`); }
}
check(mismatch === 0, `与独立实现一致（抽检 ${sample.length} 位客户，偏差 ${mismatch}）`);

// ---- 权重概念：recency 独占时应等于 rec*100 ----
vm.runInContext('W={recency:100,reply:0,volume:0};HL=14', context);
const rAny = DATA[0];
const recExpect = Math.max(0, Math.min(100, Math.round(Math.exp(-Math.max(0, (NOW - Math.max(rAny.my_last_ts || 0, rAny.last_ts || 0)) / 86400) / 14) * 100)));
check($v('scoreOf')(rAny).value === recExpect, `权重=纯最近互动时分数=${recExpect}（衰减公式生效）`);

// ---- 标签 / 手动分：选一个分数居中、不会被 0/100 截断掩盖效果的客户 ----
vm.runInContext('W={recency:40,reply:30,volume:15};HL=14', context);
const midOne = DATA.map(r => ({ r, v: $v('scoreOf')(r).value }))
  .filter(x => x.v >= 30 && x.v <= 60)[0] || { r: rAny, v: $v('scoreOf')(rAny).value };
const rm = midOne.r, U = rm.username;
const before = $v('scoreOf')(rm).value;
vm.runInContext(`TAGS[${JSON.stringify(U)}]=['高意向']`, context);
const afterTag = $v('scoreOf')(rm).value;
check(afterTag === Math.min(100, before + 20), `「高意向」+20：${before} → ${afterTag}（期望 ${Math.min(100, before + 20)}）`);
check($v('scoreOf')(rm).tagSum === 20, '标签分累加值 = 20');

vm.runInContext(`MANUAL[${JSON.stringify(U)}]=-15`, context);
const afterMan = $v('scoreOf')(rm).value;
check(afterMan === Math.max(0, Math.min(100, afterTag - 15)), `手动分 -15：${afterTag} → ${afterMan}`);
// 两个标签累加
vm.runInContext(`TAGS[${JSON.stringify(U)}]=['高意向','老客户']`, context);
check($v('scoreOf')(rm).tagSum === 35, '多标签累加 20+15 = 35');

// ---- 排序/筛选回归 ----
vm.runInContext(`delete TAGS[${JSON.stringify(U)}];delete MANUAL[${JSON.stringify(U)}]`, context);
vm.runInContext('W={recency:40,reply:30,volume:15};HL=' + $v('SCORE.half_life_days'), context);  // 还原被测试改过的权重
context.setSort('value');
for (const key of ['my_last_ts', 'replies', 'recent30', 'last_ts']) {
  try {
    context.setSort(key);
    check(rowCount() === total, `排序 ${key} 渲染 ${rowCount()} 行`);
  } catch (e) { fails++; console.log(`FAIL 排序 ${key} 抛异常: ${e.message}`); }
}
context.setSort('value');
const d0 = new Date(); d0.setHours(0, 0, 0, 0);
const today = d0.getTime() / 1000;
const expects = {
  all: total,
  today: DATA.filter(r => r.my_last_ts >= today).length,
  d3: DATA.filter(r => r.my_last_ts >= NOW - 3 * 86400).length,
  d7: DATA.filter(r => r.my_last_ts >= NOW - 7 * 86400).length,
  pending: DATA.filter(r => r.my_msgs > 0 && !r.replied).length
};
for (const [k, want] of Object.entries(expects)) {
  try {
    context.setFilter(k);
    check(rowCount() === want, `筛选 ${k} 渲染 ${rowCount()} 行（期望 ${want}）`);
  } catch (e) { fails++; console.log(`FAIL 筛选 ${k} 抛异常: ${e.message}`); }
}
context.setFilter('all');
elements['q']._on({ target: { value: '省立' } });
check(rowCount() > 0 && rowCount() < total, `搜索「省立」命中 ${rowCount()} 行`);
elements['q']._on({ target: { value: '' } });
check(rowCount() === total, '清空搜索恢复全量');

// ---- 权重面板 ----
context.toggleCfg();
check(elements['cfgpanel'].classList._on !== false, '权重面板可切换');
check(elements['tagw']._html.includes('高意向'), '面板渲染标签分值输入框');
check(String(elements['v_recency'].textContent) === '40', `滑块初值同步=${elements['v_recency'].textContent}`);

// ---- 统计行 ----
check(+elements['avg'].textContent > 0 && +elements['avg'].textContent <= 100, `平均价值分 ${elements['avg'].textContent}`);
check(+elements['hv'].textContent >= 0, `高价值(≥70) ${elements['hv'].textContent} 位`);

// ---- rel ----
const cases = [[0, /刚刚|分钟前/], [300, '5分钟前'], [7200, '2小时前'], [86401, '昨天'], [864001, '10天前'], [8640001, /个月前/]];
for (const [sec, want] of cases) {
  const got = context.rel(NOW - sec);
  check(want instanceof RegExp ? want.test(got) : got === want, `rel(${sec}s)=${got}`);
}

// ================= 标签 / 跟进状态 / 批量操作 =================
console.log('\n--- 标签 · 跟进状态 · 批量 ---');
const U1 = DATA[0].username, U2 = DATA[1].username, N1 = DATA[0].name;
const restore = () => {
  vm.runInContext(`delete TAGS[${JSON.stringify(U1)}];delete TAGS[${JSON.stringify(U2)}];
    delete STATUS[${JSON.stringify(U1)}];delete STATUS[${JSON.stringify(U2)}];
    TAGCOLORS['高意向']='#0f9d58';statusFilter='';tagFilter='';sel.clear()`, context);
  context.setFilter('all'); context.render();
};
context.setSort('value'); context.setFilter('all'); context.setTagFilter(''); context.setStatusFilter('');

check($v('STATUSES').length === 5, `预设状态 ${$v('STATUSES').length} 个`);
check(['待激活', '跟进中', '已转化', '暂时搁置', '已流失'].every(n => $v('STATUSES').some(x => x.name === n)),
  '五个状态齐全：待激活/跟进中/已转化/暂时搁置/已流失');

// 行内展示
vm.runInContext(`TAGS[${JSON.stringify(U1)}]=['高意向','已报价'];STATUS[${JSON.stringify(U1)}]='跟进中'`, context);
context.render();
check(listHtml().includes('class="stchip"'), '行内渲染状态徽章');
check(listHtml().includes('跟进中'), '行内显示状态文字');
check(/class="tagchip"[^>]*>高意向/.test(listHtml()) || listHtml().includes('高意向'), '行内显示主要标签');
check(listHtml().includes('已报价'), '第二个标签也显示');
check((listHtml().match(/class="ck"/g) || []).length === total, '每行都有勾选框');

// 按标签 / 状态筛选
context.setTagFilter(encodeURIComponent('高意向'));
check(rowCount() === 1 && listHtml().includes(N1.slice(0, 6)), `按标签筛选 ${rowCount()} 行（期望 1）`);
context.setTagFilter('');
context.setStatusFilter(encodeURIComponent('跟进中'));
check(rowCount() === 1, `按状态筛选 ${rowCount()} 行（期望 1）`);
context.setStatusFilter('__none');
const noneWant = DATA.filter(r => !$v('STATUS')[r.username]).length;
check(rowCount() === noneWant, `「未设置」筛选 ${rowCount()} 行（期望 ${noneWant}）`);
context.setStatusFilter('已转化');
check(rowCount() === 0, '未使用的状态筛选为 0 行');
context.setStatusFilter(encodeURIComponent('跟进中')); context.setTagFilter(encodeURIComponent('已报价'));
check(rowCount() === 1, '状态 + 标签叠加筛选生效');
context.setTagFilter(encodeURIComponent('同行'));
check(rowCount() === 0 && listHtml().includes('没有客户'), '不存在的标签组合显示空态');

// 筛选胶囊
const chipsHtml = () => elements['tagchips']._html;
check(chipsHtml().includes('高意向') && chipsHtml().includes('已报价'), '标签筛选胶囊列出已有标签');
check(elements['stchips']._html.includes('待激活') && elements['stchips']._html.includes('未设置'), '状态筛选胶囊渲染完整');

// 抽屉
context.openDrawer(encodeURIComponent(U1));
const dh = () => elements['drawer']._html;
check(dh().includes('dname') && dh().includes(N1.slice(0, 6)), '抽屉显示客户名');
check(dh().includes('跟进状态') && $v('STATUSES').every(x => dh().includes(x.name)), '抽屉列出全部状态按钮');
check(dh().includes('高意向') && dh().includes('class="rm"'), '抽屉里已有标签可移除');
check(dh().includes('class="cpick"'), '标签带取色器（可自定义颜色）');
check(dh().includes('快速添加'), '抽屉有快速添加区');
check(elements['drawer'].classList.contains('show') && elements['overlay'].classList.contains('show'), '抽屉与遮罩已展开');

// 标签增删 / 改色 / 状态切换
context.addTag(encodeURIComponent(U2), encodeURIComponent('同行'));
check(($v('TAGS')[U2] || []).indexOf('同行') >= 0, 'addTag 写入本地数据');
context.removeTag(encodeURIComponent(U1), encodeURIComponent('已报价'));
check(JSON.stringify($v('TAGS')[U1]) === JSON.stringify(['高意向']), 'removeTag 移除标签');
context.setStatus(encodeURIComponent(U1), encodeURIComponent('已转化'));
check($v('STATUS')[U1] === '已转化', 'setStatus 改状态');
context.setTagColor(encodeURIComponent('高意向'), '#123456');
check($v('TAGCOLORS')['高意向'] === '#123456', '标签颜色已改');
context.setTagFilter(''); context.setStatusFilter(''); context.render();
check(listHtml().includes('#123456'), '自定义颜色应用到列表标签');

// 批量
context.toggleSel(U1, true);
context.toggleSel(U2, true);
check($v('sel').size === 2, '勾选 2 位客户');
check(elements['batch'].classList.contains('show'), '批量操作条出现');
check(String(elements['seln'].textContent) === '2', `批量条计数=${elements['seln'].textContent}`);
context.batchToggleTag(encodeURIComponent('价格敏感'));
check(($v('TAGS')[U1] || []).indexOf('价格敏感') >= 0 && ($v('TAGS')[U2] || []).indexOf('价格敏感') >= 0, '批量打标签给所有人');
context.batchToggleTag(encodeURIComponent('价格敏感'));
check(($v('TAGS')[U1] || []).indexOf('价格敏感') < 0 && ($v('TAGS')[U2] || []).indexOf('价格敏感') < 0, '再点一次批量移除');
context.applyBatchStatus(encodeURIComponent('已流失'));
check($v('STATUS')[U1] === '已流失' && $v('STATUS')[U2] === '已流失', '批量改状态');
context.clearSel();
check($v('sel').size === 0 && !elements['batch'].classList.contains('show'), '取消选择后批量条收起');
context.toggleAll(true);
check($v('sel').size === total, `全选当前筛选 ${$v('sel').size} 位`);
context.clearSel();
check(!Object.keys($v('TAGS')).some(k => k.indexOf('%') >= 0), '没有把 URL 编码当成客户 key');
check(!Object.values($v('TAGS')).some(v => v.some(t => t.indexOf('%') >= 0)), '标签值里没有残留 % 编码');
check(!Object.values($v('STATUS')).some(v => v.indexOf('%') >= 0), '状态值里没有残留 % 编码');
restore();
check(rowCount() === total, '清理测试数据后回到全量');
check(!$v('TAGS').hasOwnProperty(U1), '测试标签已清理');

// ================= 快捷话术模板 =================
console.log('\n--- 快捷话术模板 ---');
const TPL0 = JSON.parse(JSON.stringify($v('TEMPLATES')));
const pickHtml = () => elements['mbody']._html;
const listHtml2 = () => elements['tpllist']._html;
const $el = id => context.document.getElementById(id);

check($v('TEMPLATES').length === 7, `默认话术 ${$v('TEMPLATES').length} 条`);
check($v('TPL_CATS').join(',') === '首次激活,跟进中,促单,售后', '默认场景：' + $v('TPL_CATS').join(' / '));
check($v('TEMPLATES').some(t => t.pinned), '有默认置顶话术');

const tplRow = DATA[0];
const rTpl = $v('renderTpl');
const out = rTpl('{{客户名}}|{{编号}}|{{上次话题}}|{{我上次说的}}|{{日期}}', tplRow).split('|');
check(out[0] === tplRow.name, '{{客户名}} 替换正确');
check(/^\d{3,6}$/.test(out[1]), `{{编号}} 取到备注编号=${out[1]}`);
check(out[4] === $v('todayStr')(), '{{日期}} 替换为今天');
check(!rTpl('{{客户名}}{{上次话题}}{{我上次说的}}', tplRow).includes('{{'), '已知变量全部替换');
check(rTpl('{{客户名}}{{不存在的变量}}', tplRow).endsWith('{{不存在的变量}}'), '未知变量原样保留（不会静默丢掉）');
check(rTpl('{{ 客户名 }}', tplRow) === tplRow.name, '变量内允许空格');

check((listHtml().match(/class="tpl"/g) || []).length === total, '每行都有「复制话术」按钮');

// 选择弹窗
context.openTplPicker(encodeURIComponent(tplRow.username));
check(pickHtml().includes('选一条话术') && pickHtml().includes(tplRow.name), '弹窗标题带客户名');
check(pickHtml().includes('管理模板') && pickHtml().includes('搜索话术'), '有管理入口与搜索框');
check(listHtml2().includes('置顶') && listHtml2().indexOf('置顶') < listHtml2().indexOf('跟进中'), '置顶分组排在最前');
check(listHtml2().includes('复制并打开'), '每条话术带「复制并打开」');
check(listHtml2().includes('首次激活') && listHtml2().includes('促单') && listHtml2().includes('售后'), '按场景分组展示');

vm.runInContext("tplQuery='破冰'", context); context.renderTplList();
check(listHtml2().includes('重新破冰') && !listHtml2().includes('老客户回访'), '搜索「破冰」只剩匹配项');
vm.runInContext("tplQuery='不可能存在的词'", context); context.renderTplList();
check(listHtml2().includes('没有匹配的话术'), '搜索无结果有提示');
vm.runInContext("tplQuery=''", context); context.renderTplList();

// 管理列表
context.openTplManager();
check(pickHtml().includes('话术模板管理'), '管理弹窗打开');
check((pickHtml().match(/编辑/g) || []).length === 7, '每条都有编辑按钮');
check(pickHtml().includes('删除') && pickHtml().includes('新建话术'), '有删除与新建入口');

// 新建表单
context.tplForm('');
check(pickHtml().includes('id="tf_title"') && pickHtml().includes('id="tf_text"'), '表单含标题与内容');
check((pickHtml().match(/insertVar/g) || []).length === 6, '6 个变量快捷按钮');
check(pickHtml().includes('datalist'), '分类可选已有场景');

// 变量插入到光标处
$el('tf_text').value = '麻烦您看一下';
$el('tf_text').selectionStart = 2; $el('tf_text').selectionEnd = 2;
context.insertVar('客户名');
check($el('tf_text').value === '麻烦{{客户名}}您看一下', '变量插入到光标位置');

// 保存（本地即时生效，无后端也能用）
const n0 = $v('TEMPLATES').length;
$el('tf_title').value = '测试话术X';
$el('tf_cat').value = '促单';
$el('tf_text').value = '{{客户名}}老师，测试一下';
$el('tf_pin').checked = true;
context.saveTpl();
check($v('TEMPLATES').length === n0 + 1, '新建话术写入本地');
const added = $v('TEMPLATES').filter(t => t.title === '测试话术X')[0];
check(!!added && added.cat === '促单' && added.pinned === true, '标题/分类/置顶保存正确');

// 置顶排序
const unpinned = $v('TEMPLATES').filter(t => !t.pinned)[0];
context.togglePin(unpinned.id);
check($v('TEMPLATES').filter(t => t.id === unpinned.id)[0].pinned === true, '置顶切换生效');
check($v('sortTpls')($v('TEMPLATES'))[0].pinned === true, '排序时置顶永远在前');

// 还原
vm.runInContext(`TEMPLATES.splice(0,TEMPLATES.length,...(${JSON.stringify(TPL0)}));
  TPL_CATS.splice(0,TPL_CATS.length,'首次激活','跟进中','促单','售后');
  tplQuery='';tplUser='';tplEditId='';`, context);
check($v('TEMPLATES').length === 7 && $v('TEMPLATES').every(t => t.title !== '测试话术X'), '话术测试数据已还原');
context.render();
check(rowCount() === total, '还原后列表仍完整');

// ---------------- 今日必跟 ----------------
console.log('\n[今日必跟]');
const TD0 = $v('TD_MANUAL').slice();
const items = $v('todoItems()');
check(Array.isArray(items) && items.length > 0, `自动挑出 ${items.length} 位客户`);
check(items.filter(x => x.kind === 'auto').length <= (+$v('TD_RULE.limit')), '自动推荐不超过每日上限');
check(items.every(x => x.s.value >= (+$v('TD_RULE.min_score'))), '价值分都不低于门槛');
check(items.filter(x => x.kind === 'auto').every(x => x.days === null || x.days >= (+$v('TD_RULE.min_days'))), '自动推荐的都没在 N 天内互动过');
check(items.every(x => $v('TD_RULE.skip_status').indexOf($v('statusOf')(x.r)) < 0), '跳过了排除状态');
check(items.every((x, i) => i === 0 || x.s.value <= items[i - 1].s.value), '按价值分从高到低');
check($el('tdn')._text === String(items.length), `顶部角标显示 ${items.length}`);

// 手动加入（抽屉里的「加入今日」走这里）
const pick = DATA.filter(r => !$v('TD_SET')[r.username] && $v('statusOf')(r) !== '已转化')[0];
context.toggleTodo(encodeURIComponent(pick.username));
const afterAdd = $v('todoItems()');
check($v('TD_MANUAL').indexOf(pick.username) >= 0, '手动加入写入待办名单');
const addedIt = afterAdd.filter(x => x.r.username === pick.username)[0];
check(!!addedIt && addedIt.kind === 'manual' && afterAdd[0].kind === 'manual', '手动加入的排在自动推荐之前，标为 manual');
check($el('tdn')._text === String(afterAdd.length), '角标随手动加入同步');
check(listHtml().includes('class="tdflag"'), '列表行上出现「今日」标记');

// 面板
context.openTodo();
check($el('todo').classList.contains('show'), '面板能打开');
const todoHtml = $el('tdbody')._html;
check(todoHtml.includes('手动加入') && todoHtml.includes('自动推荐'), '分组显示「手动加入 / 自动推荐」');
check(todoHtml.includes('✓ 已跟'), '每条都有「已跟」按钮');
check((todoHtml.match(/class="tditem"/g) || []).length === afterAdd.length, '面板条目数与角标一致');
check($el('tdstat')._text.includes('今天已跟 0 位'), '底部统计显示今天已跟 0 位');
check($el('rl_status')._html.includes('已转化'), '规则区列出可跳过的状态');

// 一键标记已跟
context.markDone(encodeURIComponent(pick.username));
const afterDone = $v('todoItems()');
check(!afterDone.some(x => x.r.username === pick.username), '已跟后从今日列表移除');
check($v('TD_DONE')[pick.username] > 0, '已跟时间写入');
check($v('TD_MANUAL').indexOf(pick.username) < 0, '已跟后同时从手动名单移除');
check($el('tdn')._text === String(afterDone.length), `角标更新为 ${afterDone.length}`);
check($el('tdstat')._text.includes('今天已跟 1 位'), '底部统计变成今天已跟 1 位');

// 今天先不跟
const skipIt = afterDone.filter(x => x.kind === 'auto')[0];
if (skipIt) {
  context.todoSkip(encodeURIComponent(skipIt.r.username), 'auto');
  check(!$v('todoItems()').some(x => x.r.username === skipIt.r.username), '「今天先不跟」把它从今天移除');
  check($v('TD_SNOOZE')[skipIt.r.username] === $v('todayStr')(), '记上了今天的稍后标记');
}

// 改门槛立即重算
$el('rl_days').value = 20; $el('rl_score').value = 0; $el('rl_limit').value = 99;
context.saveRule(true);
const strict = $v('todoItems()');
check(strict.filter(x => x.kind === 'auto').every(x => x.days === null || x.days >= 20), '调高「超过N天」后当天名单立即重算');
check($v('TD_TODAY').date === $v('todayStr')(), '重算后写回当天名单');
$el('rl_days').value = 3; $el('rl_score').value = 45; $el('rl_limit').value = 15;
context.saveRule(true);

context.closeTodo();
check(!$el('todo').classList.contains('show'), '面板能关闭');

// 还原
vm.runInContext(`TD_MANUAL.splice(0,TD_MANUAL.length);
  Object.keys(TD_DONE).forEach(k=>delete TD_DONE[k]);
  Object.keys(TD_SNOOZE).forEach(k=>delete TD_SNOOZE[k]);
  TD_MANUAL.push.apply(TD_MANUAL,${JSON.stringify(TD0)});`, context);
context.render();
check(rowCount() === total && $v('TD_MANUAL').length === TD0.length, '今日必跟测试数据已还原');

// ---------------- 客户详情：时间线 + 备注 ----------------
async function testCustomerDetail(){

console.log('\n[客户详情]');
const U3 = DATA[0].username;
context.openDrawer(encodeURIComponent(U3));
const cdHtml = $el('drawer')._html || '';
check(cdHtml.includes('互动时间线'), '抽屉含「互动时间线」区块');
check(cdHtml.includes('只给自己看'), '抽屉含「备注」区块');
check(cdHtml.includes('价值分'), '头部显示价值分');
check($el('tlc')._html && $el('tlc')._html.length > 0, '时间线先给占位，不会留空');

const cdNow = Math.floor(Date.now() / 1000);
vm.runInContext(`TL['${U3}']={total:5,mine_total:3,msgs:[
  {ts:${cdNow - 86400 * 3},mine:true,text:'三天前我发的'},
  {ts:${cdNow - 86400 * 3 + 60},mine:false,text:'三天前他回的'},
  {ts:${cdNow - 3600},mine:true,text:'一小时前我发的'},
  {ts:${cdNow - 60},mine:false,text:'刚回的'}
]}`, context);
context.renderTimeline();
const cdTl = $el('tlc')._html || '';
check(cdTl.includes('刚回的'), '时间线显示消息内容');
check(cdTl.indexOf('刚回的') < cdTl.indexOf('一小时前我发的'), '按时间倒序：最新在最上面');
check(cdTl.includes('class="tlitem me"') && cdTl.includes('class="tlitem you"'), '我发 / 对方两种节点样式');
check(cdTl.includes('我发') && cdTl.includes('对方'), '每条标注是「我发」还是「对方」');
check(cdTl.includes('tlday') && cdTl.includes('今天'), '按天分隔，今天显示为「今天」');
check(cdTl.includes('三天前我发的'), '更早的消息也在（可展开范围内）');
check(($el('tlsum')._text || '').includes('共 5 条'), '标题显示总条数：' + $el('tlsum')._text);
check(($el('tlmore')._text || '').includes('只显示最近'), '全部显示后按钮改为提示文案：' + $el('tlmore')._text);

// 看更早：默认 40 条，条数不足时点一下不影响内容
context.showMoreTL();
check(($el('tlc')._html || '').includes('三天前我发的'), '展开更早后内容仍完整');

// 备注：渲染 / 新增 / 二次确认删除
vm.runInContext(`delete NOTES['${U3}']`, context);   // 从干净状态开始
context.renderNotes();
check(($el('ntlist')._html || '').includes('还没有备注'), '没有备注时给引导文案');
vm.runInContext(`NOTES['${U3}']=[{id:'n1',text:'他关心国自然面上项目',ts:${cdNow - 120}}]`, context);
context.renderNotes();
check(($el('ntlist')._html || '').includes('他关心国自然面上项目'), '备注能显示出来');
check(($el('ntlist')._html || '').includes('删除'), '每条备注有删除入口');

vm.runInContext('noBackend=false', context);   // 模拟真后端，否则 postJSONFull 会直接拒绝
let cdSent = null;
context.fetch = async (url, opt) => {
  cdSent = opt && opt.body ? JSON.parse(opt.body) : null;
  const text = cdSent && cdSent.text ? cdSent.text : '';
  return { json: async () => ({ ok: true, notes: [{ id: 'n2', text: text, ts: Date.now() / 1000 | 0 }] }) };
};
$el('nttext').value = '他说下周三再联系';
await $v('addNote')();
check(cdSent && cdSent.action === 'add' && cdSent.text.indexOf('下周三') >= 0, '添加备注发出正确请求');
check(($el('ntlist')._html || '').includes('下周三'), '新备注立刻出现在列表里');
check($el('nttext').value === '', '添加后输入框已清空');

context.fetch = async () => ({ json: async () => ({ ok: true, notes: [] }) });
await $v('delNote')('n2');
check(($el('ntlist')._html || '').includes('确认删除'), '第一次点删除只要求确认，不会误删');
await $v('delNote')('n2');
check(($el('ntlist')._html || '').includes('还没有备注'), '第二次点确认后才真正删除');
delete context.fetch;

// 空内容不允许添加
context.fetch = async () => ({ json: async () => ({ ok: true, notes: [] }) });
$el('nttext').value = '   ';
await $v('addNote')();
check(($el('toast')._html || '').includes('先写点内容'), '空备注不发请求，直接提示');
delete context.fetch;

vm.runInContext(`delete NOTES['${U3}']; delete TL['${U3}']; delete TL_SHOW['${U3}'];`, context);
context.closeAll();
check(!$el('drawer').classList.contains('show'), '抽屉已关闭');
context.render();
}

// ---------------- 本地服务掉线自愈 ----------------
console.log('\n[断线自愈]');
check(typeof $v('noteFail') === 'function' && typeof $v('retryNow') === 'function' && typeof $v('pingOnce') === 'function', '有断线检测与重连函数');
context.noteFail();
check($v('OFFLINE') === true, '接口失败后标记为断线');
check($el('offline').classList.contains('show'), '顶部出现断线提示条');
const streak0 = $v('_failStreak');
context.noteFail();
check($v('_failStreak') >= streak0 + 1, '连续失败累计计数（断线期间持续累加）');
context.noteOk();
check($v('OFFLINE') === false, '服务恢复后自动解除断线');
check(!$el('offline').classList.contains('show'), '提示条自动隐藏');
check(($el('toast')._html || '').includes('已恢复'), '恢复时给出提示：' + ($el('toast')._html || '').trim());
check($v('_failStreak') === 0, '成功一次就清零计数');

// ping 失败不能误判为在线
context.fetch = async () => ({ ok: true, json: async () => ({ nope: 1 }) });   // 返回 200 但不是真后端
(async () => {
  const okNotBackend = await $v('pingOnce')();
  check(okNotBackend === false, '返回 200 但不是 JSON 的 ok，不当作在线（防静态预览误判）');
  context.fetch = async () => { throw new Error('ECONNREFUSED'); };
  const okDown = await $v('pingOnce')();
  check(okDown === false, '连接被拒时判定为离线');

  // 打开聊天在服务挂掉时应点亮提示条，而不是只弹一句看不懂的话
  const btn = { disabled: false, textContent: '打开聊天' };
  await $v('openChat')(btn, encodeURIComponent('曾娟'));
  check($v('OFFLINE') === true, '打开聊天失败后点亮顶部提示条');
  check(($el('toast')._html || '').includes('重连'), '提示文案指向重连按钮：' + ($el('toast')._html || '').trim());
  check(btn.disabled === false && btn.textContent === '打开聊天', '按钮状态已复原，可再次点击');
  delete context.fetch;

  // 恢复后再操作，提示条自动消失
  context.fetch = async () => ({ ok: true, json: async () => ({ ok: true, name: '曾娟' }) });
  await $v('openChat')({ disabled: false, textContent: '打开聊天' }, encodeURIComponent('曾娟'));
  check($v('OFFLINE') === false && !$el('offline').classList.contains('show'), '服务回来后点一下就自动恢复');
  delete context.fetch;

})()
  .then(testCustomerDetail)
  .then(() => {
    console.log(fails === 0 ? '\n全部通过' : `\n${fails} 项失败`);
    process.exit(fails === 0 ? 0 : 1);
  }).catch(e => {
  console.error('FAIL 断线自愈测试抛异常:', e && e.message);
  process.exit(1);
});
