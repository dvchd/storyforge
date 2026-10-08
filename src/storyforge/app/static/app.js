// Xem trước video, biên tập bóng thoại và phím tắt duyệt.
function videoPlayer(segs) {
  return {
    segs, i: 0, playing: false, subs: true, timer: null,
    get cur() { return this.segs[this.i] || { text: '', duration: 3, image_status: '' }; },
    play() {
      this.playing = true;
      clearTimeout(this.timer);
      this.$nextTick(() => {
        if (this.cur.audio) { this.$refs.a.currentTime = 0; this.$refs.a.play().catch(() => {}); }
        else { this.timer = setTimeout(() => this.next(), this.cur.duration * 1000); }
      });
    },
    pause() { this.playing = false; clearTimeout(this.timer); this.$refs.a.pause(); },
    toggle() { this.playing ? this.pause() : this.play(); },
    next() { if (this.i < this.segs.length - 1) { this.i++; if (this.playing) this.play(); } else { this.pause(); } },
    prev() { if (this.i > 0) { this.i--; if (this.playing) this.play(); } },
    jump(k) { this.i = Number(k); if (this.playing) this.play(); },
    key(e) {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
      if (e.code === 'Space') { e.preventDefault(); this.toggle(); }
      if (e.key === 'ArrowRight') this.next();
      if (e.key === 'ArrowLeft') this.prev();
    },
  };
}

function comicEditor(pages) {
  const post = (url, data) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(data || {})}).then(r => r.json());
  const drag = (ev, onMove, onUp) => {
    const el = ev.currentTarget; el.setPointerCapture(ev.pointerId);
    const move = e => onMove(e); const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); onUp(); };
    el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
  };
  return {
    pages,
    startDrag(ev, b) {
      if (ev.target.tagName === 'BUTTON') return;
      const box = ev.currentTarget.closest('.panel-inner').getBoundingClientRect();
      const sx = ev.clientX, sy = ev.clientY, ox = b.x, oy = b.y;
      drag(ev, e => {
        b.x = Math.min(0.95, Math.max(0, ox + (e.clientX - sx) / box.width));
        b.y = Math.min(0.95, Math.max(0, oy + (e.clientY - sy) / box.height));
      }, () => post(`/balloon/${b.id}`, {x: b.x, y: b.y}));
    },
    startResize(ev, b) {
      const box = ev.currentTarget.closest('.panel-inner').getBoundingClientRect();
      const sx = ev.clientX, ow = b.w;
      drag(ev, e => { b.w = Math.min(1 - b.x, Math.max(0.12, ow + (e.clientX - sx) / box.width)); },
           () => post(`/balloon/${b.id}`, {w: b.w}));
    },
    editText(b) {
      const t = prompt('Lời thoại', b.text);
      if (t !== null) { b.text = t; post(`/balloon/${b.id}`, {text: t}); }
    },
    async addBalloon(pn) {
      const t = prompt('Lời thoại mới', '');
      if (!t) return;
      const fd = new FormData(); fd.append('text', t); fd.append('kind', 'speech');
      pn.balloons.push(await fetch(`/panel/${pn.id}/balloon`, {method: 'POST', body: fd}).then(r => r.json()));
    },
    removeBalloon(pn, b) {
      if (!confirm('Xóa bóng thoại?')) return;
      post(`/balloon/${b.id}/delete`).then(() => { pn.balloons = pn.balloons.filter(x => x.id !== b.id); });
    },
  };
}

// Phím tắt: j/k chọn mục, a duyệt, r từ chối, e sửa, g tạo lại, ? trợ giúp.
(function () {
  let idx = -1;
  const rows = () => Array.from(document.querySelectorAll('.kbd-scope .row')).filter(r => r.offsetParent !== null);
  const focus = i => {
    const rs = rows(); if (!rs.length) return;
    idx = Math.max(0, Math.min(rs.length - 1, i));
    rs.forEach(r => r.classList.remove('kfocus'));
    rs[idx].classList.add('kfocus'); rs[idx].scrollIntoView({block: 'nearest', behavior: 'smooth'});
  };
  document.addEventListener('keydown', e => {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '?') { const h = document.getElementById('kbd-help'); if (h) h.hidden = !h.hidden; return; }
    if (!document.querySelector('.kbd-scope')) return;
    if (e.key === 'j') return focus(idx + 1);
    if (e.key === 'k') return focus(idx - 1);
    const cur = rows()[idx]; if (!cur) return;
    const btn = cur.querySelector(`[data-key="${e.key}"]`);
    if (btn) { e.preventDefault(); btn.click(); }
  });
  document.addEventListener('htmx:afterSwap', () => { if (idx >= 0) setTimeout(() => focus(idx), 50); });
})();
