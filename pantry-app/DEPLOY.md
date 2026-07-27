# Deploy de CityPantry App

## Opción A — Correr local (red local, ambos desde la misma WiFi)

1. Instala Python si no lo tienes: https://python.org
2. Abre terminal en la carpeta `pantry-app`
3. Ejecuta:
   ```
   python start_local.py
   ```
4. Abre en tu navegador: http://localhost:5000
5. Dora puede acceder desde su compu en la misma red: http://[TU-IP]:5000

Para saber tu IP en Windows: abre terminal y escribe `ipconfig`, busca "Dirección IPv4".

---

## Opción B — Deploy en Railway (recomendado, acceso desde cualquier lugar)

Railway es gratuito para uso pequeño (~500 horas/mes gratis).

### Paso 1 — Crear cuenta
Entra a https://railway.app y regístrate con tu cuenta de GitHub o Google.

### Paso 2 — Subir el código a GitHub
1. Crea un repo en https://github.com/new (puede ser privado)
2. Sube la carpeta `pantry-app` completa

Desde terminal en la carpeta:
```bash
git init
git add .
git commit -m "CityPantry app inicial"
git remote add origin https://github.com/TU-USUARIO/citypantry.git
git push -u origin main
```

### Paso 3 — Deploy en Railway
1. En Railway, haz clic en "New Project"
2. Elige "Deploy from GitHub repo"
3. Selecciona tu repo `citypantry`
4. Railway detecta automáticamente el Procfile y hace el deploy
5. En "Settings" → "Variables", agrega:
   - `SECRET_KEY` = cualquier string largo aleatorio (ej: `citypantry-prod-2024-xk9m`)
   - `DATABASE_PATH` = `/data/pantry.db`
6. En "Volumes", crea un volumen en `/data` para persistir la base de datos

### Paso 4 — Acceder
Railway te da una URL tipo `citypantry-production.up.railway.app`.
Comparte esa URL con Dora — ya pueden usar la app desde cualquier dispositivo.

---

## Variables de entorno

| Variable | Descripción | Default |
|---|---|---|
| `SECRET_KEY` | Clave para sesiones Flask | `citypantry-dev-key-2024` |
| `DATABASE_PATH` | Ruta del archivo SQLite | `pantry.db` |
| `PORT` | Puerto del servidor | `5000` |

---

## Notas importantes

- La base de datos es un archivo SQLite (`pantry.db`). Haz backup periódico descargando ese archivo.
- El XML del SAT viene en CFDI 3.3 o 4.0 — la app soporta ambos.
- Si un XML no tiene UUID (facturas viejas), igual se puede cargar sin problema.
