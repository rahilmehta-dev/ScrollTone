// ── Copy helpers ──────────────────────────────────────────────────────────────
function cp(id, btn) {
  navigator.clipboard.writeText(document.getElementById(id).textContent).then(() => {
    const o = btn.textContent;
    btn.textContent = 'Copied!'; btn.classList.add('ok');
    setTimeout(() => { btn.textContent = o; btn.classList.remove('ok'); }, 2000);
  });
}
function cpBlock(id, btn) {
  navigator.clipboard.writeText(document.getElementById(id).innerText).then(() => {
    const o = btn.textContent;
    btn.textContent = 'Copied!'; btn.classList.add('ok');
    setTimeout(() => { btn.textContent = o; btn.classList.remove('ok'); }, 2000);
  });
}

// ── Mockup window close / open ────────────────────────────────────────────────
const mockup     = document.getElementById('mockup');
const redDot     = document.getElementById('red-dot');
let   isOpen     = true;
let   animLocked = false;

redDot.addEventListener('click', () => {
  if (!isOpen || animLocked) return;
  animLocked = true;

  redDot.classList.add('rippling');
  redDot.addEventListener('animationend', () => redDot.classList.remove('rippling'), { once: true });

  mockup.classList.add('closing');
  mockup.addEventListener('animationend', () => {
    mockup.classList.remove('closing');
    mockup.style.opacity       = '0';
    mockup.style.pointerEvents = 'none';
    isOpen = false;

    setTimeout(() => {
      mockup.style.opacity       = '';
      mockup.style.pointerEvents = '';
      mockup.classList.add('opening');
      mockup.addEventListener('animationend', () => {
        mockup.classList.remove('opening');
        isOpen     = true;
        animLocked = false;
      }, { once: true });
    }, 900);
  }, { once: true });
});

// ── Scroll-triggered reveal ───────────────────────────────────────────────────
const io = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.style.opacity   = '1';
      e.target.style.transform = 'translateY(0)';
      io.unobserve(e.target);
    }
  });
}, { threshold: 0.08 });

document.querySelectorAll('.feat').forEach((el, i) => {
  el.style.opacity    = '0';
  el.style.transform  = 'translateY(20px)';
  el.style.transition = `opacity .5s ease ${i * 0.07}s, transform .5s ease ${i * 0.07}s`;
  io.observe(el);
});
