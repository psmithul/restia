// Shared modal layer. Modals stack in #modal-root.
import { el } from './util.js';

export function openModal({ title = '', wide = false, locked = false } = {}) {
  const root = el(`<div class="modal-backdrop">
    <div class="modal ${wide ? 'modal-wide' : ''}" role="dialog" aria-modal="true" ${title ? `aria-label="${title}"` : ''}>
      ${title ? `<div class="modal-title">${title}</div>` : ''}
      <div class="modal-body"></div>
    </div>
  </div>`);
  document.getElementById('modal-root').appendChild(root);
  const close = () => root.remove();
  if (!locked) {
    root.addEventListener('pointerdown', (e) => { if (e.target === root) close(); });
    const onEsc = (e) => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onEsc); } };
    document.addEventListener('keydown', onEsc);
  }
  return { root, body: root.querySelector('.modal-body'), close };
}
