"""Migra os dados operacionais da V3/SQLite para Supabase.

Não migra as senhas/usuários antigos, porque a autenticação passa a ser Supabase Auth.
Crie os usuários novamente pela V4.

Uso:
  python migrate_sqlite_to_supabase.py caminho\\para\\rastreio.db
"""
import os
import sys
import sqlite3
from pathlib import Path
import tomllib
from supabase import create_client


def secret(name):
    if os.getenv(name):
        return os.getenv(name)
    p = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
    if p.exists():
        return tomllib.loads(p.read_text(encoding="utf-8")).get(name)
    return None


def chunked(rows, size=150):
    for i in range(0, len(rows), size):
        yield rows[i:i+size]


def table_exists(conn, name):
    return conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


if len(sys.argv) < 2:
    raise SystemExit("Uso: python migrate_sqlite_to_supabase.py C:\\caminho\\rastreio.db")

db_path = Path(sys.argv[1])
if not db_path.exists():
    raise SystemExit(f"Banco não encontrado: {db_path}")

url = secret("SUPABASE_URL")
key = secret("SUPABASE_SECRET_KEY")
if not url or not key:
    raise SystemExit("Configure SUPABASE_URL e SUPABASE_SECRET_KEY.")

sb = create_client(url, key)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# 1. Pedidos: não preserva IDs do SQLite; cria mapa antigo -> novo.
orders = [dict(r) for r in conn.execute("select id, order_number, expedition_start, created_at from orders order by id").fetchall()]
order_map = {}
for row in orders:
    payload = {
        "order_number": row["order_number"],
        "expedition_start": row["expedition_start"],
        "created_at": row["created_at"],
    }
    result = sb.table("orders").upsert(payload, on_conflict="order_number").select("id,order_number").execute()
    created = result.data[0]
    order_map[row["id"]] = created["id"]
print(f"Pedidos migrados: {len(orders)}")

# 2. NFs com remapeamento do pedido.
invoices = [dict(r) for r in conn.execute("select nf_number, order_id, first_seen_at from invoices").fetchall()]
inv_payload = []
for row in invoices:
    inv_payload.append({
        "nf_number": row["nf_number"],
        "order_id": order_map.get(row["order_id"]) if row["order_id"] is not None else None,
        "first_seen_at": row["first_seen_at"],
    })
for part in chunked(inv_payload):
    sb.table("invoices").upsert(part, on_conflict="nf_number").execute()
print(f"NFs migradas: {len(invoices)}")

# 3. Tracking atual.
if table_exists(conn, "tracking_current"):
    rows = [dict(r) for r in conn.execute("select * from tracking_current").fetchall()]
    for row in rows:
        if not row.get("occurrence_ts"):
            row["occurrence_ts"] = None
    for part in chunked(rows):
        sb.table("tracking_current").upsert(part, on_conflict="nf_number").execute()
    print(f"Rastreios atuais migrados: {len(rows)}")

# 4. Histórico.
if table_exists(conn, "tracking_history"):
    rows = [dict(r) for r in conn.execute("select * from tracking_history").fetchall()]
    for row in rows:
        if not row.get("occurrence_ts"):
            row["occurrence_ts"] = None
    for part in chunked(rows):
        sb.table("tracking_history").upsert(part, on_conflict="row_hash", ignore_duplicates=True).execute()
    print(f"Históricos migrados: {len(rows)}")

# 5. Importações, sem preservar ID/imported_by.
if table_exists(conn, "imports"):
    rows = [dict(r) for r in conn.execute("select filename, imported_at, rows_read, nf_mentions, new_history_records from imports order by id").fetchall()]
    for part in chunked(rows):
        sb.table("imports").insert(part).execute()
    print(f"Importações migradas: {len(rows)}")

# 6. Auditoria antiga opcional; sem user_id.
if table_exists(conn, "audit_log"):
    rows = [dict(r) for r in conn.execute("select username, action, details, created_at from audit_log order by id").fetchall()]
    for part in chunked(rows):
        sb.table("audit_log").insert(part).execute()
    print(f"Auditorias antigas migradas: {len(rows)}")

conn.close()
print("Migração concluída. Usuários/senhas devem ser recriados no Supabase Auth pela V4.")
