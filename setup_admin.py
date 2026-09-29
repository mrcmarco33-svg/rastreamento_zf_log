"""Cria/promove o primeiro administrador no Supabase.

Uso:
  python setup_admin.py

Lê SUPABASE_URL e SUPABASE_SECRET_KEY das variáveis de ambiente ou
.streamlit/secrets.toml.
"""
import os
from pathlib import Path
import getpass
import tomllib
from supabase import create_client


def load_secret(name):
    if os.getenv(name):
        return os.getenv(name)
    p = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        return data.get(name)
    return None


url = load_secret("SUPABASE_URL")
service_key = load_secret("SUPABASE_SECRET_KEY")
if not url or not service_key:
    raise SystemExit("Defina SUPABASE_URL e SUPABASE_SECRET_KEY antes de executar.")

full_name = input("Nome completo: ").strip()
username = input("Usuário: ").strip()
email = input("E-mail: ").strip().lower()
password = getpass.getpass("Senha (mínimo 8 caracteres): ")

if len(username) < 3:
    raise SystemExit("Usuário deve ter pelo menos 3 caracteres.")
if len(password) < 8:
    raise SystemExit("Senha deve ter pelo menos 8 caracteres.")
if "@" not in email:
    raise SystemExit("E-mail inválido.")

client = create_client(url, service_key)
result = client.auth.admin.create_user({
    "email": email,
    "password": password,
    "email_confirm": True,
    "user_metadata": {"full_name": full_name, "username": username},
})
user = getattr(result, "user", None)
if not user:
    raise SystemExit("Usuário não foi criado.")

client.table("profiles").update({
    "full_name": full_name,
    "username": username,
    "email": email,
    "role": "admin",
    "is_active": True,
}).eq("id", str(user.id)).execute()

print(f"Administrador @{username} criado com sucesso.")
