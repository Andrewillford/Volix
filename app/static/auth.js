/* Tema claro/escuro (Volix). Aplica o tema salvo o quanto antes (evita flash) e
   injeta o botão de alternância no canto inferior direito. Padrão = escuro.
   A escolha persiste em localStorage por navegador. Independe do login. */
(function () {
  var KEY = 'loghub-theme';
  var root = document.documentElement;
  function atual() { return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
  function aplica(t) { root.setAttribute('data-theme', t === 'light' ? 'light' : 'dark'); }
  try { aplica(localStorage.getItem(KEY)); } catch (e) { aplica('dark'); }
  var btn;
  function pinta() {
    if (!btn) return;
    var claro = atual() === 'light';
    btn.textContent = claro ? '🌙' : '☀️';
    btn.title = claro ? 'Mudar para o tema escuro' : 'Mudar para o tema claro';
    btn.setAttribute('aria-label', btn.title);
  }
  function alterna() {
    var prox = atual() === 'light' ? 'dark' : 'light';
    aplica(prox);
    try { localStorage.setItem(KEY, prox); } catch (e) {}
    pinta();
  }
  function monta() {
    if (document.getElementById('themeToggle')) return;
    btn = document.createElement('button');
    btn.id = 'themeToggle';
    btn.type = 'button';
    btn.className = 'theme-toggle';
    btn.addEventListener('click', alterna);
    document.body.appendChild(btn);
    pinta();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', monta);
  else monta();
})();

/* Auth UI compartilhada dos painéis (incluída nas 6 telas + Usuários).
   - Descobre quem está logado (/api/auth/status).
   - Mostra "logado como … · Sair" no rodapé da sidebar.
   - Se admin, injeta o link "👤 Usuários" na seção Configuração.
   O middleware do servidor já protege as rotas; aqui é só a camada visual. */
(function () {
  async function boot() {
    let st;
    try {
      st = await fetch('/api/auth/status').then(r => r.json());
    } catch (e) { return; }
    if (!st || !st.authenticated) { location.href = '/login'; return; }
    const user = st.user || {};
    const nav = document.querySelector('.side-nav');
    const sidebar = document.querySelector('.sidebar');
    const path = location.pathname;

    // Links de administração (só admin) — na seção Configuração, após o Cadastro:
    // Usuários e Log de Acesso. Inseridos na ordem para ficarem juntos.
    if (nav && user.is_admin) {
      const cad = nav.querySelector('a[href="/cadastro"]');
      const adminLinks = [
        { href: '/usuarios', txt: '👤 Usuários' },
        { href: '/log-acesso', txt: '🧾 Log de Acesso' },
      ];
      let ancora = cad;
      adminLinks.forEach(({ href, txt }) => {
        if (nav.querySelector('a[href="' + href + '"]')) { ancora = nav.querySelector('a[href="' + href + '"]'); return; }
        const a = document.createElement('a');
        a.href = href;
        a.textContent = txt;
        if (path === href) a.className = 'active';
        if (ancora && ancora.nextSibling) ancora.parentNode.insertBefore(a, ancora.nextSibling);
        else nav.appendChild(a);
        ancora = a;
      });
    }

    // Esconde da sidebar as áreas que o usuário não pode ver (o servidor já bloqueia
    // as rotas; aqui é só não mostrar o que ele não acessa). Admin vê tudo.
    const AREA_DA_ROTA = {
      '/audit': 'financeiro', '/percent-nf': 'financeiro', '/conferencia': 'financeiro',
      '/romaneio': 'expedicao',
      '/coletas': 'suprimentos',
      '/tracking': 'entregas', '/scorecard': 'entregas', '/ocorrencias': 'entregas',
      '/vendas': 'comercial',
      '/cadastro': 'config',
    };
    const veArea = (area) => user.is_admin || (user.areas || []).includes('*') || (user.areas || []).includes(area);
    if (nav && !user.is_admin) {
      nav.querySelectorAll('a[href]').forEach(a => {
        const area = AREA_DA_ROTA[a.getAttribute('href')];
        if (area && !veArea(area)) a.remove();
      });
      // esconde cabeçalhos de seção que ficaram sem nenhum link
      const filhos = Array.from(nav.children);
      filhos.forEach((el, i) => {
        if (!el.classList.contains('sec')) return;
        let temLink = false;
        for (let j = i + 1; j < filhos.length; j++) {
          if (filhos[j].classList.contains('sec')) break;
          if (filhos[j].tagName === 'A') { temLink = true; break; }
        }
        if (!temLink) el.style.display = 'none';
      });
    }

    // Esconde os botões/links de "Exportar Excel" para quem não tem a permissão.
    const podeExcel = user.is_admin || !!user.pode_exportar;
    if (!podeExcel) {
      const hideExcel = () => {
        document.querySelectorAll('a[href*="export.xlsx"]').forEach(el => { el.style.display = 'none'; });
        document.querySelectorAll('button, a').forEach(el => {
          if (/excel/i.test(el.textContent || '')) el.style.display = 'none';
        });
      };
      hideExcel();
      // alguns painéis criam o botão depois de carregar os dados — reaplica nas mudanças do DOM
      try { new MutationObserver(hideExcel).observe(document.body, { childList: true, subtree: true }); } catch (e) {}
    }

    // Rodapé da sidebar: usuário + Sair.
    if (sidebar && !document.getElementById('sideUser')) {
      const box = document.createElement('div');
      box.id = 'sideUser';
      box.style.cssText = 'margin-top:auto;padding:12px 8px 2px;border-top:1px solid var(--border);'
        + 'font-size:11.5px;color:var(--muted);display:flex;align-items:center;justify-content:space-between;gap:8px;';
      const nome = user.nome || user.login || 'usuário';
      const quem = document.createElement('span');
      quem.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      quem.textContent = '👤 ' + nome;
      quem.title = 'Logado como ' + nome + (user.login ? ' (' + user.login + ')' : '');
      const sair = document.createElement('button');
      sair.textContent = 'Sair';
      sair.style.cssText = 'background:transparent;border:1px solid var(--border);color:var(--muted);'
        + 'border-radius:6px;padding:4px 10px;font-size:11.5px;cursor:pointer;flex:none;';
      sair.addEventListener('mouseover', () => { sair.style.color = 'var(--text)'; sair.style.borderColor = 'var(--volix)'; });
      sair.addEventListener('mouseout', () => { sair.style.color = 'var(--muted)'; sair.style.borderColor = 'var(--border)'; });
      sair.addEventListener('click', async () => {
        try { await fetch('/api/logout', { method: 'POST' }); } catch (e) {}
        location.href = '/login';
      });
      box.appendChild(quem); box.appendChild(sair);
      sidebar.appendChild(box);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
