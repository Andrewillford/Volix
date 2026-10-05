# LogHub — Ambientes (Homologação e Produção) e Roteiro de Deploy

> **Regra de ouro:** nada vai para produção sem ter sido testado **e aprovado**
> em homologação primeiro.

> Todos os arquivos desta estrutura ficam na pasta **`homologacao/`**.

---

## 1. Visão geral dos ambientes

| | **Produção** | **Homologação (teste)** |
|---|---|---|
| Pasta (hoje) | `...\André\IA CLAUDE` (OneDrive) | `C:\LogHub-HOMOLOG` (fora do OneDrive) |
| Branch Git | `main` | `homologacao` |
| Porta | **8000** → http://ANDRE:8000 | **8001** → http://localhost:8001 |
| Dados (`tables/`) | **reais** | **cópia** dos reais (atualizável) |
| Olist | token real (ligado) | **desligado** (sem token, `OLIST_AUTO=0`) |
| Config | `.env` (real) | `.env` (do `homologacao/.env.homolog.exemplo`) |
| Ligar / Parar | `INICIAR-LOGHUB.vbs` / `PARAR-LOGHUB.bat` (raiz) | `homologacao/INICIAR-HOMOLOG.vbs` / `homologacao/PARAR-HOMOLOG.bat` |

**Por que funciona sem configuração complicada:** cada banco aponta para
`<pasta-do-código>/tables`. Como homologação é **outra pasta**, ela já usa os
**próprios** bancos automaticamente — os dois ambientes nunca se misturam.

> ℹ️ **Mês que vem:** produção migra para um servidor. Homologação continua na
> sua máquina. Como tudo é por pasta + `.env`, a migração é só repetir o passo 2
> na máquina nova (ver seção 7).

---

## 2. Criar o ambiente de homologação (só uma vez)

**Pré-requisito:** de preferência, comite/pushe o que estiver pendente na
produção antes, para a cópia sair "limpa".

1. **(recomendado)** Pare a produção um instante para copiar os bancos
   consistentes: dois cliques em `PARAR-LOGHUB.bat` (na raiz).
2. Dois cliques em **`homologacao/criar-ambiente-homolog.bat`**.
   - Copia tudo para `C:\LogHub-HOMOLOG` (menos `.env`, `backups/` e caches).
   - Cria o `.env` de homologação (Olist desligado) a partir do exemplo.
3. Religue a produção: dois cliques em `INICIAR-LOGHUB.vbs` (na raiz).
4. Abra um terminal em `C:\LogHub-HOMOLOG` e crie a branch de teste:
   ```bash
   git checkout -b homologacao
   ```
5. Suba o homolog: dois cliques em **`C:\LogHub-HOMOLOG\homologacao\INICIAR-HOMOLOG.vbs`**.
6. Acesse **http://localhost:8001** e faça login (os usuários são os mesmos da
   cópia dos dados).

> Para atualizar os dados de homologação depois: pare o homolog
> (`homologacao/PARAR-HOMOLOG.bat`) e rode **`homologacao/atualizar-dados-homolog.bat`**
> na pasta de produção.

---

## 3. Fluxo de trabalho do dia a dia

```
   melhoria  ──►  branch homologacao  ──►  testar em http://localhost:8001
                                                     │
                                            aprovado ▼
   produção (main, :8000)  ◄──  merge homologacao → main  ◄──  push
```

1. Toda alteração entra primeiro na branch **`homologacao`** (na pasta de homolog).
2. Testa em **http://localhost:8001** (checklist da seção 6).
3. **Aprovado** → segue o roteiro de deploy da seção 4.
4. **Reprovado** → ajusta na `homologacao` e testa de novo. Produção **não** é tocada.

---

## 4. Roteiro de DEPLOY em produção (passo a passo)

> Faça em um horário de baixo movimento. Tempo típico: 2–5 minutos.

**Antes de começar:** a melhoria está testada e aprovada em homologação.

1. **Levar o código aprovado para a `main`** (uma vez, de qualquer terminal):
   ```bash
   git checkout homologacao && git pull
   git checkout main && git pull
   git merge homologacao
   git push origin main
   ```
2. **Na pasta de PRODUÇÃO**, atualizar o código:
   ```bash
   git checkout main
   git pull origin main
   ```
3. **Backup dos bancos** (rede de segurança) — na pasta de produção:
   ```bash
   python -c "import shutil,glob,os; from datetime import datetime; d='backups/pre-deploy_'+datetime.now().strftime('%Y%m%d_%H%M%S'); os.makedirs(d); [shutil.copy2(f,d) for f in glob.glob('tables/*.db')]; print('backup em',d)"
   ```
4. **Reiniciar a produção** (as migrações de banco rodam sozinhas no boot):
   - `PARAR-LOGHUB.bat` → aguarde 3s → `INICIAR-LOGHUB.vbs`.
5. **Smoke test** (2 min) — confirme que subiu bem:
   - [ ] Abre http://ANDRE:8000 e faz login.
   - [ ] Home carrega os KPIs.
   - [ ] Romaneio abre a fila e o cartão do Olist mostra "✓ Conectado".
   - [ ] Uma tela alterada nesta versão funciona como esperado.
6. **Pronto.** Se algo der errado, veja a seção 5.

---

## 5. Rollback (se o deploy der problema)

1. Pare a produção: `PARAR-LOGHUB.bat`.
2. Volte o código para a versão anterior:
   ```bash
   git log --oneline -5            # veja o hash do commit anterior
   git checkout <hash-anterior>    # ou: git reset --hard <hash-anterior>
   ```
3. **Se o problema foi de dados/migração**, restaure o backup da pasta
   `backups/pre-deploy_...` por cima de `tables/`.
4. Religue: `INICIAR-LOGHUB.vbs` e refaça o smoke test.
5. Depois, investigue a falha **em homologação** antes de tentar de novo.

---

## 6. Checklist de validação em homologação

Antes de aprovar para produção, confirme em http://localhost:8001:

- [ ] Login e permissões funcionam.
- [ ] As telas afetadas pela melhoria fazem o que deveriam.
- [ ] Nada quebrou nas telas vizinhas (Home, Romaneio, Cadastro).
- [ ] Sem erros no console do navegador (F12).
- [ ] A integração Olist aparece **desligada** (esperado em homolog).
- [ ] Se houve mudança de banco, a tela abre normal com os dados copiados.

---

## 7. Futuro: mover a PRODUÇÃO para o servidor

Quando a produção for para o servidor dedicado:

1. No servidor: instale Python, clone o repositório (`git clone ... && git checkout main`).
2. Copie os bancos reais atuais (`tables/*.db`) e o `.env` real para o servidor.
3. Abra a porta 8000 no firewall do servidor.
4. Configure o `INICIAR-LOGHUB.vbs` (ou um serviço do Windows) para subir no boot.
5. Atualize o endereço de acesso dos PCs (ex.: http://NOME-DO-SERVIDOR:8000).
6. **Homologação continua na sua máquina**, na porta 8001, sem mudança.
7. O `atualizar-dados-homolog.bat` passará a copiar do servidor (ajuste o
   caminho de origem para a pasta compartilhada/backup do servidor).

---

## Arquivos desta estrutura (pasta `homologacao/`)

| Arquivo | Função |
|---|---|
| `INICIAR-HOMOLOG.vbs` | Sobe o homolog na porta 8001 |
| `PARAR-HOMOLOG.bat` | Para o homolog |
| `.env.homolog.exemplo` | Modelo do `.env` de homologação (Olist desligado) |
| `criar-ambiente-homolog.bat` | Cria a pasta `C:\LogHub-HOMOLOG` a partir da produção |
| `atualizar-dados-homolog.bat` | Copia os bancos de produção para homologação |
| `AMBIENTES-E-DEPLOY.md` | Este documento |
| `INICIAR-LOGHUB.vbs` / `PARAR-LOGHUB.bat` (na raiz) | Ligar/parar a produção (porta 8000) |
