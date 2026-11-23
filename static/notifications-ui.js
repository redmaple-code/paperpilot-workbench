/* All workflow notices share one resettable, accessible lifetime. */
(() => {
  const notice = document.querySelector('#workflowAnnouncement');
  if (!notice) return;
  let delay = null;
  let fade = null;
  const show = () => {
    clearTimeout(delay);
    clearTimeout(fade);
    notice.classList.remove('is-fading');
    notice.hidden = !notice.textContent.trim();
    if (notice.hidden) return;
    delay = setTimeout(() => {
      notice.classList.add('is-fading');
      fade = setTimeout(() => { notice.hidden = true; },
        matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 250);
    }, notice.dataset.kind === 'error' ? 8000 : 5000);
  };
  new MutationObserver(show).observe(notice, { childList: true, characterData: true, subtree: true });
  show();
})();
