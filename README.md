# BRASA Financial Dashboard

Painel financeiro para diretores da BRASA, conectado ao QuickBooks Online.

---

## Estrutura

```
brasa-dashboard/
├── .env.example                  # Variáveis de ambiente necessárias
├── sync.py                       # Script de sincronização QB → JSON
├── requirements.txt
├── data/
│   └── directors.json            # Dados gerados pelo sync (commitados)
├── dashboard/
│   └── index.html                # Dashboard single-file
└── .github/workflows/
    └── sync.yml                  # GitHub Actions — roda a cada hora
```

---

## 1. Setup do QuickBooks Developer Portal

### Criar o app OAuth 2.0

1. Acesse https://developer.intuit.com e faça login com sua conta Intuit/QB.
2. Clique em **Dashboard → Create an app**.
3. Selecione **QuickBooks Online and Payments**.
4. Em **Redirect URIs**, adicione `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl` (para obter o token inicial).
5. Anote o **Client ID** e o **Client Secret** gerados.

### Obter o Refresh Token (primeira vez)

1. Acesse o [OAuth 2.0 Playground](https://developer.intuit.com/app/developer/playground).
2. Selecione os scopes: `com.intuit.quickbooks.accounting`.
3. Conecte com a conta da empresa BRASA no QuickBooks.
4. Copie o **Refresh Token** gerado — ele vale 100 dias e é renovado automaticamente pelo script.
5. Anote também o **Realm ID** (Company ID), visível na URL do QB Online: `app.qbo.intuit.com/app/homepage?realmId=XXXXXXXX`.

---

## 2. Configuração local

```bash
# 1. Clone o repositório
git clone <url-do-repo>
cd brasa-dashboard

# 2. Crie o arquivo .env
cp .env.example .env
# Preencha QB_CLIENT_ID, QB_CLIENT_SECRET, QB_REFRESH_TOKEN, QB_REALM_ID

# 3. Instale as dependências Python
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 4. Rode o sync manualmente
python sync.py
```

Após o sync, o arquivo `data/directors.json` será atualizado.

Para visualizar o dashboard localmente:

```bash
# Qualquer servidor HTTP simples funciona:
python -m http.server 8000
# Abra: http://localhost:8000/dashboard/?diretor=marketing
```

---

## 3. Mapeamento de diretores

Os gastos são organizados no QuickBooks por **Classes** ou **Departamentos**.
Edite a função `build_director_map()` em `sync.py` para mapear os nomes das
classes QB para os slugs usados nas URLs:

```python
manual_map = {
    "Summit Americas": "summit-americas",
    "Marketing":       "marketing",
    "Operações":       "operacoes",
    # adicione quantos diretores precisar
}
```

O slug define a URL de acesso: `dashboard/index.html?diretor=summit-americas`.

---

## 4. GitHub Actions (sync automático a cada hora)

### Adicionar os secrets no repositório

Vá em **Settings → Secrets and variables → Actions** e adicione:

| Secret                        | Valor                              |
|-------------------------------|------------------------------------|
| `QB_CLIENT_ID`                | Client ID do app QuickBooks        |
| `QB_CLIENT_SECRET`            | Client Secret                      |
| `QB_REFRESH_TOKEN`            | Refresh token OAuth 2.0            |
| `QB_REALM_ID`                 | Company ID (Realm ID)              |
| `GOOGLE_SHEETS_ID`            | (opcional) ID da planilha Google   |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | (opcional) JSON da service account |
| `ALERT_EMAIL_FROM`            | (opcional) email de origem         |
| `ALERT_EMAIL_TO`              | (opcional) email de destino        |
| `SMTP_USER` / `SMTP_PASSWORD` | (opcional) credenciais SMTP        |

O workflow `.github/workflows/sync.yml` roda automaticamente a cada hora,
atualiza `data/directors.json` e faz commit no repositório.

---

## 5. Acesso dos diretores

Cada diretor acessa o painel via URL com o parâmetro `?diretor=`:

```
https://<seu-dominio>/dashboard/?diretor=summit-americas
https://<seu-dominio>/dashboard/?diretor=marketing
https://<seu-dominio>/dashboard/?diretor=operacoes
```

Sem o parâmetro, a página exibe a lista de todas as áreas.

Para publicar o dashboard gratuitamente, use **GitHub Pages**:
- Vá em **Settings → Pages**.
- Source: `Deploy from a branch → main → / (root)`.
- A URL será: `https://<org>.github.io/<repo>/dashboard/?diretor=marketing`.

---

## 6. Alertas automáticos

O dashboard exibe automaticamente:
- **Banner amarelo** quando o gasto ultrapassa 70% do orçamento.
- **Banner vermelho** quando ultrapassa 90%.

O script `sync.py` envia um email de alerta se a sincronização falhar
(configure as variáveis SMTP no `.env`).

---

## Terminologia

| Exibido no painel  | Termo técnico (QB)     |
|--------------------|------------------------|
| Orçamento total    | Budget                 |
| Já gastou          | Actual expenses        |
| Ainda disponível   | Remaining balance      |
| Área / Time        | Class / Department     |
| Lançamento         | Transaction            |
| Fornecedor         | Vendor                 |
| Resumo financeiro  | P&L / Income Statement |
