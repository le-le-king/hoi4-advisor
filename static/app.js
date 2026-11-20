/* 世界资料库引导页
 *
 * 跟宿主的 ui-api 打交道：POST /ui-api/runs 起一次调用，轮询到终态，
 * 再 GET /export 取业务数据（跟 qqbetter_auto_reply 的 callPlugin 一个路子，
 * 只是这里用轮询代替 SSE，少一个文件、少一种出错姿势）。
 */
(function () {
  'use strict';

  // 插件 id 固定，别从 URL 猜——宿主给的页面路径不一定带 /plugin/<id>/
  var pluginId = 'hoi4_advisor';
  // 宿主的面板接口挂在 <origin>/runs 这一组全局路由上（跟 qqbetter_auto_reply 同一个），
  // 靠 body 里的 plugin_id 区分是谁。之前自己拼 /plugin/<id>/ui-api/runs 就是那个 404。
  var RUNS = '/runs';

  var S = {
    game: null,       // {dir, version, mods}
    options: [],      // preview 出来的候选世界
    selected: 0,
    build: null,
    worlds: [],
    current: '',
    modsAll: [],      // 全量 mod 清单（含启动器没启用的），给勾选用
    skipped: []       // 启用了但读不进来的 mod（要如实显示）
  };

  // ── 小工具 ────────────────────────────
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function num(n) { return (n == null ? 0 : n).toLocaleString('zh-CN'); }

  // 宿主把面板放进 sandboxed iframe，alert / confirm / prompt 全被浏览器吞掉，
  // 点了就像没反应。统一用页面内的提示条代替。
  function toast(msg, kind) {
    var el = 	oast;
    if (!el) return;
    el.textContent = msg;
    el.className = 'toast show' + (kind === 'err' ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.className = 'toast'; },
                          kind === 'err' ? 6000 : 3200);
  }

  async function call(entry, args) {
    var resp = await fetch(RUNS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plugin_id: pluginId, entry_id: entry, args: args || {} })
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var j = await resp.json();
    var rid = j.run_id || j.id;
    if (!rid) throw new Error('没拿到 run_id');
    for (var i = 0; i < 300; i++) {
      await sleep(400);
      var p = await fetch(RUNS + '/' + rid);
      if (!p.ok) continue;
      var rec = await p.json();
      if (['succeeded', 'failed', 'canceled', 'timeout'].indexOf(rec.status) < 0) continue;
      if (rec.status !== 'succeeded') throw new Error('调用失败：' + rec.status);
      var exp = await fetch(RUNS + '/' + rid + '/export');
      if (!exp.ok) return {};
      var payload = await exp.json();
      var items = payload.items || [];
      var item = items.filter(function (x) { return x.type === 'json' && x.json; })[0] || items[0];
      var raw = (item && item.json) || {};
      // 剥掉宿主的 {success, data} 壳
      while (raw && raw.data && typeof raw.data === 'object' &&
             ('success' in raw.data || 'error' in raw.data)) raw = raw.data;
      if (raw && raw.success && raw.data && typeof raw.data === 'object') raw = raw.data;
      return raw || {};
    }
    throw new Error('等太久了');
  }

  // ── 步骤切换 ──────────────────────────
  function go(n) {
    ['s1', 's2', 's3', 's4', 's5'].forEach(function (id) {
      var el = $(id);
      if (el) el.classList.toggle('hidden', Number(id.slice(1)) !== n);
    });
    var dots = document.querySelectorAll('.step-dot');
    for (var i = 0; i < dots.length; i++) {
      var s = Number(dots[i].dataset.s);
      dots[i].classList.toggle('on', s === n);
      dots[i].classList.toggle('done', s < n);
    }
    $('steps').classList.toggle('hidden', n === 5);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ── 第 1 步 ───────────────────────────
  function renderGame(g) {
    S.game = g;
    var dir = $('s1-game'), ver = $('s1-ver'), mods = $('s1-mods'), note = $('s1-note');
    if (!g) {
      dir.textContent = '没找到';
      ver.textContent = '—';
      mods.textContent = '—';
      note.className = 'note warn';
      note.innerHTML = '没找到游戏目录。点「手动指定目录」，选到 <span class="mono">Hearts of Iron IV</span> 那一层（里面有 <span class="mono">hoi4.exe</span>）。';
      $('s1-next').disabled = true;
      return;
    }
    dir.innerHTML = '<span class="mono">' + esc(g.dir) + '</span>';
    ver.textContent = g.version || '未知';
    var ms = g.mods || [];
    if (!ms.length) {
      mods.innerHTML = '<span class="chip off">没有启用任何 mod</span>';
    } else {
      mods.innerHTML = ms.map(function (m) {
        return '<span class="chip ok">' + esc(m.name) +
               (m.version ? ' ' + esc(m.version) : '') + '</span>';
      }).join('');
    }
    note.className = 'note';
    note.textContent = '找到 ' + (ms.length ? ms.length + ' 个启用的 mod' : '原版环境') + '，可以往下走了。';
    $('s1-next').disabled = false;
  }

  async function loadSnapshot(gameDir, stay) {
    var r = await call('world_snapshot', gameDir ? { game_dir: gameDir } : {});
    var game = (r.game && r.game.game_dirs && r.game.game_dirs[0]) || null;
    renderGame(game);
    S.worlds = r.worlds || [];
    S.current = r.current || '';
    // stay=true 时不要自作主张跳页面（比如刚生成完、正在看完成页）
    if (S.worlds.length && !gameDir && !stay) {
      renderWorlds();
      go(5);          // 已经有资料库，直接进管理页
    }
  }

  async function loadPreview() {
    if (!S.game) return;
    $('s2-list').innerHTML = '<div class="note">正在清点省和州…</div>';
    try {
      var r = await call('world_preview', { game_dir: S.game.dir });
      S.options = r.options || [];
      S.modsAll = r.all_mods || [];
      S.skipped = r.skipped || [];
      S.selected = Math.max(0, S.options.length - 1);   // 默认选「带 mod」那份
      renderOptions();
      renderModPicker();
    } catch (e) {
      $('s2-list').innerHTML = '<div class="note err">清点失败：' + esc(e.message) + '</div>';
    }
  }

  // 全量 mod 勾选：想读启动器里没启用的 mod（给 TFR 单独建一份数据）用这里
  function renderModPicker() {
    var wrap = $('s2-pickwrap'), box = $('s2-mods'), note = $('s2-picknote');
    if (!wrap || !box) return;
    if (!S.modsAll.length) { wrap.classList.add('hidden'); return; }
    wrap.classList.remove('hidden');
    box.innerHTML = S.modsAll.map(function (m, i) {
      return '<label class="opt" style="display:flex;gap:8px;align-items:center">' +
        '<input type="checkbox" data-mi="' + i + '"' + (m.enabled ? ' checked' : '') + '>' +
        '<span>' + esc(m.name) + (m.enabled ? ' <b>（启动器里已启用）</b>' : '') +
        '</span></label>';
    }).join('');
    var bits = [];
    if (S.skipped.length) {
      bits.push('⚠ 有 ' + S.skipped.length + ' 个启用的 mod 读不进来：' +
        S.skipped.map(function (s) { return esc(s.name) + '（' + esc(s.reason) + '）'; }).join('；'));
    }
    note.innerHTML = bits.join('<br>');
    note.className = bits.length ? 'note err' : 'note hidden';
  }

  function pickedModNames() {
    var out = [];
    document.querySelectorAll('#s2-mods input[data-mi]').forEach(function (el) {
      if (el.checked) {
        var m = S.modsAll[parseInt(el.getAttribute('data-mi'), 10)];
        if (m) out.push(m.name);
      }
    });
    return out;
  }

  function renderOptions() {
    var box = $('s2-list');
    if (!S.options.length) {
      box.innerHTML = '<div class="note err">这个目录下没有能读的世界。</div>';
      $('s2-build').disabled = true;
      return;
    }
    $('s2-build').disabled = false;
    box.innerHTML = S.options.map(function (o, i) {
      var isMod = (o.mods || []).length > 0;
      return '<div class="opt' + (i === S.selected ? ' sel' : '') + '" data-i="' + i + '">' +
        '<div><div class="t">' + esc(o.label) + '</div>' +
        '<div class="m">' + (isMod ? esc((o.mod_names || []).join('、')) : '不带任何 mod 的原始地图') +
        (o.error ? '<br><span style="color:#ef4444">' + esc(o.error) + '</span>' : '') +
        '</div></div>' +
        '<div class="n">' +
          '<b>' + num(o.provinces) + '</b>省' +
          '<b>' + num(o.states) + '</b>州' +
        '</div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.opt'), function (el) {
      el.onclick = function () {
        S.selected = Number(el.dataset.i);
        renderOptions();
      };
    });
  }

  // ── 第 3 步：进度 ─────────────────────
  function applyProgress(b) {
    if (!b) return;
    S.build = b;
    var pct = Math.max(0, Math.min(100, Number(b.percent) || 0));
    $('s3-fill').style.width = pct + '%';
    $('s3-pct').textContent = pct.toFixed(0) + '%';
    $('s3-phase').textContent = b.phase || (b.running ? '读取中…' : '准备中…');
    if (b.error) {
      $('s3-log').className = 'note err';
      $('s3-log').textContent = '出错了：' + b.error;
    } else if (!b.running && b.ok) {
      $('s3-log').className = 'note';
      $('s3-log').textContent = '读好了。';
    }
  }

  async function pollBuild() {
    for (var i = 0; i < 900; i++) {           // 最长约 6 分钟
      await sleep(500);
      var b = null;
      try { b = await call('world_progress'); } catch (e) { continue; }
      applyProgress(b);
      if (!b.running) {
        if (b.ok) {
          showDone(b);
          return;
        }
        $('s3-log').className = 'note err';
        $('s3-log').textContent = '出错了：' + (b.error || '未知原因');
        $('s3-back').disabled = false;
        return;
      }
    }
    $('s3-log').className = 'note err';
    $('s3-log').textContent = '等太久了，可能卡住了。';
    $('s3-back').disabled = false;
  }

  function showDone(b) {
    var r = b.result || {};
    $('s4-name').innerHTML = '<span class="mono">' + esc(b.world_id || '') + '</span>';
    $('s4-size').textContent = num(r.provinces) + ' 个省 / ' + num(r.states) +
                               ' 个州 / ' + num(r.countries) + ' 个国家';
    $('s4-time').textContent = (r.build_seconds || 0) + ' 秒';
    var warns = b.warnings || [];
    var wn = $('s4-warn');
    if (warns.length) {
      wn.classList.remove('hidden');
      wn.innerHTML = '<b>说明：</b><ul><li>' + warns.map(esc).join('</li><li>') + '</li></ul>';
    } else {
      wn.classList.add('hidden');
    }
    go(4);
    loadSnapshot(undefined, true);   // 只刷新数据，别把完成页顶掉
  }

  // ── 管理页 ────────────────────────────
  function renderWorlds() {
    var box = $('s5-list');
    if (!S.worlds.length) {
      box.innerHTML = '<div class="note">还没有任何资料库。点「再读一个世界」开始。</div>';
      return;
    }
    box.innerHTML = S.worlds.map(function (w) {
      return '<div class="wrow"><div>' +
        '<div class="t"><span class="dot' + (w.current ? ' on' : '') + '"></span>' + esc(w.label) + '</div>' +
        '<div class="m">' + num(w.provinces) + ' 省 · ' + num(w.states) + ' 州 · ' +
        num(w.countries) + ' 国名 · 邻接 ' + num(w.adjacency || 0) + ' 省 · 游戏 ' +
        esc(w.game_version || '?') + ' · 生成于 ' + esc(w.built_at || '?') +
        (w.stale ? ' · <b style="color:#d97706">⚠ 文件改过，建议重建</b>' : '') +
        '</div></div>' +
        '<div class="btns">' +
          (w.current ? '<span class="chip ok">当前使用中</span>'
                     : '<button data-sw="' + esc(w.id) + '">切到这个</button>') +
          (w.current ? '' : '<button class="danger" data-del="' + esc(w.id) + '">删除</button>') +
        '</div></div>';
    }).join('');

    Array.prototype.forEach.call(box.querySelectorAll('[data-sw]'), function (b) {
      b.onclick = async function () {
        b.disabled = true;
        try {
          await call('world_switch', { world_id: b.dataset.sw });
          await loadSnapshot();
        } catch (e) { toast('切换失败：' + e.message, 'err'); }
        b.disabled = false;
      };
    });
    Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function (b) {
      b.onclick = async function () {
        // confirm() 同样被 iframe 吞掉，改成「再点一次才真删」
        if (b.dataset.armed !== '1') {
          if (S.armedDel && S.armedDel !== b) {
            S.armedDel.dataset.armed = '';
            S.armedDel.textContent = '删除';
          }
          b.dataset.armed = '1';
          b.textContent = '再点一次删除';
          S.armedDel = b;
          toast('再点一次就真的删掉这份资料库（随时可以重读）');
          return;
        }
        b.dataset.armed = '';
        b.textContent = '删除';
        S.armedDel = null;
        b.disabled = true;
        try {
          await call('world_delete', { world_id: b.dataset.del });
          await loadSnapshot();
        } catch (e) { toast('删除失败：' + e.message, 'err'); }
        b.disabled = false;
      };
    });
  }

  // ── 事件绑定 ──────────────────────────
  function bind() {
    $('s1-rescan').onclick = function () { go(1); loadSnapshot().catch(function (e) { toast(e.message, 'err'); }); };
    $('s1-manual').onclick = function () {
      // 不能用 prompt()：面板在 sandboxed iframe 里，浏览器直接吞掉弹窗，看着就像"没反应"
      var row = $('s1-manrow');
      if (!row) return;
      row.classList.remove('hidden');
      var inp = $('s1-maninput');
      if (inp) { inp.value = S.game ? S.game.dir : ''; inp.focus(); inp.select(); }
    };
    $('s1-manok').onclick = function () {
      var inp = $('s1-maninput');
      var p = inp ? inp.value.trim() : '';
      if (!p) return;
      $('s1-manrow').classList.add('hidden');
      loadSnapshot(p).catch(function (e) { toast('读取失败：' + e.message, 'err'); });
    };
    $('s1-mancancel').onclick = function () {
      $('s1-manrow').classList.add('hidden');
    };
    $('s1-next').onclick = function () {
      if (!S.game) return;
      go(2);
      loadPreview();
    };
    $('s2-back').onclick = function () { go(1); };
    var startBuild = async function (payload) {
      $('s3-fill').style.width = '0%';
      $('s3-pct').textContent = '0%';
      $('s3-phase').textContent = '准备中…';
      $('s3-log').className = 'note';
      $('s3-log').textContent = '读地图是这次最慢的一步，扫完就快了。';
      $('s3-back').disabled = true;
      await call('world_build', payload);
      go(3);
      pollBuild();
    };
    if ($('s2-pickbuild')) {
      $('s2-pickbuild').onclick = async function () {
        var names = pickedModNames();
        $('s2-pickbuild').disabled = true;
        try {
          // 一个都不勾就是读原版——这也是一种合法选择，别拦
          await startBuild({ game_dir: S.game.dir, mod_names: names });
        } catch (e) {
          toast('启动失败：' + e.message, 'err');
        }
        $('s2-pickbuild').disabled = false;
      };
    }
    $('s2-build').onclick = async function () {
      var opt = S.options[S.selected];
      if (!opt) return;
      if (opt.error) { toast('这份读不了：' + opt.error, 'err'); return; }
      $('s2-build').disabled = true;
      try {
        var mods = (opt.mods || []).map(function (_, i) { return i + 1; });
        await startBuild({ game_dir: S.game.dir, mod_indexes: mods });
      } catch (e) {
        toast('启动失败：' + e.message, 'err');
      }
      $('s2-build').disabled = false;
    };
    $('s3-back').onclick = function () { go(2); };
    $('s4-done').onclick = function () { go(1); loadSnapshot(); };
    $('s4-manage').onclick = function () { loadSnapshot().then(function () { renderWorlds(); go(5); }); };
    $('s5-add').onclick = function () { go(1); };
    $('s5-back').onclick = function () { go(1); };
  }

  // ── 启动 ──────────────────────────────
  bind();
  loadSnapshot().catch(function (e) {
    renderGame(null);
    $('s1-note').className = 'note err';
    $('s1-note').textContent = '读不到插件状态：' + e.message;
  });
})();