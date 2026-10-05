# Migração do LogHub para o servidor da empresa

Guia para tirar o sistema do PC do André e colocá-lo rodando 24/7 no servidor que
a empresa comprou. Cobre **Windows Server** e **Linux (Ubuntu/Debian)** — siga o
bloco do sistema operacional do seu servidor.

**Decisões desta migração**
- Os **dados atuais vão junto** (usuários/senhas, histórico de auditoria, base de
  acordo, torre de controle, conferência, vendas, coletas, romaneio).
- Acesso **pela rede interna via IP**; quem estiver fora entra pela **VPN da
  empresa** e enxerga o mesmo IP interno. Sem domínio público nem HTTPS.

**Estado do repositório (04/09/2026)**
- Branch `main` no GitHub com a base pronta para produção: interface na paleta
  Ardósia, estáticos com `no-cache`, código sem itens mortos (ruff limpo) e os
  módulos de integração (Olist, e-mail EDI, SSW, Jamef).
- O **manual de uso** já vem embutido em `app/static/docs/manual-freight-gateway.pdf`
  (17 páginas, passo a passo por painel) e é aberto pelo próprio menu do sistema, em
  **Ajuda → Manual do Sistema (PDF)**. Não precisa de nada extra no servidor para isso
  — em especial, **o servidor não precisa de Playwright** (é só ferramenta de quem
  regenera o manual, não do sistema rodando).
- O mapa de arquitetura (camadas, módulos, fluxos e topologia) está registrado à parte
  como referência de manutenção.

---

## 1. O que vai para o servidor (e como)

| Item | Como chega no servidor | Contém segredo/dados? |
|---|---|---|
| Código do sistema | `git clone` do repositório | Não |
| Dependências Python | `pip install -r requirements.txt` | Não |
| **`.env`** (configuração + segredos) | **cópia manual** do PC | **Sim — token Olist, senha do e-mail** |
| **`tables/*.db`** (8 bancos) | **cópia manual** do PC | **Sim — usuários, senhas, histórico** |
| Estrutura de pastas operacionais | criada por comando | Não |

> O `.env` e os `.db` **nunca estão no git** (têm segredos e dados reais). Eles são
> copiados à mão, uma vez, do seu PC para o servidor.

Repositório: `https://github.com/andregavioes27-pixel/volix-logistica.git`

---

## 2. Pré-requisitos no servidor

- **Python 3.11 ou superior** (o sistema roda hoje no 3.14).
- **git**.
- Acesso de administrador ao servidor.
- Um IP fixo na rede interna (peça ao TI para reservar; ex.: `192.168.1.50`).
- A porta **8000** liberada no firewall do servidor para a rede interna.

---

## 3. Levar os arquivos com segredo/dados do seu PC

Antes de mexer no servidor, junte num pen drive (ou pasta de rede) **a partir do
seu PC**:

1. O arquivo **`.env`** que está na raiz do projeto.
2. Os **8 bancos** da pasta `tables/`:
   - `audit_history.db`, `auth.db`, `cadastro.db`, `coletas.db`,
     `conferencia.db`, `romaneio.db`, `taxas.db`, `vendas.db`
3. (Opcional) A pasta `tables/` também guarda ajustes em JSON
   (`transportadoras_cnpj.json`, `ocorrencias_codigos.json`, metas etc.). O que é
   versionado vem pelo git; se você editou algum à mão, copie junto.

> **Feche o sistema no seu PC antes de copiar os `.db`** (pare o servidor local),
> para o banco não estar sendo escrito no momento da cópia.

---

## 4. Instalar o sistema no servidor

### 4.1 Clonar o repositório

**Windows Server** (PowerShell) — escolha uma pasta, ex. `C:\LogHub`:
```powershell
cd C:\
git clone https://github.com/andregavioes27-pixel/volix-logistica.git LogHub
cd C:\LogHub
```

**Linux** — ex. `/opt/loghub`:
```bash
sudo git clone https://github.com/andregavioes27-pixel/volix-logistica.git /opt/loghub
cd /opt/loghub
```

### 4.2 Criar o ambiente Python e instalar dependências

**Windows Server:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux:**
```bash
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
```

### 4.3 Colocar o `.env` e os dados

1. Copie o **`.env`** que você trouxe do PC para a **raiz do projeto** no servidor
   (`C:\LogHub\.env` ou `/opt/loghub/.env`).
2. Copie os **8 arquivos `.db`** para a pasta `tables/` do servidor.
3. Ajuste uma linha no `.env` para marcar que este é o ambiente de produção (tira
   a faixa vermelha de "homologação"):
   ```
   LOGHUB_AMBIENTE=producao
   ```

### 4.4 Criar as pastas operacionais

O sistema lê/escreve arquivos de CTe, EDI e DOCCOB nessas pastas.

**Windows Server:**
```powershell
mkdir C:\LogHub\cte_xml\entrada, C:\LogHub\edi\entrada, C:\LogHub\doccob -Force
```

**Linux:**
```bash
mkdir -p /opt/loghub/cte_xml/entrada /opt/loghub/edi/entrada /opt/loghub/doccob
```

> As subpastas `cte_xml/processados/ok`, `cte_xml/processados/nok` e
> `edi/processados` são criadas sozinhas quando o sistema processa o primeiro
> arquivo.

### 4.5 Teste rápido (rodar na mão uma vez)

**Windows Server:**
```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Linux:**
```bash
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abra no navegador do próprio servidor `http://127.0.0.1:8000` e faça login com um
usuário que já existia no seu PC (os dados vieram junto). Funcionou? Pare com
`Ctrl+C` e siga para transformar em serviço, para subir sozinho no boot.

---

## 5. Rodar como serviço (sobe sozinho, reinicia se cair)

### 5.1 Windows Server — via NSSM

[NSSM](https://nssm.cc/) transforma o comando em serviço do Windows.

1. Baixe o NSSM e coloque `nssm.exe` em `C:\LogHub\nssm.exe`.
2. Em um PowerShell **como administrador**:
```powershell
C:\LogHub\nssm.exe install LogHub "C:\LogHub\.venv\Scripts\python.exe" "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
C:\LogHub\nssm.exe set LogHub AppDirectory C:\LogHub
C:\LogHub\nssm.exe set LogHub Start SERVICE_AUTO_START
C:\LogHub\nssm.exe start LogHub
```
- Parar: `C:\LogHub\nssm.exe stop LogHub`
- Reiniciar (após atualizar): `C:\LogHub\nssm.exe restart LogHub`
- Ver estado: `C:\LogHub\nssm.exe status LogHub`

### 5.2 Linux — via systemd

Crie `/etc/systemd/system/loghub.service`:
```ini
[Unit]
Description=LogHub - Freight Gateway
After=network.target

[Service]
WorkingDirectory=/opt/loghub
ExecStart=/opt/loghub/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
User=www-data

[Install]
WantedBy=multi-user.target
```
Depois:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loghub
sudo systemctl status loghub      # conferir
```
- Reiniciar (após atualizar): `sudo systemctl restart loghub`
- Ver logs: `sudo journalctl -u loghub -f`

---

## 6. Firewall e acesso

1. Libere a porta **8000** no firewall do servidor para a rede interna.

   **Windows Server:**
   ```powershell
   New-NetFirewallRule -DisplayName "LogHub 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
   ```
   **Linux (ufw):**
   ```bash
   sudo ufw allow 8000/tcp
   ```

2. **Acesso interno:** de qualquer PC da empresa, `http://IP_DO_SERVIDOR:8000`
   (ex.: `http://192.168.1.50:8000`).
3. **Acesso externo:** quem está fora conecta na **VPN da empresa** e usa a mesma
   URL interna. Nada muda no sistema — a VPN coloca a pessoa "dentro" da rede.

> Como o acesso é interno/VPN, não é obrigatório domínio nem HTTPS. Se um dia
> quiserem `https://loghub.empresa.com`, dá para pôr um proxy reverso
> (nginx/IIS) na frente — fica para um passo futuro.

---

## 7. Depois de subir — conferir

- [ ] Login funciona com os usuários antigos.
- [ ] A faixa vermelha de "homologação" **não** aparece (confirma `LOGHUB_AMBIENTE=producao`).
- [ ] Painéis abrem com o histórico de sempre (auditoria, torre, vendas…).
- [ ] Integração Olist puxando (se usar): abrir Romaneio/Vendas e conferir.
- [ ] Carteiro de e-mail lendo (se `EMAIL_AUTO=1`): mandar um EDI de teste para a
      caixa e ver chegar na torre.
- [ ] Acesso pela VPN de um PC externo.

---

## 8. Backup dos dados no servidor

Os 8 `.db` são o que não pode se perder. Agende uma cópia diária.

**Windows Server** — Tarefa Agendada rodando:
```powershell
Copy-Item C:\LogHub\tables\*.db "C:\Backups\LogHub\$(Get-Date -Format yyyy-MM-dd)\" -Force
```
(crie a pasta destino antes; aponte a Tarefa Agendada para um `.ps1` com essa linha.)

**Linux** — `cron` diário (`crontab -e`):
```bash
0 2 * * * mkdir -p /var/backups/loghub/$(date +\%F) && cp /opt/loghub/tables/*.db /var/backups/loghub/$(date +\%F)/
```

Guarde também uma cópia do **`.env`** em local seguro (tem os segredos e não está
no git).

---

## 9. Atualizações futuras (quando houver melhorias)

No servidor, dentro da pasta do projeto:

**Windows Server:**
```powershell
cd C:\LogHub
git pull
.\.venv\Scripts\pip.exe install -r requirements.txt   # só se as dependências mudaram
C:\LogHub\nssm.exe restart LogHub
```

**Linux:**
```bash
cd /opt/loghub
sudo git pull
sudo ./.venv/bin/pip install -r requirements.txt      # só se as dependências mudaram
sudo systemctl restart loghub
```

Como os estáticos agora saem com `Cache-Control: no-cache`, as telas atualizam nos
navegadores sem precisar de `Ctrl+F5`.

---

## 10. Resumo de uma tela

1. Servidor com Python 3.11+ e git; IP fixo; porta 8000 liberada.
2. `git clone` → `venv` → `pip install -r requirements.txt`.
3. Copiar do PC: **`.env`** e os **8 `.db`**; setar `LOGHUB_AMBIENTE=producao`.
4. Criar pastas `cte_xml/entrada`, `edi/entrada`, `doccob`.
5. Rodar como serviço (NSSM no Windows, systemd no Linux).
6. Acessar por `http://IP:8000` (interno) ou VPN (externo).
7. Agendar backup diário dos `.db`.
8. Atualizar com `git pull` + restart do serviço.
