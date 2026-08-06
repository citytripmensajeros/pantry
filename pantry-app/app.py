import os
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, flash, send_file, session
import io
import csv

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "citypantry-dev-key-2024")

# ─── AUTH ─────────────────────────────────────────────────────────────────────────

APP_USER     = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("autenticado"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not APP_PASSWORD:
            error = "APP_PASSWORD no configurada en el servidor."
        elif (request.form.get("usuario") == APP_USER and
              request.form.get("password") == APP_PASSWORD):
            session["autenticado"] = True
            session.permanent = False
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        else:
            error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

DATABASE = os.environ.get("DATABASE_PATH", "pantry.db")

# Migración automática: si hay DB vieja y la nueva ruta no existe, copiarla
_old_db = "pantry.db"
if DATABASE != _old_db and not os.path.exists(DATABASE) and os.path.exists(_old_db):
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    shutil.copy2(_old_db, DATABASE)
    print(f"[CityPantry] Base de datos migrada de {_old_db} a {DATABASE}")

# ─── DB CONNECTION ───────────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ─── INIT DB ─────────────────────────────────────────────────────────────────────

def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE,
            markup_default REAL DEFAULT 0.15,
            notas TEXT,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfc TEXT UNIQUE,
            nombre TEXT NOT NULL,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS facturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE,
            proveedor_id INTEGER REFERENCES proveedores(id),
            proveedor_nombre TEXT,
            fecha DATE NOT NULL,
            subtotal REAL,
            total REAL,
            xml_raw TEXT,
            subido_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conceptos_factura (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
            descripcion TEXT NOT NULL,
            cantidad REAL NOT NULL,
            unidad TEXT,
            valor_unitario REAL NOT NULL,
            descuento_factura REAL DEFAULT 0,
            costo_real_unitario REAL NOT NULL,
            importe_total REAL NOT NULL,
            clave_prod_serv TEXT,
            tipo TEXT DEFAULT 'producto'
        );

        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE,
            precio_base REAL,
            unidad TEXT,
            categoria TEXT,
            notas TEXT,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS precio_base_historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL REFERENCES productos(id),
            precio_base REAL NOT NULL,
            registrado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL UNIQUE,
            cliente_id INTEGER NOT NULL REFERENCES clientes(id),
            fecha DATE NOT NULL,
            status TEXT DEFAULT 'abierto',
            notas TEXT,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pedido_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id INTEGER NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
            concepto_id INTEGER REFERENCES conceptos_factura(id),
            producto_nombre TEXT NOT NULL,
            cantidad REAL NOT NULL,
            precio_base REAL NOT NULL,
            costo_real REAL NOT NULL,
            markup_pct REAL NOT NULL DEFAULT 0.15,
            proveedor_nombre TEXT,
            notas TEXT
        );

        CREATE VIEW IF NOT EXISTS v_pedido_items AS
        SELECT
            pi.id, pi.pedido_id, pi.concepto_id, pi.producto_nombre,
            pi.cantidad, pi.precio_base, pi.costo_real,
            pi.markup_pct, pi.proveedor_nombre, pi.notas,
            (pi.precio_base - pi.costo_real) as descuento_proveedor,
            (pi.precio_base * (1 + pi.markup_pct)) as precio_cliente,
            (pi.costo_real * pi.cantidad) as total_costo,
            (pi.precio_base * (1 + pi.markup_pct) * pi.cantidad) as total_facturar
        FROM pedido_items pi;
    """)
    db.commit()
    db.close()

# ─── MIGRACIONES ─────────────────────────────────────────────────────────────────

def migrate_db():
    db = sqlite3.connect(DATABASE)
    migrations = [
        "ALTER TABLE conceptos_factura ADD COLUMN tipo TEXT DEFAULT 'producto'",
        "ALTER TABLE productos ADD COLUMN alegra_codigo TEXT",
        "ALTER TABLE productos ADD COLUMN descripcion_xml TEXT",
        "ALTER TABLE clientes ADD COLUMN alegra_codigo TEXT",
        "ALTER TABLE clientes ADD COLUMN costo_envio_default REAL DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN costo_envio REAL DEFAULT 0",
        """CREATE VIEW IF NOT EXISTS v_pedido_items AS
        SELECT
            pi.id, pi.pedido_id, pi.concepto_id, pi.producto_nombre,
            pi.cantidad, pi.precio_base, pi.costo_real,
            pi.markup_pct, pi.proveedor_nombre, pi.notas,
            (pi.precio_base - pi.costo_real) as descuento_proveedor,
            (pi.precio_base * (1 + pi.markup_pct)) as precio_cliente,
            (pi.costo_real * pi.cantidad) as total_costo,
            (pi.precio_base * (1 + pi.markup_pct) * pi.cantidad) as total_facturar
        FROM pedido_items pi""",
    ]
    for sql in migrations:
        try:
            db.execute(sql)
            db.commit()
        except Exception:
            pass
    db.close()

# ─── XML CFDI PARSER ─────────────────────────────────────────────────────────────

CFDI_NS = {
    "3": "http://www.sat.gob.mx/cfd/3",
    "4": "http://www.sat.gob.mx/cfd/4",
}

def parse_cfdi(xml_content):
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}")

    tag = root.tag
    ns = None
    for ver, uri in CFDI_NS.items():
        if uri in tag:
            ns = {"cfdi": uri}
            break
    if ns is None:
        ns = {"cfdi": ""}

    def find(el, path):
        try: return el.find(path, ns)
        except: return None

    def findall(el, path):
        try: return el.findall(path, ns)
        except: return []

    def attr(el, name, default=""):
        if el is None: return default
        return el.get(name, default)

    emisor = find(root, "cfdi:Emisor")
    proveedor_rfc = attr(emisor, "Rfc")
    proveedor_nombre = attr(emisor, "Nombre")
    fecha_str = attr(root, "Fecha") or attr(root, "fecha")

    try:
        fecha = datetime.fromisoformat(fecha_str[:10]).date().isoformat()
    except:
        fecha = datetime.today().date().isoformat()

    uuid = ""
    complemento = find(root, "cfdi:Complemento")
    if complemento is not None:
        for child in complemento:
            if "TimbreFiscalDigital" in child.tag:
                uuid = child.get("UUID", "") or child.get("uuid", "")

    subtotal = float(attr(root, "SubTotal") or attr(root, "Subtotal") or 0)
    total = float(attr(root, "Total") or attr(root, "total") or 0)

    conceptos_el = find(root, "cfdi:Conceptos")
    conceptos = []
    if conceptos_el is not None:
        for c in findall(conceptos_el, "cfdi:Concepto"):
            cantidad = float(attr(c, "Cantidad", "1"))
            valor_unitario = float(attr(c, "ValorUnitario", "0"))
            importe = float(attr(c, "Importe", "0"))
            descuento = float(attr(c, "Descuento", "0"))
            descripcion = attr(c, "Descripcion") or attr(c, "descripcion", "Sin descripción")
            unidad = attr(c, "ClaveUnidad") or attr(c, "Unidad", "")
            clave = attr(c, "ClaveProdServ", "")
            costo_real = (importe - descuento) / cantidad if cantidad else valor_unitario
            conceptos.append({
                "descripcion": descripcion,
                "cantidad": cantidad,
                "unidad": unidad,
                "valor_unitario": valor_unitario,
                "descuento_factura": descuento,
                "costo_real_unitario": round(costo_real, 4),
                "importe_total": round(importe - descuento, 4),
                "clave_prod_serv": clave,
            })

    return {
        "uuid": uuid,
        "proveedor_rfc": proveedor_rfc,
        "proveedor_nombre": proveedor_nombre,
        "fecha": fecha,
        "subtotal": subtotal,
        "total": total,
        "conceptos": conceptos,
    }

# ─── HELPERS ─────────────────────────────────────────────────────────────────────

def get_or_create_proveedor(db, rfc, nombre):
    row = db.execute("SELECT id FROM proveedores WHERE rfc = ?", (rfc,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO proveedores (rfc, nombre) VALUES (?,?)", (rfc, nombre))
    db.commit()
    return cur.lastrowid

def get_or_create_producto(db, nombre):
    row = db.execute("SELECT id, precio_base FROM productos WHERE nombre = ?", (nombre,)).fetchone()
    if row:
        return dict(row)
    db.execute("INSERT INTO productos (nombre) VALUES (?)", (nombre,))
    db.commit()
    row = db.execute("SELECT id, precio_base FROM productos WHERE nombre = ?", (nombre,)).fetchone()
    return dict(row)

# ─── ROUTES ──────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    db = get_db()
    stats = {
        "pedidos_abiertos": db.execute("SELECT COUNT(*) FROM pedidos WHERE status='abierto'").fetchone()[0],
        "facturas_cargadas": db.execute("SELECT COUNT(*) FROM facturas").fetchone()[0],
        "clientes": db.execute("SELECT COUNT(*) FROM clientes").fetchone()[0],
        "productos": db.execute("SELECT COUNT(*) FROM productos").fetchone()[0],
    }
    pedidos_recientes = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre,
               COUNT(pi.id) as num_items,
               SUM(pi.total_facturar) as total_facturar
        FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id
        LEFT JOIN v_pedido_items pi ON pi.pedido_id = p.id
        GROUP BY p.id ORDER BY p.fecha DESC LIMIT 5
    """).fetchall()
    return render_template("index.html", stats=stats, pedidos_recientes=pedidos_recientes)

# ── FACTURAS / XML ────────────────────────────────────────────────────────────────

@app.route("/facturas")
@login_required
def facturas():
    db = get_db()
    rows = db.execute("""
        SELECT f.*, p.nombre as proveedor_nombre_cat, COUNT(c.id) as num_conceptos
        FROM facturas f
        LEFT JOIN proveedores p ON f.proveedor_id = p.id
        LEFT JOIN conceptos_factura c ON c.factura_id = f.id
        GROUP BY f.id ORDER BY f.fecha DESC
    """).fetchall()
    return render_template("facturas.html", facturas=rows)

@app.route("/facturas/upload", methods=["GET", "POST"])
@login_required
def upload_factura():
    db = get_db()
    if request.method == "POST":
        files = request.files.getlist("xmlfiles")
        resultados = []
        for f in files:
            if not f.filename:
                continue
            try:
                content = f.read().decode("utf-8-sig")
                data = parse_cfdi(content)
                prov_id = get_or_create_proveedor(db, data["proveedor_rfc"], data["proveedor_nombre"])
                existing = db.execute("SELECT id FROM facturas WHERE uuid=?", (data["uuid"],)).fetchone()
                if existing and data["uuid"]:
                    resultados.append({"archivo": f.filename, "status": "duplicada", "uuid": data["uuid"]})
                    continue
                cur = db.execute("""
                    INSERT INTO facturas (uuid, proveedor_id, proveedor_nombre, fecha, subtotal, total, xml_raw)
                    VALUES (?,?,?,?,?,?,?)
                """, (data["uuid"], prov_id, data["proveedor_nombre"],
                      data["fecha"], data["subtotal"], data["total"], content))
                factura_id = cur.lastrowid
                for c in data["conceptos"]:
                    db.execute("""
                        INSERT INTO conceptos_factura
                        (factura_id, descripcion, cantidad, unidad, valor_unitario,
                         descuento_factura, costo_real_unitario, importe_total, clave_prod_serv)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (factura_id, c["descripcion"], c["cantidad"], c["unidad"],
                          c["valor_unitario"], c["descuento_factura"], c["costo_real_unitario"],
                          c["importe_total"], c["clave_prod_serv"]))
                db.commit()
                resultados.append({
                    "archivo": f.filename, "status": "ok",
                    "factura_id": factura_id,
                    "proveedor": data["proveedor_nombre"],
                    "fecha": data["fecha"], "conceptos": len(data["conceptos"])
                })
            except Exception as e:
                resultados.append({"archivo": f.filename, "status": "error", "mensaje": str(e)})
        return render_template("upload_resultado.html", resultados=resultados)
    return render_template("upload.html")

@app.route("/facturas/<int:factura_id>")
@login_required
def factura_detail(factura_id):
    db = get_db()
    factura = db.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
    if not factura:
        flash("Factura no encontrada", "error")
        return redirect(url_for("facturas"))
    conceptos = db.execute("""
        SELECT cf.*, pi.id as asignado_item_id, pi.pedido_id,
               p.numero as pedido_numero, c.nombre as cliente_nombre,
               pr.precio_base as precio_base_catalogo,
               pr.nombre as prod_nombre_catalogo,
               pr.alegra_codigo as prod_alegra_codigo,
               pr.id as prod_id
        FROM conceptos_factura cf
        LEFT JOIN pedido_items pi ON pi.concepto_id = cf.id
        LEFT JOIN pedidos p ON pi.pedido_id = p.id
        LEFT JOIN clientes c ON p.cliente_id = c.id
        LEFT JOIN productos pr ON (pr.nombre = cf.descripcion OR pr.descripcion_xml = cf.descripcion)
        WHERE cf.factura_id = ?
    """, (factura_id,)).fetchall()
    pedidos = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE p.status = 'abierto' ORDER BY p.fecha DESC
    """).fetchall()
    return render_template("factura_detail.html", factura=factura, conceptos=conceptos, pedidos=pedidos)

@app.route("/facturas/concepto/<int:concepto_id>/configurar_producto", methods=["POST"])
@login_required
def configurar_producto(concepto_id):
    db = get_db()
    concepto = db.execute("SELECT * FROM conceptos_factura WHERE id=?", (concepto_id,)).fetchone()
    if not concepto:
        flash("Concepto no encontrado", "error")
        return redirect(url_for("facturas"))

    nombre_catalogo = request.form.get("nombre_catalogo", "").strip() or concepto["descripcion"]
    alegra_codigo   = request.form.get("alegra_codigo", "").strip() or None
    factura_id      = concepto["factura_id"]

    # Buscar si ya hay un producto vinculado a esta descripción XML
    prod_by_xml  = db.execute("SELECT id FROM productos WHERE descripcion_xml=?", (concepto["descripcion"],)).fetchone()
    prod_by_orig = db.execute("SELECT id FROM productos WHERE nombre=?",          (concepto["descripcion"],)).fetchone()
    prod_by_new  = db.execute("SELECT id FROM productos WHERE nombre=?",          (nombre_catalogo,)).fetchone()

    if prod_by_xml:
        # Ya está vinculado — solo actualiza nombre y código
        db.execute("UPDATE productos SET nombre=?, alegra_codigo=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=?",
                   (nombre_catalogo, alegra_codigo, prod_by_xml["id"]))
    elif prod_by_orig:
        # Existe con el nombre del XML — renombrar y guardar alias
        db.execute("""UPDATE productos SET nombre=?, alegra_codigo=?, descripcion_xml=?,
                      actualizado_en=CURRENT_TIMESTAMP WHERE id=?""",
                   (nombre_catalogo, alegra_codigo, concepto["descripcion"], prod_by_orig["id"]))
    elif prod_by_new:
        # Ya existe con nombre personalizado — solo agregar alias y código
        db.execute("""UPDATE productos SET alegra_codigo=?, descripcion_xml=?,
                      actualizado_en=CURRENT_TIMESTAMP WHERE id=?""",
                   (alegra_codigo, concepto["descripcion"], prod_by_new["id"]))
    else:
        # Producto nuevo — crear con nombre personalizado y alias XML
        db.execute("INSERT INTO productos (nombre, alegra_codigo, descripcion_xml) VALUES (?,?,?)",
                   (nombre_catalogo, alegra_codigo, concepto["descripcion"]))

    db.commit()
    flash(f"Producto '{nombre_catalogo}' configurado en catálogo", "success")
    return redirect(url_for("factura_detail", factura_id=factura_id))

@app.route("/facturas/concepto/<int:concepto_id>/tipo", methods=["POST"])
@login_required
def cambiar_tipo_concepto(concepto_id):
    db = get_db()
    tipo = request.form.get("tipo", "producto")
    concepto = db.execute(
        "SELECT cf.*, f.id as fid FROM conceptos_factura cf JOIN facturas f ON cf.factura_id=f.id WHERE cf.id=?",
        (concepto_id,)).fetchone()
    if not concepto:
        flash("Concepto no encontrado", "error")
        return redirect(url_for("facturas"))
    db.execute("UPDATE conceptos_factura SET tipo=? WHERE id=?", (tipo, concepto_id))
    db.commit()
    label = "gasto operativo" if tipo == "gasto" else "producto"
    flash(f"Concepto marcado como {label}", "success")
    return redirect(url_for("factura_detail", factura_id=concepto["fid"]))

# ── PEDIDOS ───────────────────────────────────────────────────────────────────────

@app.route("/pedidos")
@login_required
def pedidos():
    db = get_db()
    rows = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre,
               COUNT(pi.id) as num_items,
               COALESCE(SUM(pi.total_costo),0) as total_costo,
               COALESCE(SUM(pi.total_facturar),0) as total_facturar
        FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id
        LEFT JOIN v_pedido_items pi ON pi.pedido_id = p.id
        GROUP BY p.id ORDER BY p.fecha DESC
    """).fetchall()
    clientes = db.execute("SELECT id, nombre, markup_default, costo_envio_default FROM clientes ORDER BY nombre").fetchall()

    # Generar siguiente folio P-XXX
    import re
    ultimos = db.execute("SELECT numero FROM pedidos ORDER BY id DESC LIMIT 20").fetchall()
    max_num = 0
    for u in ultimos:
        m = re.match(r'P-(\d+)', u["numero"])
        if m:
            max_num = max(max_num, int(m.group(1)))
    siguiente_folio = f"P-{(max_num + 1):03d}"

    return render_template("pedidos.html", pedidos=rows, clientes=clientes, siguiente_folio=siguiente_folio)

@app.route("/pedidos/nuevo", methods=["POST"])
@login_required
def nuevo_pedido():
    db = get_db()
    try:
        db.execute("INSERT INTO pedidos (numero, cliente_id, fecha, notas, costo_envio) VALUES (?,?,?,?,?)",
                   (request.form["numero"], request.form["cliente_id"],
                    request.form["fecha"], request.form.get("notas", ""),
                    float(request.form.get("costo_envio", 0) or 0)))
        db.commit()
        flash(f"Pedido {request.form['numero']} creado", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("pedidos"))

@app.route("/pedidos/<int:pedido_id>")
@login_required
def pedido_detail(pedido_id):
    db = get_db()
    pedido = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre, c.markup_default, c.alegra_codigo as cliente_alegra_codigo
        FROM pedidos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id=?
    """, (pedido_id,)).fetchone()
    if not pedido:
        flash("Pedido no encontrado", "error")
        return redirect(url_for("pedidos"))
    items = db.execute("""
        SELECT pi.*, f.fecha as factura_fecha
        FROM v_pedido_items pi
        LEFT JOIN conceptos_factura cf ON pi.concepto_id = cf.id
        LEFT JOIN facturas f ON cf.factura_id = f.id
        WHERE pi.pedido_id = ?
    """, (pedido_id,)).fetchall()
    sin_asignar = db.execute("""
        SELECT cf.*, f.fecha, f.proveedor_nombre, f.id as factura_id,
               pr.precio_base as precio_base_catalogo
        FROM conceptos_factura cf
        JOIN facturas f ON cf.factura_id = f.id
        LEFT JOIN productos pr ON pr.nombre = cf.descripcion
        WHERE cf.id NOT IN (SELECT concepto_id FROM pedido_items WHERE concepto_id IS NOT NULL)
        AND (cf.tipo IS NULL OR cf.tipo = 'producto')
        ORDER BY f.fecha DESC, cf.descripcion
    """).fetchall()
    productos_cat = db.execute("SELECT * FROM productos ORDER BY nombre").fetchall()
    costo_envio = pedido["costo_envio"] or 0
    totales = {
        "costo": sum(i["total_costo"] for i in items),
        "facturar": sum(i["total_facturar"] for i in items) + costo_envio,
        "margen": sum((i["total_facturar"] - i["total_costo"]) for i in items) + costo_envio,
        "envio": costo_envio,
    }
    totales["margen_pct"] = (totales["margen"] / totales["facturar"] * 100) if totales["facturar"] > 0 else 0
    return render_template("pedido_detail.html", pedido=pedido, items=items,
                           sin_asignar=sin_asignar, productos_cat=productos_cat, totales=totales)

@app.route("/pedidos/<int:pedido_id>/agregar_concepto", methods=["POST"])
@login_required
def agregar_concepto(pedido_id):
    db = get_db()
    concepto_id = request.form.get("concepto_id")
    pedido = db.execute("SELECT p.*, c.markup_default FROM pedidos p JOIN clientes c ON p.cliente_id=c.id WHERE p.id=?",
                        (pedido_id,)).fetchone()
    concepto = db.execute("SELECT cf.*, f.proveedor_nombre FROM conceptos_factura cf JOIN facturas f ON cf.factura_id=f.id WHERE cf.id=?",
                          (concepto_id,)).fetchone()
    if not concepto or not pedido:
        return jsonify({"error": "No encontrado"}), 404

    nombre_custom  = request.form.get("nombre_custom", "").strip() or concepto["descripcion"]
    cantidad_custom = float(request.form.get("cantidad_custom") or concepto["cantidad"])
    nota_equiv     = request.form.get("nota_equivalencia", "").strip()

    # Costo real unitario: puede venir ajustado del form (ej: equivalencia de presentación)
    costo_real_form = request.form.get("costo_real_custom", "").strip()
    if costo_real_form:
        costo_real_unit = float(costo_real_form)
    else:
        # Fallback: dividir total de factura entre cantidad ajustada
        total_factura = concepto["costo_real_unitario"] * concepto["cantidad"]
        costo_real_unit = total_factura / cantidad_custom if cantidad_custom else concepto["costo_real_unitario"]

    # Si el nombre cambió, renombrar en catálogo automáticamente
    if nombre_custom != concepto["descripcion"]:
        old_prod = db.execute("SELECT id FROM productos WHERE nombre=?", (concepto["descripcion"],)).fetchone()
        new_exists = db.execute("SELECT id FROM productos WHERE nombre=?", (nombre_custom,)).fetchone()
        if old_prod and not new_exists:
            db.execute("UPDATE productos SET nombre=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=?",
                       (nombre_custom, old_prod["id"]))

    prod = get_or_create_producto(db, nombre_custom)

    # Precio base: usar el del form si lo capturaron, si no el del catálogo
    precio_base_form = request.form.get("precio_base_custom", "").strip()
    if precio_base_form:
        precio_base = float(precio_base_form)
        db.execute("UPDATE productos SET precio_base=?, actualizado_en=CURRENT_TIMESTAMP WHERE nombre=?",
                   (precio_base, nombre_custom))
    else:
        precio_base = prod["precio_base"] or costo_real_unit

    markup = pedido["markup_default"]

    db.execute("""
        INSERT INTO pedido_items
        (pedido_id, concepto_id, producto_nombre, cantidad, precio_base, costo_real, markup_pct, proveedor_nombre, notas)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (pedido_id, concepto_id, nombre_custom, cantidad_custom,
          precio_base, costo_real_unit, markup, concepto["proveedor_nombre"], nota_equiv))
    db.commit()
    flash(f"Producto '{nombre_custom}' agregado al pedido", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/agregar_manual", methods=["POST"])
@login_required
def agregar_manual(pedido_id):
    db = get_db()
    pedido = db.execute("SELECT p.*, c.markup_default FROM pedidos p JOIN clientes c ON p.cliente_id=c.id WHERE p.id=?",
                        (pedido_id,)).fetchone()
    producto_nombre = request.form["producto_nombre"]
    cantidad = float(request.form["cantidad"])
    costo_real = float(request.form["costo_real"])
    precio_base = float(request.form["precio_base"])
    markup = float(request.form.get("markup_pct", pedido["markup_default"]))
    proveedor = request.form.get("proveedor_nombre", "")
    db.execute("""
        INSERT INTO pedido_items
        (pedido_id, producto_nombre, cantidad, precio_base, costo_real, markup_pct, proveedor_nombre)
        VALUES (?,?,?,?,?,?,?)
    """, (pedido_id, producto_nombre, cantidad, precio_base, costo_real, markup, proveedor))
    db.commit()
    flash(f"Producto '{producto_nombre}' agregado manualmente", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/item/<int:item_id>/editar", methods=["POST"])
@login_required
def editar_item(pedido_id, item_id):
    db = get_db()
    db.execute("""
        UPDATE pedido_items SET producto_nombre=?, precio_base=?, markup_pct=?, cantidad=?, notas=?
        WHERE id=? AND pedido_id=?
    """, (request.form["producto_nombre"].strip(),
          float(request.form["precio_base"]),
          float(request.form["markup_pct"]),
          float(request.form["cantidad"]),
          request.form.get("notas", "").strip(),
          item_id, pedido_id))
    db.commit()
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/item/<int:item_id>/eliminar", methods=["POST"])
@login_required
def eliminar_item(pedido_id, item_id):
    db = get_db()
    db.execute("DELETE FROM pedido_items WHERE id=? AND pedido_id=?", (item_id, pedido_id))
    db.commit()
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/cerrar", methods=["POST"])
@login_required
def cerrar_pedido(pedido_id):
    db = get_db()
    db.execute("UPDATE pedidos SET status='cerrado' WHERE id=?", (pedido_id,))
    db.commit()
    flash("Pedido cerrado", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/reabrir", methods=["POST"])
@login_required
def reabrir_pedido(pedido_id):
    db = get_db()
    db.execute("UPDATE pedidos SET status='abierto' WHERE id=?", (pedido_id,))
    db.commit()
    flash("Pedido reabierto", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/envio", methods=["POST"])
@login_required
def actualizar_envio(pedido_id):
    db = get_db()
    costo_envio = float(request.form.get("costo_envio", 0) or 0)
    db.execute("UPDATE pedidos SET costo_envio=? WHERE id=?", (costo_envio, pedido_id))
    db.commit()
    flash("Costo de envío actualizado", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/remision")
@login_required
def remision_pedido(pedido_id):
    db = get_db()
    pedido = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id WHERE p.id=?
    """, (pedido_id,)).fetchone()
    if not pedido:
        flash("Pedido no encontrado", "error")
        return redirect(url_for("pedidos"))
    items = db.execute("""
        SELECT pi.producto_nombre, pi.cantidad
        FROM pedido_items pi
        WHERE pi.pedido_id = ?
        ORDER BY pi.id
    """, (pedido_id,)).fetchall()
    return render_template("remision.html", pedido=pedido, items=items)

@app.route("/pedidos/<int:pedido_id>/exportar")
@login_required
def exportar_pedido(pedido_id):
    db = get_db()
    pedido = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id WHERE p.id=?
    """, (pedido_id,)).fetchone()
    items = db.execute("""
        SELECT pi.*, f.fecha as factura_fecha
        FROM v_pedido_items pi
        LEFT JOIN conceptos_factura cf ON pi.concepto_id = cf.id
        LEFT JOIN facturas f ON cf.factura_id = f.id
        WHERE pi.pedido_id=?
    """, (pedido_id,)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Cliente", pedido["cliente_nombre"]])
    writer.writerow(["Pedido", pedido["numero"]])
    writer.writerow(["Fecha", pedido["fecha"]])
    writer.writerow([])
    writer.writerow(["Producto", "Proveedor", "Cantidad", "Costo Real Unit.",
                     "Precio Base Unit.", "Margen Capturado Unit.",
                     "Markup %", "Precio Cliente Unit.", "Total Costo", "Total a Facturar"])
    for item in items:
        writer.writerow([
            item["producto_nombre"],
            item["proveedor_nombre"] or "",
            item["cantidad"],
            f"{item['costo_real']:.2f}",
            f"{item['precio_base']:.2f}",
            f"{item['descuento_proveedor']:.2f}",
            f"{item['markup_pct']*100:.1f}%",
            f"{item['precio_cliente']:.2f}",
            f"{item['total_costo']:.2f}",
            f"{item['total_facturar']:.2f}",
        ])
    total_costo = sum(i["total_costo"] for i in items)
    total_facturar = sum(i["total_facturar"] for i in items)
    writer.writerow([])
    writer.writerow(["", "", "", "", "", "", "", "TOTALES", f"{total_costo:.2f}", f"{total_facturar:.2f}"])
    writer.writerow(["", "", "", "", "", "", "", "MARGEN", "", f"{total_facturar - total_costo:.2f}"])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"pedido_{pedido['numero']}_{pedido['cliente_nombre']}.csv"
    )

# ── CLIENTES ──────────────────────────────────────────────────────────────────────

@app.route("/clientes")
@login_required
def clientes():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(p.id) as num_pedidos,
               COALESCE(SUM(pi.total_facturar),0) as total_facturado
        FROM clientes c
        LEFT JOIN pedidos p ON p.cliente_id = c.id
        LEFT JOIN v_pedido_items pi ON pi.pedido_id = p.id
        GROUP BY c.id ORDER BY c.nombre
    """).fetchall()
    return render_template("clientes.html", clientes=rows)

@app.route("/clientes/nuevo", methods=["POST"])
@login_required
def nuevo_cliente():
    db = get_db()
    try:
        db.execute("INSERT INTO clientes (nombre, markup_default, notas, alegra_codigo, costo_envio_default) VALUES (?,?,?,?,?)",
                   (request.form["nombre"], float(request.form.get("markup_default", 0.15)),
                    request.form.get("notas", ""),
                    request.form.get("alegra_codigo", "").strip() or None,
                    float(request.form.get("costo_envio_default", 0) or 0)))
        db.commit()
        flash(f"Cliente '{request.form['nombre']}' creado", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("clientes"))

@app.route("/clientes/<int:cliente_id>/editar", methods=["POST"])
@login_required
def editar_cliente(cliente_id):
    db = get_db()
    db.execute("""UPDATE clientes SET markup_default=?, notas=?, alegra_codigo=?, costo_envio_default=? WHERE id=?""",
               (float(request.form.get("markup_default", 0.15)),
                request.form.get("notas", ""),
                request.form.get("alegra_codigo", "").strip() or None,
                float(request.form.get("costo_envio_default", 0) or 0),
                cliente_id))
    db.commit()
    flash("Cliente actualizado", "success")
    return redirect(url_for("clientes"))

# ── CATÁLOGO ──────────────────────────────────────────────────────────────────────

@app.route("/catalogo")
@login_required
def catalogo():
    db = get_db()
    q = request.args.get("q", "")
    sql = """
        SELECT p.*,
               (SELECT COUNT(*) FROM pedido_items pi WHERE pi.producto_nombre = p.nombre) as veces_pedido,
               (SELECT MAX(f.fecha) FROM pedido_items pi
                JOIN conceptos_factura cf ON pi.concepto_id = cf.id
                JOIN facturas f ON cf.factura_id = f.id
                WHERE pi.producto_nombre = p.nombre) as ultima_compra
        FROM productos p
        {}ORDER BY p.nombre
    """
    if q:
        rows = db.execute(sql.format("WHERE p.nombre LIKE ? "), (f"%{q}%",)).fetchall()
    else:
        rows = db.execute(sql.format("")).fetchall()
    return render_template("catalogo.html", productos=rows, q=q)

@app.route("/catalogo/<int:producto_id>/editar", methods=["POST"])
@login_required
def editar_producto(producto_id):
    db = get_db()
    nombre_nuevo = request.form.get("nombre", "").strip()
    precio_base_nuevo = request.form.get("precio_base")
    categoria = request.form.get("categoria", "")
    notas = request.form.get("notas", "")

    prod = db.execute("SELECT * FROM productos WHERE id=?", (producto_id,)).fetchone()
    if not prod:
        flash("Producto no encontrado", "error")
        return redirect(url_for("catalogo"))

    if nombre_nuevo and nombre_nuevo != prod["nombre"]:
        existe = db.execute("SELECT id FROM productos WHERE nombre=? AND id!=?",
                            (nombre_nuevo, producto_id)).fetchone()
        if existe:
            flash(f"Ya existe un producto con el nombre '{nombre_nuevo}'", "error")
            return redirect(url_for("catalogo"))
        db.execute("UPDATE productos SET nombre=? WHERE id=?", (nombre_nuevo, producto_id))

    alegra_codigo = request.form.get("alegra_codigo", "").strip() or None

    if precio_base_nuevo:
        precio_base_nuevo = float(precio_base_nuevo)
        if prod["precio_base"] != precio_base_nuevo:
            db.execute("INSERT INTO precio_base_historial (producto_id, precio_base) VALUES (?,?)",
                       (producto_id, precio_base_nuevo))
        db.execute("""UPDATE productos SET precio_base=?, categoria=?, notas=?,
                      alegra_codigo=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=?""",
                   (precio_base_nuevo, categoria, notas, alegra_codigo, producto_id))
    else:
        db.execute("UPDATE productos SET categoria=?, notas=?, alegra_codigo=? WHERE id=?",
                   (categoria, notas, alegra_codigo, producto_id))

    db.commit()
    flash("Producto actualizado", "success")
    return redirect(url_for("catalogo"))

# ── CONSULTA EN CAMPO ─────────────────────────────────────────────────────────────

@app.route("/consulta")
@login_required
def consulta():
    db = get_db()
    q = request.args.get("q", "")
    resultados = []
    if q:
        resultados = db.execute("""
            SELECT cf.descripcion, cf.cantidad, cf.costo_real_unitario, cf.descuento_factura,
                   f.fecha, f.proveedor_nombre,
                   pr.nombre as producto_nombre, pr.precio_base,
                   pi.markup_pct, pi.precio_cliente, pi.pedido_id,
                   ped.numero as pedido_numero, c.nombre as cliente_nombre
            FROM conceptos_factura cf
            JOIN facturas f ON cf.factura_id = f.id
            JOIN productos pr ON pr.nombre = cf.descripcion
            LEFT JOIN pedido_items pi ON pi.concepto_id = cf.id
            LEFT JOIN pedidos ped ON pi.pedido_id = ped.id
            LEFT JOIN clientes c ON ped.cliente_id = c.id
            WHERE cf.descripcion LIKE ?
            ORDER BY f.fecha DESC
        """, (f"%{q}%",)).fetchall()
    return render_template("consulta.html", q=q, resultados=resultados)

# ── GASTOS OPERATIVOS ─────────────────────────────────────────────────────────────

@app.route("/gastos")
@login_required
def gastos_operativos():
    db = get_db()
    rows = db.execute("""
        SELECT cf.descripcion, cf.cantidad, cf.costo_real_unitario, cf.importe_total,
               f.fecha, f.proveedor_nombre
        FROM conceptos_factura cf
        JOIN facturas f ON cf.factura_id = f.id
        WHERE cf.tipo = 'gasto'
        ORDER BY f.fecha DESC
    """).fetchall()
    resumen = db.execute("""
        SELECT f.proveedor_nombre, COUNT(*) as num_cargos, SUM(cf.importe_total) as total
        FROM conceptos_factura cf
        JOIN facturas f ON cf.factura_id = f.id
        WHERE cf.tipo = 'gasto'
        GROUP BY f.proveedor_nombre ORDER BY total DESC
    """).fetchall()
    return render_template("gastos.html", gastos=rows, resumen=resumen)

@app.route("/reporte")
@login_required
def reporte():
    db = get_db()
    clientes = db.execute("SELECT * FROM clientes ORDER BY nombre").fetchall()
    pedidos_lista = db.execute("""
        SELECT p.id, p.numero, p.fecha, c.nombre as cliente_nombre
        FROM pedidos p JOIN clientes c ON p.cliente_id=c.id
        ORDER BY p.fecha DESC
    """).fetchall()

    # Filtros
    cliente_id = request.args.get("cliente_id", "")
    pedido_id  = request.args.get("pedido_id", "")
    fecha_ini  = request.args.get("fecha_ini", "")
    fecha_fin  = request.args.get("fecha_fin", "")

    where = ["1=1"]
    params = []
    if cliente_id:
        where.append("p.cliente_id = ?"); params.append(cliente_id)
    if pedido_id:
        where.append("p.id = ?"); params.append(pedido_id)
    if fecha_ini:
        where.append("p.fecha >= ?"); params.append(fecha_ini)
    if fecha_fin:
        where.append("p.fecha <= ?"); params.append(fecha_fin)

    rows = db.execute(f"""
        SELECT
            p.numero as pedido_numero, p.fecha as pedido_fecha,
            c.nombre as cliente_nombre,
            pi.producto_nombre, pi.cantidad,
            pi.costo_real, pi.precio_base, pi.markup_pct,
            pi.proveedor_nombre,
            (pi.precio_base - pi.costo_real) as descuento_proveedor,
            (pi.precio_base * (1 + pi.markup_pct)) as precio_cliente,
            (pi.precio_base * (1 + pi.markup_pct) - pi.costo_real) as margen_unit,
            (pi.precio_base * (1 + pi.markup_pct) * pi.cantidad) as total_facturar,
            (pi.costo_real * pi.cantidad) as total_costo,
            f.uuid as folio_uuid, f.proveedor_nombre as factura_proveedor
        FROM pedido_items pi
        JOIN pedidos p ON pi.pedido_id = p.id
        JOIN clientes c ON p.cliente_id = c.id
        LEFT JOIN conceptos_factura cf ON pi.concepto_id = cf.id
        LEFT JOIN facturas f ON cf.factura_id = f.id
        WHERE {" AND ".join(where)}
        ORDER BY p.fecha DESC, p.numero, pi.id
    """, params).fetchall()

    exportar = request.args.get("exportar", "")
    if exportar == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Pedido", "Fecha", "Cliente", "Producto", "Proveedor",
                         "Folio Factura", "Cantidad", "Costo Real Unit.",
                         "Precio Base Unit.", "Desc. Proveedor Unit.",
                         "Markup %", "Precio Cliente Unit.", "Margen Unit.",
                         "Total Costo", "Total Facturar"])
        for r in rows:
            writer.writerow([
                r["pedido_numero"], r["pedido_fecha"], r["cliente_nombre"],
                r["producto_nombre"], r["proveedor_nombre"] or r["factura_proveedor"] or "",
                r["folio_uuid"] or "",
                r["cantidad"],
                f"{r['costo_real']:.2f}",
                f"{r['precio_base']:.2f}",
                f"{r['descuento_proveedor']:.2f}",
                f"{r['markup_pct']*100:.1f}%",
                f"{r['precio_cliente']:.2f}",
                f"{r['margen_unit']:.2f}",
                f"{r['total_costo']:.2f}",
                f"{r['total_facturar']:.2f}",
            ])
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="reporte_citypantry.csv"
        )

    totales = {
        "costo": sum(r["total_costo"] for r in rows),
        "facturar": sum(r["total_facturar"] for r in rows),
    }
    totales["margen"] = totales["facturar"] - totales["costo"]
    totales["margen_pct"] = (totales["margen"] / totales["facturar"] * 100) if totales["facturar"] else 0

    return render_template("reporte.html", rows=rows, clientes=clientes,
                           pedidos_lista=pedidos_lista, totales=totales,
                           filtros={"cliente_id": cliente_id, "pedido_id": pedido_id,
                                    "fecha_ini": fecha_ini, "fecha_fin": fecha_fin})

@app.route("/api/productos/buscar")
@login_required
def api_buscar_productos():
    db = get_db()
    q = request.args.get("q", "")
    rows = db.execute("SELECT nombre, precio_base FROM productos WHERE nombre LIKE ? LIMIT 10",
                      (f"%{q}%",)).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── ARRANQUE ────────────────────────────────────────────────────────────────────

init_db()
migrate_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
