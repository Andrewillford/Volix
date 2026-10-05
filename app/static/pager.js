/* ============================================================================
   Pager — paginação acessível e reutilizável para as tabelas do LogHub.
   Renderiza (idempotente) dentro de `el`: seletor de tamanho de página,
   botões Anterior/Próxima e um resumo "X–Y de TOTAL" com aria-live para o
   leitor de tela anunciar a troca de página. Não guarda estado próprio: quem
   chama passa {offset, limit, total} e recebe os callbacks de navegação.

   Uso:
     Pager.render(document.querySelector('#pager'), {
       offset, limit, total,
       pageSizes: [50, 100, 200],
       onGo:    (novoOffset) => { offset = novoOffset; carregar(); },
       onLimit: (novoLimit)  => { limit = novoLimit; offset = 0; carregar(); },
     });
   ============================================================================ */
(function () {
  function render(el, o) {
    if (!el) return;
    const total = Math.max(0, o.total | 0);
    const limit = Math.max(1, o.limit | 0);
    const offset = Math.min(Math.max(0, o.offset | 0), Math.max(0, total - 1));
    const pageSizes = o.pageSizes || [50, 100, 200];
    const paginas = Math.max(1, Math.ceil(total / limit));
    const paginaAtual = Math.floor(offset / limit) + 1;
    const ini = total ? offset + 1 : 0;
    const fim = Math.min(offset + limit, total);
    const temPrev = offset > 0;
    const temNext = offset + limit < total;

    el.classList.add('pager');
    el.innerHTML =
      '<label class="pager-size">Por página ' +
        '<select class="pager-limit" aria-label="Registros por página">' +
          pageSizes.map(n => `<option value="${n}"${n === limit ? ' selected' : ''}>${n}</option>`).join('') +
        '</select>' +
      '</label>' +
      '<div class="pager-nav" role="group" aria-label="Navegação de páginas">' +
        `<button type="button" class="pager-btn pager-prev" aria-label="Página anterior"${temPrev ? '' : ' disabled'}>‹ Anterior</button>` +
        `<span class="pager-status" aria-live="polite">${ini}–${fim} de ${total.toLocaleString('pt-BR')} · pág. ${paginaAtual}/${paginas}</span>` +
        `<button type="button" class="pager-btn pager-next" aria-label="Próxima página"${temNext ? '' : ' disabled'}>Próxima ›</button>` +
      '</div>';

    const prev = el.querySelector('.pager-prev');
    const next = el.querySelector('.pager-next');
    const sel = el.querySelector('.pager-limit');
    if (prev) prev.addEventListener('click', () => { if (temPrev) o.onGo(Math.max(0, offset - limit)); });
    if (next) next.addEventListener('click', () => { if (temNext) o.onGo(offset + limit); });
    if (sel) sel.addEventListener('change', () => o.onLimit(parseInt(sel.value, 10) || limit));
  }

  window.Pager = { render };
})();
