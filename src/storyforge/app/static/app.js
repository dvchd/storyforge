// Thanh phan Alpine cho xem truoc video va bien tap bong thoai.
function videoPlayer(segs) {
  return {
    segs, i: 0, playing: false, subs: true, t: 0, timer: null,
    get cur() { return this.segs[this.i] || { text: '', duration: 3 }; },
    play() {
      this.playing = true;
      clearTimeout(this.timer);
      this.$nextTick(() => {
        if (this.cur.audio) { this.$refs.a.play().catch(() => {}); }
        else { this.timer = setTimeout(() => this.next(), this.cur.duration * 1000); }
      });
    },
    pause() { this.playing = false; clearTimeout(this.timer); this.$refs.a.pause(); },
    toggle() { this.playing ? this.pause() : this.play(); },
    next() {
      if (this.i < this.segs.length - 1) { this.i++; if (this.playing) this.play(); }
      else { this.pause(); }
    },
    prev() { if (this.i > 0) { this.i--; if (this.playing) this.play(); } },
    jump(k) { this.i = Number(k); if (this.playing) this.play(); },
  };
}

function comicEditor(pages) {
  const post = (url, data) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(data || {})}).then(r => r.json());
  return {
    pages,
    startDrag(ev, b) {
      if (ev.target.tagName === 'BUTTON') return;
      const el = ev.currentTarget, box = el.closest('.panel-inner').getBoundingClientRect();
      const sx = ev.clientX, sy = ev.clientY, ox = b.x, oy = b.y;
      el.setPointerCapture(ev.pointerId);
      const move = e => {
        b.x = Math.min(0.95, Math.max(0, ox + (e.clientX - sx) / box.width));
        b.y = Math.min(0.95, Math.max(0, oy + (e.clientY - sy) / box.height));
      };
      const up = () => {
        el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up);
        post(`/balloon/${b.id}`, {x: b.x, y: b.y});
      };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    },
    editText(b) {
      const t = prompt('Lời thoại', b.text);
      if (t !== null) { b.text = t; post(`/balloon/${b.id}`, {text: t}); }
    },
    async addBalloon(pn) {
      const t = prompt('Lời thoại mới', '');
      if (!t) return;
      const fd = new FormData(); fd.append('text', t); fd.append('kind', 'speech');
      const b = await fetch(`/panel/${pn.id}/balloon`, {method: 'POST', body: fd}).then(r => r.json());
      pn.balloons.push(b);
    },
    removeBalloon(pn, b) {
      if (!confirm('Xóa bóng thoại?')) return;
      post(`/balloon/${b.id}/delete`).then(() => { pn.balloons = pn.balloons.filter(x => x.id !== b.id); });
    },
  };
}
