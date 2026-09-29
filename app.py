import os
import re
import hashlib
from io import BytesIO
from datetime import datetime, date, timezone

import pandas as pd
import streamlit as st
from supabase import create_client

st.set_page_config(
    page_title="Controle de Expedições",
    page_icon="🚚",
    layout="wide",
)

ROLE_LABELS = {
    "admin": "Administrador",
    "operator": "Operador",
    "viewer": "Consulta",
}

REQUIRED_COLS = {
    "Notas Fiscais",
    "Última ocorrência",
    "Data última ocorrência",
}

# -----------------------------
# Configuração Supabase
# -----------------------------
def _secret(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)

SUPABASE_URL = _secret("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = _secret("SUPABASE_PUBLISHABLE_KEY") or _secret("SUPABASE_ANON_KEY")
SUPABASE_SECRET_KEY = _secret("SUPABASE_SECRET_KEY") or _secret("SUPABASE_SERVICE_ROLE_KEY")


def ensure_config():
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_PUBLISHABLE_KEY:
        missing.append("SUPABASE_PUBLISHABLE_KEY")
    if not SUPABASE_SECRET_KEY:
        missing.append("SUPABASE_SECRET_KEY")
    if missing:
        st.error(
            "Configuração do Supabase incompleta. Defina: " + ", ".join(missing)
        )
        st.code(
            '[supabase]\n# opcional: use seção se adaptar o código\n\n'
            'SUPABASE_URL = "https://SEU-PROJETO.supabase.co"\n'
            'SUPABASE_PUBLISHABLE_KEY = "sb_publishable_..."\n'
            'SUPABASE_SECRET_KEY = "sb_secret_..."',
            language="toml",
        )
        st.stop()


ensure_config()


def anon_client():
    return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


@st.cache_resource
def service_client():
    # A service role fica apenas no servidor/Secrets e nunca é enviada ao navegador.
    return create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


def authenticated_client():
    access = st.session_state.get("access_token")
    refresh = st.session_state.get("refresh_token")
    if not access or not refresh:
        return None
    client = anon_client()
    try:
        response = client.auth.set_session(access, refresh)
        session = getattr(response, "session", None)
        if session:
            st.session_state["access_token"] = session.access_token
            st.session_state["refresh_token"] = session.refresh_token
        return client
    except Exception:
        clear_session()
        return None


def clear_session():
    for key in ["access_token", "refresh_token", "auth_user"]:
        st.session_state.pop(key, None)


# -----------------------------
# Helpers Supabase / paginação
# -----------------------------
def fetch_all(client, table, columns="*", order=None, desc=False, filters=None, page_size=1000):
    rows = []
    start = 0
    while True:
        q = client.table(table).select(columns)
        if filters:
            for kind, col, value in filters:
                if kind == "eq":
                    q = q.eq(col, value)
                elif kind == "is":
                    q = q.is_(col, value)
                elif kind == "in":
                    q = q.in_(col, value)
        if order:
            q = q.order(order, desc=desc)
        response = q.range(start, start + page_size - 1).execute()
        batch = response.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def chunks(items, size=200):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Autenticação / perfis
# -----------------------------
def lookup_email(login_value):
    login_value = login_value.strip()
    if "@" in login_value:
        return login_value.lower()
    # Busca de username é feita com service role no servidor.
    response = (
        service_client()
        .table("profiles")
        .select("email")
        .eq("username", login_value)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    return response.data[0].get("email")


def load_profile(user_id, client=None):
    client = client or authenticated_client()
    if client is None:
        return None
    response = (
        client.table("profiles")
        .select("id,username,email,full_name,role,is_active,created_at,last_login")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def audit(action, details="", username=None):
    client = authenticated_client()
    user = st.session_state.get("auth_user", {})
    if client is None:
        return
    payload = {
        "user_id": user.get("id"),
        "username": username or user.get("username") or user.get("email"),
        "action": action,
        "details": details,
    }
    try:
        client.table("audit_log").insert(payload).execute()
    except Exception:
        # Auditoria não deve derrubar o fluxo operacional principal.
        pass


def login_screen():
    st.title("🚚 Controle de Expedições e Rastreio")
    st.caption("Acesso restrito · Supabase")
    left, center, right = st.columns([1, 1.2, 1])
    with center:
        with st.form("login_form"):
            login_value = st.text_input("Usuário ou e-mail")
            password = st.text_input("Senha", type="password")
            submitted = st.form_submit_button("Entrar", type="primary", use_container_width=True)

        if submitted:
            email = lookup_email(login_value)
            if not email:
                st.error("Usuário ou senha inválidos.")
                st.stop()
            try:
                client = anon_client()
                result = client.auth.sign_in_with_password({"email": email, "password": password})
                session = getattr(result, "session", None)
                user = getattr(result, "user", None)
                if not session or not user:
                    raise ValueError("Sessão não criada")

                st.session_state["access_token"] = session.access_token
                st.session_state["refresh_token"] = session.refresh_token

                auth_client = authenticated_client()
                profile = load_profile(str(user.id), auth_client)
                if not profile or not profile.get("is_active", False):
                    clear_session()
                    st.error("Usuário inativo ou sem perfil de acesso.")
                    st.stop()

                st.session_state["auth_user"] = profile
                # Atualiza last_login via service role para não ampliar permissões do usuário sobre o perfil.
                service_client().table("profiles").update({"last_login": utc_now_iso()}).eq("id", str(user.id)).execute()
                audit("LOGIN_SUCCESS", "Login realizado.")
                st.rerun()
            except Exception:
                clear_session()
                st.error("Usuário ou senha inválidos.")
    st.stop()


def ensure_logged_in():
    if not st.session_state.get("access_token"):
        login_screen()

    client = authenticated_client()
    if client is None:
        login_screen()

    try:
        auth_user = client.auth.get_user()
        user = getattr(auth_user, "user", None)
        if not user:
            raise ValueError("Usuário inválido")
        profile = load_profile(str(user.id), client)
        if not profile or not profile.get("is_active", False):
            clear_session()
            login_screen()
        st.session_state["auth_user"] = profile
        return client
    except Exception:
        clear_session()
        login_screen()


def require_role(*roles):
    user = st.session_state.get("auth_user") or {}
    if user.get("role") not in roles:
        st.error("Seu usuário não possui permissão para acessar esta função.")
        st.stop()


DB = ensure_logged_in()
CURRENT_USER = st.session_state["auth_user"]


# -----------------------------
# Utilitários CSV
# -----------------------------
def clean_scalar(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def split_nfs(value):
    s = clean_scalar(value)
    if not s:
        return []
    parts = re.split(r"[,;/\n]+", s)
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if re.fullmatch(r"\d+\.0", p):
            p = p[:-2]
        result.append(p)
    return result


def parse_br_datetime(value):
    s = clean_scalar(value)
    if not s:
        return None
    dt = pd.to_datetime(s, dayfirst=True, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.to_pydatetime()


def iso_or_none(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def read_tracking_csv(uploaded):
    raw = uploaded.getvalue()
    attempts = [
        ("utf-8-sig", ";"),
        ("utf-8", ";"),
        ("latin1", ";"),
        ("cp1252", ";"),
        ("utf-8-sig", ","),
        ("latin1", ","),
    ]
    for enc, sep in attempts:
        try:
            frame = pd.read_csv(BytesIO(raw), encoding=enc, sep=sep)
            if len(frame.columns) > 1 and "Notas Fiscais" in frame.columns:
                return frame, enc, sep
        except Exception:
            pass
    raise ValueError("Não foi possível reconhecer o CSV da transportadora.")


def value_from(row, col):
    return clean_scalar(row[col]) if col in row.index else ""


def make_hash(nf, row):
    fields = [
        nf,
        value_from(row, "Data Frete"),
        value_from(row, "Tipo Frete"),
        value_from(row, "N° Minuta"),
        value_from(row, "N° CT-e"),
        value_from(row, "Série"),
        value_from(row, "Emissão CT-e"),
        value_from(row, "Prazo Entrega"),
        value_from(row, "Última ocorrência"),
        value_from(row, "Data última ocorrência"),
    ]
    return hashlib.sha256("||".join(fields).encode("utf-8")).hexdigest()


def tracking_payload(nf, row, filename):
    occ_dt = parse_br_datetime(value_from(row, "Data última ocorrência"))
    return {
        "nf_number": nf,
        "data_frete": value_from(row, "Data Frete") or None,
        "tipo_frete": value_from(row, "Tipo Frete") or None,
        "minuta": value_from(row, "N° Minuta") or None,
        "cte": value_from(row, "N° CT-e") or None,
        "serie": value_from(row, "Série") or None,
        "emissao_cte": value_from(row, "Emissão CT-e") or None,
        "destinatario": value_from(row, "Destinatário") or None,
        "cidade_destinatario": value_from(row, "Cidade Destinatário") or None,
        "uf_destinatario": value_from(row, "UF Destinatário") or None,
        "volumes": value_from(row, "Volumes") or None,
        "peso_real": value_from(row, "Peso real") or None,
        "valor_nf": value_from(row, "Valor NF") or None,
        "total_frete": value_from(row, "Total Frete") or None,
        "prazo_entrega": value_from(row, "Prazo Entrega") or None,
        "ultima_ocorrencia": value_from(row, "Última ocorrência") or None,
        "data_ultima_ocorrencia": value_from(row, "Data última ocorrência") or None,
        "occurrence_ts": iso_or_none(occ_dt),
        "source_filename": filename,
        "updated_at": utc_now_iso(),
    }


def import_csv(frame, filename):
    missing = REQUIRED_COLS - set(frame.columns)
    if missing:
        raise ValueError("Colunas obrigatórias ausentes: " + ", ".join(sorted(missing)))

    now = utc_now_iso()
    rows_read = len(frame)
    nf_mentions = 0

    invoice_numbers = []
    history_by_hash = {}
    newest_from_file = {}

    for _, row in frame.iterrows():
        nfs = split_nfs(row.get("Notas Fiscais", ""))
        for nf in nfs:
            nf_mentions += 1
            invoice_numbers.append(nf)
            p = tracking_payload(nf, row, filename)
            h = make_hash(nf, row)
            history_payload = dict(p)
            history_payload.pop("updated_at", None)
            history_by_hash[h] = {
                **history_payload,
                "row_hash": h,
                "imported_at": now,
            }

            existing = newest_from_file.get(nf)
            old_ts = (existing or {}).get("occurrence_ts") or ""
            new_ts = p.get("occurrence_ts") or ""
            if existing is None or (new_ts and new_ts >= old_ts) or (not old_ts and not new_ts):
                newest_from_file[nf] = p

    unique_nfs = sorted(set(invoice_numbers))

    # Descobre NFs já existentes para contabilizar novas inclusões.
    existing_nfs = set()
    for part in chunks(unique_nfs, 200):
        if not part:
            continue
        res = DB.table("invoices").select("nf_number").in_("nf_number", part).execute()
        existing_nfs.update(r["nf_number"] for r in (res.data or []))

    invoice_payloads = [
        {"nf_number": nf, "first_seen_at": now}
        for nf in unique_nfs
    ]
    for part in chunks(invoice_payloads, 200):
        if part:
            DB.table("invoices").upsert(
                part,
                on_conflict="nf_number",
                ignore_duplicates=True,
            ).execute()

    # Histórico: primary key row_hash evita duplicação mesmo reimportando o arquivo.
    history_rows = list(history_by_hash.values())
    existing_hashes = set()
    hashes = list(history_by_hash.keys())
    for part in chunks(hashes, 150):
        if not part:
            continue
        res = DB.table("tracking_history").select("row_hash").in_("row_hash", part).execute()
        existing_hashes.update(r["row_hash"] for r in (res.data or []))

    for part in chunks(history_rows, 150):
        if part:
            DB.table("tracking_history").upsert(
                part,
                on_conflict="row_hash",
                ignore_duplicates=True,
            ).execute()

    # Atualiza somente se a nova ocorrência for mais recente.
    current_existing = {}
    for part in chunks(unique_nfs, 200):
        if not part:
            continue
        res = DB.table("tracking_current").select("nf_number,occurrence_ts").in_("nf_number", part).execute()
        for r in res.data or []:
            current_existing[r["nf_number"]] = r.get("occurrence_ts")

    current_updates = []
    for nf, p in newest_from_file.items():
        old_ts = current_existing.get(nf) or ""
        new_ts = p.get("occurrence_ts") or ""
        should_update = nf not in current_existing
        if new_ts and (not old_ts or new_ts >= old_ts):
            should_update = True
        elif not new_ts and not old_ts:
            should_update = True
        if should_update:
            current_updates.append(p)

    for part in chunks(current_updates, 150):
        if part:
            DB.table("tracking_current").upsert(part, on_conflict="nf_number").execute()

    new_history = len(set(hashes) - existing_hashes)
    new_invoices = len(set(unique_nfs) - existing_nfs)

    DB.table("imports").insert({
        "filename": filename,
        "imported_at": now,
        "rows_read": rows_read,
        "nf_mentions": nf_mentions,
        "new_history_records": new_history,
        "imported_by": CURRENT_USER.get("id"),
    }).execute()

    return {
        "linhas": rows_read,
        "nfs_lidas": nf_mentions,
        "novas_nfs": new_invoices,
        "novos_registros_historico": new_history,
        "rastreamentos_atualizados": len(current_updates),
    }


# -----------------------------
# Dados / relatórios
# -----------------------------
def classify_status(occ):
    if occ is None:
        return "Sem rastreio"
    try:
        if pd.isna(occ):
            return "Sem rastreio"
    except Exception:
        pass
    s = str(occ).strip().lower()
    if not s:
        return "Sem rastreio"
    if (
        s.startswith("001")
        or s.startswith("138")
        or "entrega realizada" in s
        or "comprovante digitalizado" in s
    ):
        return "Entregue"
    if any(x in s for x in [
        "devolu", "retida", "fiscalização", "analise fiscal", "análise fiscal",
        "mudou de endereço", "sinistro", "avaria"
    ]):
        return "Atenção"
    if "documento fiscal emitido" in s:
        return "Preparação"
    return "Em trânsito"


def df_from(rows, columns=None):
    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=columns or [])


def load_orders():
    orders = fetch_all(DB, "orders", "id,order_number,expedition_start,created_at", order="id", desc=True)
    invoices = fetch_all(DB, "invoices", "nf_number,order_id")
    count_map = {}
    for inv in invoices:
        oid = inv.get("order_id")
        if oid is not None:
            count_map[oid] = count_map.get(oid, 0) + 1
    for order in orders:
        order["qtd_nfs"] = count_map.get(order["id"], 0)
    return df_from(orders, ["id", "order_number", "expedition_start", "created_at", "qtd_nfs"])


def load_unlinked_invoices():
    invoices = fetch_all(DB, "invoices", "nf_number,order_id")
    unlinked = [i for i in invoices if i.get("order_id") is None]
    if not unlinked:
        return pd.DataFrame(columns=["nf_number", "destinatario", "cidade_destinatario", "uf_destinatario", "ultima_ocorrencia", "data_ultima_ocorrencia", "cte", "minuta"])

    nf_set = {i["nf_number"] for i in unlinked}
    tracking = fetch_all(DB, "tracking_current", "nf_number,destinatario,cidade_destinatario,uf_destinatario,ultima_ocorrencia,data_ultima_ocorrencia,cte,minuta,occurrence_ts")
    track_map = {t["nf_number"]: t for t in tracking if t.get("nf_number") in nf_set}
    rows = []
    for inv in unlinked:
        t = track_map.get(inv["nf_number"], {})
        rows.append({"nf_number": inv["nf_number"], **{k: t.get(k) for k in ["destinatario", "cidade_destinatario", "uf_destinatario", "ultima_ocorrencia", "data_ultima_ocorrencia", "cte", "minuta", "occurrence_ts"]}})
    return pd.DataFrame(rows).sort_values(by=["occurrence_ts", "nf_number"], ascending=[False, True], na_position="last")


def load_dashboard():
    orders = fetch_all(DB, "orders", "id,order_number,expedition_start", order="id", desc=True)
    invoices = fetch_all(DB, "invoices", "nf_number,order_id")
    tracking = fetch_all(DB, "tracking_current", "nf_number,data_frete,tipo_frete,cte,minuta,destinatario,cidade_destinatario,uf_destinatario,prazo_entrega,ultima_ocorrencia,data_ultima_ocorrencia,occurrence_ts")

    inv_by_order = {}
    for inv in invoices:
        inv_by_order.setdefault(inv.get("order_id"), []).append(inv)
    track_map = {t["nf_number"]: t for t in tracking}

    rows = []
    for o in orders:
        linked = inv_by_order.get(o["id"], [])
        if not linked:
            linked = [{"nf_number": None}]
        for inv in linked:
            nf = inv.get("nf_number")
            t = track_map.get(nf, {}) if nf else {}
            rows.append({
                "order_id": o["id"],
                "pedido": o["order_number"],
                "inicio_expedicao": o["expedition_start"],
                "nota_fiscal": nf,
                "data_frete": t.get("data_frete"),
                "tipo_frete": t.get("tipo_frete"),
                "cte": t.get("cte"),
                "minuta": t.get("minuta"),
                "destinatario": t.get("destinatario"),
                "cidade_destinatario": t.get("cidade_destinatario"),
                "uf_destinatario": t.get("uf_destinatario"),
                "prazo_entrega": t.get("prazo_entrega"),
                "ultima_ocorrencia": t.get("ultima_ocorrencia"),
                "data_ultima_ocorrencia": t.get("data_ultima_ocorrencia"),
                "occurrence_ts": t.get("occurrence_ts"),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["status"] = df["ultima_ocorrencia"].apply(classify_status)
    start = pd.to_datetime(df["inicio_expedicao"], errors="coerce")
    data_frete = pd.to_datetime(df["data_frete"], dayfirst=True, errors="coerce")
    today = pd.Timestamp(date.today())
    end_date = data_frete.fillna(today)
    df["dias_em_expedicao"] = (end_date.dt.normalize() - start.dt.normalize()).dt.days
    df["dias_em_expedicao"] = df["dias_em_expedicao"].clip(lower=0)
    df["contagem_dias_status"] = data_frete.apply(
        lambda x: "Finalizada na Data Frete" if pd.notna(x) else "Contando até hoje"
    )

    prazo = pd.to_datetime(df["prazo_entrega"], dayfirst=True, errors="coerce")
    df["prazo_atrasado"] = (
        prazo.notna()
        & (prazo.dt.normalize() < today)
        & (df["status"] != "Entregue")
    )
    return df


def load_history(nf):
    response = (
        DB.table("tracking_history")
        .select("data_ultima_ocorrencia,ultima_ocorrencia,cte,minuta,prazo_entrega,source_filename,occurrence_ts,imported_at")
        .eq("nf_number", nf)
        .order("occurrence_ts", desc=True, nullsfirst=False)
        .limit(1000)
        .execute()
    )
    hist = df_from(response.data or [])
    if hist.empty:
        return hist
    return hist.rename(columns={
        "data_ultima_ocorrencia": "Data ocorrência",
        "ultima_ocorrencia": "Ocorrência",
        "cte": "CT-e",
        "minuta": "Minuta",
        "prazo_entrega": "Prazo entrega",
        "source_filename": "Arquivo importado",
        "imported_at": "Importado em",
    }).drop(columns=["occurrence_ts"], errors="ignore")


def load_linked():
    orders = fetch_all(DB, "orders", "id,order_number")
    invoices = fetch_all(DB, "invoices", "nf_number,order_id")
    tracking = fetch_all(DB, "tracking_current", "nf_number,destinatario,ultima_ocorrencia")
    order_map = {o["id"]: o["order_number"] for o in orders}
    track_map = {t["nf_number"]: t for t in tracking}
    rows = []
    for inv in invoices:
        if inv.get("order_id") is None:
            continue
        t = track_map.get(inv["nf_number"], {})
        rows.append({
            "nf_number": inv["nf_number"],
            "order_id": inv["order_id"],
            "order_number": order_map.get(inv["order_id"], ""),
            "destinatario": t.get("destinatario"),
            "ultima_ocorrencia": t.get("ultima_ocorrencia"),
        })
    return pd.DataFrame(rows)


# -----------------------------
# Layout global
# -----------------------------
st.sidebar.success(f"{CURRENT_USER.get('full_name') or CURRENT_USER.get('username')}")
st.sidebar.caption(f"{ROLE_LABELS.get(CURRENT_USER.get('role'), CURRENT_USER.get('role'))} · @{CURRENT_USER.get('username')}")
if st.sidebar.button("Sair", use_container_width=True):
    audit("LOGOUT", "Logout realizado.")
    try:
        DB.auth.sign_out()
    except Exception:
        pass
    clear_session()
    st.rerun()

menu = ["📊 Painel", "🗂️ Kanban", "🕘 Histórico"]
if CURRENT_USER["role"] in ("admin", "operator"):
    menu += ["➕ Novo pedido", "🔗 Vincular NFs", "📥 Importar rastreio", "⚙️ Gerenciar vínculos"]
if CURRENT_USER["role"] == "admin":
    menu += ["👥 Usuários"]

page = st.sidebar.radio("Menu", menu)

st.title("🚚 Controle de Expedições e Rastreio")


# -----------------------------
# Páginas
# -----------------------------
if page == "📊 Painel":
    df = load_dashboard()
    orders = load_orders()
    invoices_all = fetch_all(DB, "invoices", "nf_number,order_id")
    total_orders = len(orders)
    total_nfs = len(invoices_all)
    linked_nfs = sum(1 for i in invoices_all if i.get("order_id") is not None)
    unlinked_nfs = total_nfs - linked_nfs

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pedidos cadastrados", total_orders)
    c2.metric("NFs conhecidas", total_nfs)
    c3.metric("NFs vinculadas", linked_nfs)
    c4.metric("NFs aguardando vínculo", unlinked_nfs)

    if df.empty:
        st.info("Nenhum pedido cadastrado.")
    else:
        f1, f2, f3 = st.columns(3)
        search = f1.text_input("Buscar pedido / NF / destinatário")
        status_options = sorted(df["status"].dropna().unique().tolist())
        status_filter = f2.multiselect("Status", status_options)
        only_late = f3.checkbox("Somente prazo vencido")

        view = df.copy()
        if search:
            mask = (
                view["pedido"].fillna("").astype(str).str.contains(search, case=False, regex=False)
                | view["nota_fiscal"].fillna("").astype(str).str.contains(search, case=False, regex=False)
                | view["destinatario"].fillna("").astype(str).str.contains(search, case=False, regex=False)
            )
            view = view[mask]
        if status_filter:
            view = view[view["status"].isin(status_filter)]
        if only_late:
            view = view[view["prazo_atrasado"]]

        display = view[[
            "pedido", "inicio_expedicao", "data_frete", "dias_em_expedicao", "contagem_dias_status",
            "nota_fiscal", "status", "ultima_ocorrencia", "data_ultima_ocorrencia", "prazo_entrega",
            "destinatario", "cidade_destinatario", "uf_destinatario", "cte", "minuta", "prazo_atrasado"
        ]].rename(columns={
            "pedido": "Pedido",
            "inicio_expedicao": "Início expedição",
            "data_frete": "Data Frete",
            "dias_em_expedicao": "Dias até o frete",
            "contagem_dias_status": "Contagem",
            "nota_fiscal": "NF",
            "status": "Status",
            "ultima_ocorrencia": "Última ocorrência",
            "data_ultima_ocorrencia": "Data ocorrência",
            "prazo_entrega": "Prazo entrega",
            "destinatario": "Destinatário",
            "cidade_destinatario": "Cidade",
            "uf_destinatario": "UF",
            "cte": "CT-e",
            "minuta": "Minuta",
            "prazo_atrasado": "Prazo vencido",
        })
        st.dataframe(display, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Exportar painel em CSV",
            data=display.to_csv(index=False, sep=";", encoding="utf-8-sig"),
            file_name=f"painel_rastreio_{date.today().isoformat()}.csv",
            mime="text/csv",
        )

elif page == "🗂️ Kanban":
    df = load_dashboard()
    if df.empty:
        st.info("Nenhum pedido cadastrado.")
    else:
        def kanban_bucket(r):
            if r["status"] == "Entregue":
                return "Entregue"
            if r["status"] == "Atenção":
                return "Atenção"
            if bool(r["prazo_atrasado"]):
                return "Atrasado"
            if pd.isna(pd.to_datetime(r.get("data_frete"), dayfirst=True, errors="coerce")):
                return "Aguardando frete"
            return "Em trânsito"

        df = df.copy()
        df["kanban"] = df.apply(kanban_bucket, axis=1)
        columns = ["Aguardando frete", "Em trânsito", "Atrasado", "Atenção", "Entregue"]
        cols = st.columns(len(columns))
        for col, bucket in zip(cols, columns):
            subset = df[df["kanban"] == bucket]
            with col:
                st.subheader(f"{bucket} · {len(subset)}")
                if subset.empty:
                    st.caption("Nenhum item")
                for _, r in subset.iterrows():
                    pedido = clean_scalar(r.get("pedido")) or "—"
                    nf = clean_scalar(r.get("nota_fiscal")) or "Sem NF"
                    destino = clean_scalar(r.get("destinatario")) or "Sem destinatário"
                    cidade = clean_scalar(r.get("cidade_destinatario"))
                    uf = clean_scalar(r.get("uf_destinatario"))
                    local = " / ".join(x for x in [cidade, uf] if x)
                    dias = r.get("dias_em_expedicao")
                    dias_txt = "—" if pd.isna(dias) else str(int(dias))
                    inicio = clean_scalar(r.get("inicio_expedicao")) or "—"
                    data_frete = clean_scalar(r.get("data_frete")) or "Aguardando"
                    prazo = clean_scalar(r.get("prazo_entrega")) or "—"
                    ocorrencia = clean_scalar(r.get("ultima_ocorrencia")) or "Sem rastreio"
                    with st.container(border=True):
                        st.markdown(f"**Pedido {pedido}**")
                        st.caption(f"NF {nf}")
                        st.write(destino)
                        if local:
                            st.caption(local)
                        st.metric("Dias até o frete", dias_txt)
                        st.caption(f"Início: {inicio} · Data Frete: {data_frete}")
                        st.caption(f"Prazo entrega: {prazo}")
                        if bool(r.get("prazo_atrasado")):
                            st.error("Prazo de entrega vencido")
                        st.write(ocorrencia)

elif page == "➕ Novo pedido":
    require_role("admin", "operator")
    st.subheader("Cadastrar início da expedição")
    st.write("O pedido é criado antes do vínculo da nota fiscal.")
    with st.form("new_order", clear_on_submit=True):
        order_number = st.text_input("Número do pedido *", placeholder="Ex.: PV123456")
        expedition_start = st.date_input("Data de início da expedição *", value=date.today())
        submitted = st.form_submit_button("Cadastrar pedido", type="primary")
    if submitted:
        order_number = order_number.strip()
        if not order_number:
            st.error("Informe o número do pedido.")
        else:
            try:
                DB.table("orders").insert({
                    "order_number": order_number,
                    "expedition_start": expedition_start.isoformat(),
                    "created_by": CURRENT_USER.get("id"),
                }).execute()
                audit("ORDER_CREATED", f"Pedido {order_number}; início {expedition_start.isoformat()}.")
                st.success(f"Pedido {order_number} cadastrado com sucesso.")
            except Exception as exc:
                if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                    st.error("Esse número de pedido já está cadastrado.")
                else:
                    st.error(f"Não foi possível cadastrar o pedido: {exc}")

    orders = load_orders()
    if not orders.empty:
        st.divider()
        st.subheader("Pedidos recentes")
        st.dataframe(
            orders[["order_number", "expedition_start", "qtd_nfs"]].rename(columns={
                "order_number": "Pedido",
                "expedition_start": "Início expedição",
                "qtd_nfs": "Qtd. NFs",
            }),
            use_container_width=True,
            hide_index=True,
        )

elif page == "🔗 Vincular NFs":
    require_role("admin", "operator")
    st.subheader("Vincular notas fiscais a um pedido")
    orders = load_orders()
    nfs = load_unlinked_invoices()
    if orders.empty:
        st.warning("Cadastre ao menos um pedido primeiro.")
    elif nfs.empty:
        st.info("Não há NFs livres. Importe um CSV de rastreio ou verifique os vínculos existentes.")
    else:
        order_map = {
            f"{r['order_number']} — início {r['expedition_start']} — {r['qtd_nfs']} NF(s)": int(r["id"])
            for _, r in orders.iterrows()
        }
        selected_order_label = st.selectbox("Pedido", list(order_map.keys()))
        selected_order_id = order_map[selected_order_label]
        st.dataframe(
            nfs.rename(columns={
                "nf_number": "NF", "destinatario": "Destinatário", "cidade_destinatario": "Cidade",
                "uf_destinatario": "UF", "ultima_ocorrencia": "Última ocorrência",
                "data_ultima_ocorrencia": "Data ocorrência", "cte": "CT-e", "minuta": "Minuta",
            }).drop(columns=["occurrence_ts"], errors="ignore"),
            use_container_width=True,
            hide_index=True,
        )
        selected_nfs = st.multiselect("Selecione uma ou mais NFs", nfs["nf_number"].astype(str).tolist())
        if st.button("Vincular NFs selecionadas", type="primary", disabled=not selected_nfs):
            for nf in selected_nfs:
                DB.table("invoices").update({"order_id": selected_order_id}).eq("nf_number", nf).execute()
            audit("INVOICES_LINKED", f"Pedido ID {selected_order_id}; NFs: {', '.join(selected_nfs)}")
            st.success(f"{len(selected_nfs)} NF(s) vinculada(s) ao pedido.")
            st.rerun()

elif page == "📥 Importar rastreio":
    require_role("admin", "operator")
    st.subheader("Importação diária da planilha da transportadora")
    st.write("Reimporte o arquivo completo diariamente. O histórico não é duplicado e os vínculos de NF são preservados.")
    uploaded = st.file_uploader("Selecione o CSV exportado pela transportadora", type=["csv"])
    if uploaded is not None:
        try:
            preview_df, enc, sep = read_tracking_csv(uploaded)
            missing = REQUIRED_COLS - set(preview_df.columns)
            if missing:
                st.error("O arquivo não contém as colunas esperadas: " + ", ".join(sorted(missing)))
            else:
                nf_count = preview_df["Notas Fiscais"].apply(split_nfs).apply(len).sum()
                multi_count = (preview_df["Notas Fiscais"].apply(split_nfs).apply(len) > 1).sum()
                c1, c2, c3 = st.columns(3)
                c1.metric("Linhas no arquivo", len(preview_df))
                c2.metric("NFs encontradas", int(nf_count))
                c3.metric("Linhas com várias NFs", int(multi_count))
                st.caption(f"Formato reconhecido: codificação {enc}, separador {repr(sep)}")
                cols = [c for c in ["Data Frete", "Notas Fiscais", "Destinatário", "Prazo Entrega", "Última ocorrência", "Data última ocorrência"] if c in preview_df.columns]
                st.dataframe(preview_df[cols].head(30), use_container_width=True, hide_index=True)
                if st.button("Importar e atualizar rastreios", type="primary"):
                    with st.spinner("Atualizando Supabase..."):
                        result = import_csv(preview_df, uploaded.name)
                    audit("TRACKING_IMPORTED", f"Arquivo {uploaded.name}; {result['linhas']} linhas; {result['nfs_lidas']} NFs.")
                    st.success("Importação concluída.")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Linhas lidas", result["linhas"])
                    c2.metric("NFs lidas", result["nfs_lidas"])
                    c3.metric("Novas NFs", result["novas_nfs"])
                    c4.metric("Novos históricos", result["novos_registros_historico"])
                    st.info(f"{result['rastreamentos_atualizados']} rastreamento(s) atual(is) inserido(s) ou atualizado(s).")
        except Exception as exc:
            st.error(f"Erro ao importar: {exc}")

    st.divider()
    st.subheader("Últimas importações")
    imports_rows = fetch_all(DB, "imports", "filename,imported_at,rows_read,nf_mentions,new_history_records", order="id", desc=True)
    imports_df = df_from(imports_rows[:20])
    if imports_df.empty:
        st.caption("Nenhuma importação realizada.")
    else:
        st.dataframe(imports_df.rename(columns={
            "filename": "Arquivo", "imported_at": "Importado em", "rows_read": "Linhas",
            "nf_mentions": "NFs lidas", "new_history_records": "Novos históricos",
        }), use_container_width=True, hide_index=True)

elif page == "🕘 Histórico":
    st.subheader("Histórico de rastreio por nota fiscal")
    invoices = fetch_all(DB, "invoices", "nf_number", order="nf_number")
    all_nfs = [r["nf_number"] for r in invoices]
    if not all_nfs:
        st.info("Importe um CSV para consultar o histórico.")
    else:
        nf = st.selectbox("Nota fiscal", all_nfs)
        hist = load_history(nf)
        if hist.empty:
            st.info("Sem histórico para esta NF.")
        else:
            st.dataframe(hist, use_container_width=True, hide_index=True)

elif page == "⚙️ Gerenciar vínculos":
    require_role("admin", "operator")
    st.subheader("Gerenciar NFs já vinculadas")
    linked = load_linked()
    if linked.empty:
        st.info("Ainda não há NFs vinculadas.")
    else:
        st.dataframe(
            linked[["order_number", "nf_number", "destinatario", "ultima_ocorrencia"]].rename(columns={
                "order_number": "Pedido", "nf_number": "NF", "destinatario": "Destinatário", "ultima_ocorrencia": "Última ocorrência",
            }),
            use_container_width=True,
            hide_index=True,
        )
        labels = [f"Pedido {r['order_number']} — NF {r['nf_number']}" for _, r in linked.iterrows()]
        selected = st.selectbox("Selecione um vínculo para alterar", labels)
        idx = labels.index(selected)
        nf = str(linked.iloc[idx]["nf_number"])
        orders = load_orders()
        target_labels = ["— Deixar NF sem pedido —"] + orders["order_number"].astype(str).tolist()
        target = st.selectbox("Novo destino da NF", target_labels)
        if st.button("Salvar alteração"):
            if target == "— Deixar NF sem pedido —":
                DB.table("invoices").update({"order_id": None}).eq("nf_number", nf).execute()
                audit("INVOICE_UNLINKED", f"NF {nf} desvinculada.")
                st.success(f"NF {nf} desvinculada.")
            else:
                order_id = int(orders.loc[orders["order_number"].astype(str) == target, "id"].iloc[0])
                DB.table("invoices").update({"order_id": order_id}).eq("nf_number", nf).execute()
                audit("INVOICE_MOVED", f"NF {nf} movida para pedido {target}.")
                st.success(f"NF {nf} movida para o pedido {target}.")
            st.rerun()

elif page == "👥 Usuários":
    require_role("admin")
    st.subheader("Usuários e permissões")
    tab1, tab2 = st.tabs(["Usuários", "Auditoria"])

    with tab1:
        with st.expander("Criar novo usuário", expanded=False):
            with st.form("create_user_form", clear_on_submit=True):
                new_full_name = st.text_input("Nome completo")
                new_username = st.text_input("Usuário")
                new_email = st.text_input("E-mail")
                new_password = st.text_input("Senha inicial", type="password")
                new_role_label = st.selectbox("Perfil", ["Operador", "Consulta", "Administrador"])
                create_btn = st.form_submit_button("Criar usuário", type="primary")
            if create_btn:
                role_map = {"Administrador": "admin", "Operador": "operator", "Consulta": "viewer"}
                if len(new_username.strip()) < 3:
                    st.error("O usuário deve ter pelo menos 3 caracteres.")
                elif len(new_password) < 8:
                    st.error("A senha deve ter pelo menos 8 caracteres.")
                elif "@" not in new_email:
                    st.error("Informe um e-mail válido.")
                else:
                    try:
                        result = service_client().auth.admin.create_user({
                            "email": new_email.strip().lower(),
                            "password": new_password,
                            "email_confirm": True,
                            "user_metadata": {
                                "full_name": new_full_name.strip(),
                                "username": new_username.strip(),
                                "role": role_map[new_role_label],
                            },
                        })
                        created = getattr(result, "user", None)
                        if not created:
                            raise ValueError("Supabase não retornou o usuário criado.")
                        service_client().table("profiles").update({
                            "full_name": new_full_name.strip(),
                            "username": new_username.strip(),
                            "email": new_email.strip().lower(),
                            "role": role_map[new_role_label],
                            "is_active": True,
                        }).eq("id", str(created.id)).execute()
                        audit("USER_CREATED", f"Usuário {new_username.strip()} criado como {role_map[new_role_label]}.")
                        st.success("Usuário criado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Não foi possível criar o usuário: {exc}")

        users_rows = fetch_all(DB, "profiles", "id,username,email,full_name,role,is_active,created_at,last_login", order="full_name")
        users_df = df_from(users_rows)
        if not users_df.empty:
            users_view = users_df.copy()
            users_view["role"] = users_view["role"].map(ROLE_LABELS)
            users_view["is_active"] = users_view["is_active"].map({True: "Ativo", False: "Inativo"})
            st.dataframe(users_view.rename(columns={
                "username": "Usuário", "email": "E-mail", "full_name": "Nome", "role": "Perfil",
                "is_active": "Status", "created_at": "Criado em", "last_login": "Último login",
            }).drop(columns=["id"]), use_container_width=True, hide_index=True)

            options = {
                f"{r['full_name']} (@{r['username']}) — {ROLE_LABELS[r['role']]} — {'Ativo' if r['is_active'] else 'Inativo'}": r["id"]
                for _, r in users_df.iterrows()
            }
            selected_label = st.selectbox("Gerenciar usuário", list(options.keys()))
            selected_id = options[selected_label]
            selected_row = users_df.loc[users_df["id"] == selected_id].iloc[0]
            c1, c2 = st.columns(2)
            role_label = c1.selectbox(
                "Perfil do usuário",
                ["Administrador", "Operador", "Consulta"],
                index=["admin", "operator", "viewer"].index(selected_row["role"]),
                key="manage_role",
            )
            active = c2.checkbox("Usuário ativo", value=bool(selected_row["is_active"]), key="manage_active")
            new_pwd = st.text_input("Nova senha (deixe vazio para manter)", type="password", key="manage_password")
            if st.button("Salvar usuário", type="primary"):
                if str(selected_id) == str(CURRENT_USER["id"]) and not active:
                    st.error("Você não pode desativar o usuário com o qual está conectado.")
                elif new_pwd and len(new_pwd) < 8:
                    st.error("A nova senha deve ter pelo menos 8 caracteres.")
                else:
                    role_map = {"Administrador": "admin", "Operador": "operator", "Consulta": "viewer"}
                    try:
                        service_client().table("profiles").update({
                            "role": role_map[role_label],
                            "is_active": bool(active),
                        }).eq("id", str(selected_id)).execute()
                        if new_pwd:
                            service_client().auth.admin.update_user_by_id(str(selected_id), {"password": new_pwd})
                        audit("USER_UPDATED", f"Usuário {selected_row['username']} atualizado.")
                        st.success("Usuário atualizado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Não foi possível atualizar o usuário: {exc}")

    with tab2:
        audit_rows = fetch_all(DB, "audit_log", "created_at,username,action,details", order="id", desc=True)
        audit_df = df_from(audit_rows[:300])
        if audit_df.empty:
            st.caption("Nenhum evento de auditoria.")
        else:
            st.dataframe(audit_df.rename(columns={
                "created_at": "Data/hora", "username": "Usuário", "action": "Ação", "details": "Detalhes"
            }), use_container_width=True, hide_index=True)

st.sidebar.divider()
st.sidebar.caption("V4 · Supabase PostgreSQL + Supabase Auth")
