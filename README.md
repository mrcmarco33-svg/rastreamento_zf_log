# Controle de Expedições e Rastreio — V4 Supabase

Esta versão substitui o SQLite por **Supabase PostgreSQL** e o login local por **Supabase Auth**.

## O que continua igual

- Cadastro do pedido e data de início da expedição.
- Importação diária do CSV da transportadora.
- Vínculo de uma ou várias NFs a um pedido.
- Um frete com várias NFs pode ser distribuído entre pedidos diferentes.
- Painel, Kanban e histórico.
- Contagem de dias do início da expedição até a `Data Frete`; enquanto ela estiver vazia, conta até hoje.
- Perfis Administrador, Operador e Consulta.
- Auditoria de ações importantes.

## O que mudou

- Banco centralizado no Supabase/PostgreSQL.
- Usuários autenticados pelo Supabase Auth.
- Row Level Security (RLS) no banco.
- Vários usuários podem acessar o mesmo banco pela nuvem.
- Credenciais ficam em `st.secrets`, nunca dentro do código/GitHub.
- Login continua aceitando **usuário + senha**; cada usuário também possui um e-mail interno no Supabase Auth.

---

# 1. Criar o projeto no Supabase

1. Acesse o Supabase e crie um projeto no plano Free.
2. Aguarde o projeto ficar ativo.
3. Abra **SQL Editor**.
4. Crie uma nova query.
5. Copie todo o conteúdo de `supabase_schema.sql`.
6. Execute o script.

O script cria:

- `profiles`
- `orders`
- `invoices`
- `tracking_current`
- `tracking_history`
- `imports`
- `audit_log`
- políticas RLS
- gatilho de criação de perfil

---

# 2. Obter as chaves

No Supabase, obtenha:

- **Project URL**
- **Publishable key** (`sb_publishable_...`)
- **Secret key** (`sb_secret_...`)

A Secret key é equivalente ao antigo `service_role`: ela possui acesso elevado e **nunca deve ser enviada ao GitHub nem exposta ao navegador**.

---

# 3. Configurar localmente

Dentro da pasta do projeto, copie:

```text
.streamlit/secrets.toml.example
```

para:

```text
.streamlit/secrets.toml
```

Preencha:

```toml
SUPABASE_URL = "https://SEU-PROJETO.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_..."
SUPABASE_SECRET_KEY = "sb_secret_..."
```

`secrets.toml` já está no `.gitignore` e não deve ser commitado.

---

# 4. Criar o primeiro administrador

Depois de executar `supabase_schema.sql`, rode no terminal:

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
python setup_admin.py
```

Informe:

- nome completo;
- nome de usuário;
- e-mail;
- senha.

Esse usuário será criado no Supabase Auth e promovido para `admin` na tabela `profiles`.

Depois disso, novos usuários poderão ser criados diretamente na tela **Usuários** do app.

---

# 5. Migrar o banco atual do SQLite (opcional, recomendado)

Se você quer preservar os pedidos, NFs, rastreios e históricos já existentes na V3, mantenha uma cópia do seu `rastreio.db` e execute:

```powershell
python migrate_sqlite_to_supabase.py "C:\caminho\para\rastreio.db"
```

A migração leva para o Supabase:

- pedidos;
- NFs e vínculos;
- rastreio atual;
- histórico de rastreio;
- importações;
- auditoria antiga.

**Usuários e senhas da V3 não são migrados.** Como as senhas antigas estavam em hash bcrypt, elas não podem ser convertidas para Supabase Auth. Recrie os usuários pela V4.

---

# 6. Testar localmente

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Entre usando o usuário ou e-mail do administrador criado no passo anterior.

---

# 7. Publicar no GitHub

Envie para o repositório:

```text
app.py
requirements.txt
supabase_schema.sql
setup_admin.py
migrate_sqlite_to_supabase.py
README.md
.gitignore
.streamlit/config.toml
.streamlit/secrets.toml.example
```

**Não envie:**

```text
.streamlit/secrets.toml
rastreio.db
.venv/
.env
```

---

# 8. Publicar no Streamlit Community Cloud

1. Entre no Streamlit Community Cloud usando sua conta GitHub.
2. Clique em **Create app**.
3. Selecione o repositório e branch.
4. Main file: `app.py`.
5. Abra **Advanced settings / Secrets**.
6. Cole:

```toml
SUPABASE_URL = "https://SEU-PROJETO.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_..."
SUPABASE_SECRET_KEY = "sb_secret_..."
```

7. Faça o deploy.

O Streamlit fornecerá um endereço `*.streamlit.app` que pode ser compartilhado com sua equipe.

---

# Segurança

- O Publishable key pode operar apenas dentro do que as políticas RLS permitem.
- O Secret key ignora RLS e fica restrito ao servidor Streamlit via Secrets.
- A senha é gerenciada pelo Supabase Auth e não fica na tabela `profiles`.
- Perfil `viewer`: apenas leitura.
- Perfil `operator`: leitura + operações de expedição/rastreio.
- Perfil `admin`: funções administrativas e usuários.

## Observação sobre o plano gratuito

Projetos gratuitos do Supabase podem ser pausados após um período de baixa atividade. Um projeto pausado pode ser retomado pelo Dashboard. Para um app usado regularmente pela equipe, o próprio uso tende a gerar atividade no banco.
