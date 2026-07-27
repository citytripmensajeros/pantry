"""
Ejecuta este script para correr la app localmente.
Solo necesitas tener Python instalado.

En terminal:
  pip install flask
  python start_local.py
"""
import subprocess, sys, os

# instalar dependencias si no están
try:
    import flask
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])

# inicializar DB e iniciar servidor
os.environ.setdefault("SECRET_KEY", "local-dev-secret")
from app import app, init_db
init_db()
print("\n✅ CityPantry corriendo en: http://localhost:5000\n")
app.run(debug=True, port=5000)
