import os
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, flash, send_file
import io
import csv

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "citypantry-dev-key-2024")

DATABASE = os.environ.get("DATABASE_PATH", "pantry.db")

# ─── DB CONNECTION ──────────────────────────────────────────────────────────────

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

# ─── INIT DB ────────────────────────────────────────────────────────────────────

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
            clave_prod_serv TEXT
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
            descuento_proveedor REAL GENERATED ALWAYS AS (precio_base - costo_real) STORED,
            markup_pct REAL NOT NULL DEFAULT 0.15,
            precio_cliente REAL GENERATED ALWAYS AS (precio_base * (1 + markup_pct)) STORED,
            total_costo REAL GENERATED ALWAYS AS (costo_real * cantidad) STORED,
            total_facturar REAL GENERATED ALWAYS AS (precio_base * (1 + markup_pct) * cantidad) STORED,
            proveedor_nombre TEXT,
            notas TEXT
        );
    """)
    db.commit()
    db.close()

# ─── XML CFDI PARSER ────────────────────────────────────────────────────────────

CFDI_NS = {
    "3": "http://www.sat.gob.mx/cfd/3",
    "4": "http://www.sat.gob.mx/cfd/4",
}

def parse_cfdi(xml_content):
    """Parse Mexican CFDI XML and return structured data."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}")

    # detect namespace version
    tag = root.tag
    ns = None
    for ver, uri in CFDI_NS.items():
        if uri in tag:
            ns = {"cfdi": uri}
            break
    if ns is None:
        # try without namespace
        ns = {"cfdi": ""}

    def find(el, path):
        try:
            return el.find(path, ns)
        except Exception:
            return None

    def findall(el, path):
        try:
            return el.findall(path, ns)
        except Exception:
            return []

    def attr(el, name, default=""):
        if el is None:
            return default
        return el.get(name, default)

    emisor = find(root, "cfdi:Emisor")
    receptor = find(root, "cfdi:Receptor")

    proveedor_rfc = attr(emisor, "Rfc")
    proveedor_nombre = attr(emisor, "Nombre")
    fecha_str = attr(root, "Fecha") or attr(root, "fecha")

    # parse date
    try:
        fecha = datetime.fromisoformat(fecha_str[:10]).date().isoformat()
    except Exception:
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

            # costo real unitario = (importe - descuento) / cantidad
            costo_real = (importe - descuento) / cantidad if cantidad else (valor_unitario)

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

# ─── HELPERS ────────────────────────────────────────────────────────────────────

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

# ─── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/")
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
        LEFT JOIN pedido_items pi ON pi.pedido_id = p.id
        GROUP BY p.id
        ORDER BY p.fecha DESC LIMIT 5
    """).fetchall()
    return render_template("index.html", stats=stats, pedidos_recientes=pedidos_recientes)

# ── FACTURAS / XML ───────────────────────────────────────────────────────────────

@app.route("/facturas")
def facturas():
    db = get_db()
    rows = db.execute("""
        SELECT f.*, p.nombre as proveedor_nombre_cat,
               COUNT(c.id) as num_conceptos
        FROM facturas f
        LEFT JOIN proveedores p ON f.proveedor_id = p.id
        LEFT JOIN conceptos_factura c ON c.factura_id = f.id
        GROUP BY f.id ORDER BY f.fecha DESC
    """).fetchall()
    return render_template("facturas.html", facturas=rows)

@app.route("/facturas/upload", methods=["GET", "POST"])
def upload_factura():
    db = get_db()
    if request.method == "POST":
        files = request.files.getlist("xmlfiles")
        resultados = []
        for f in files:
            if not f.filename:
                continue
            try:
                content = f.read().decode("utf-8-sig")  # handle BOM
                data = parse_cfdi(content)

                # proveedor
                prov_id = get_or_create_proveedor(db, data["proveedor_rfc"], data["proveedor_nombre"])

                # factura
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

                # conceptos
                for c in data["conceptos"]:
                    db.execute("""
                        INSERT INTO conceptos_factura
                        (factura_id, descripcion, cantidad, unidad, valor_unitario,
                         descuento_factura, costo_real_unitario, importe_total, clave_prod_serv)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (factura_id, c["descripcion"], c["cantidad"], c["unidad"],
                          c["valor_unitario"], c["descuento_factura"], c["costo_real_unitario"],
                          c["importe_total"], c["clave_prod_serv"]))
                    # auto-create producto en catálogo si no existe
                    get_or_create_producto(db, c["descripcion"])

                db.commit()
                resultados.append({
                    "archivo": f.filename,
                    "status": "ok",
                    "proveedor": data["proveedor_nombre"],
                    "fecha": data["fecha"],
                    "conceptos": len(data["conceptos"])
                })
            except Exception as e:
                resultados.append({"archivo": f.filename, "status": "error", "mensaje": str(e)})

        return render_template("upload_resultado.html", resultados=resultados)

    return render_template("upload.html")

@app.route("/facturas/<int:factura_id>")
def factura_detail(factura_id):
    db = get_db()
    factura = db.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
    if not factura:
        flash("Factura no encontrada", "error")
        return redirect(url_for("facturas"))
    conceptos = db.execute("""
        SELECT cf.*, pi.id as asignado_item_id, pi.pedido_id,
               p.numero as pedido_numero, c.nombre as cliente_nombre
        FROM conceptos_factura cf
        LEFT JOIN pedido_items pi ON pi.concepto_id = cf.id
        LEFT JOIN pedidos p ON pi.pedido_id = p.id
        LEFT JOIN clientes c ON p.cliente_id = c.id
        WHERE cf.factura_id = ?
    """, (factura_id,)).fetchall()
    pedidos = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id
        WHERE p.status = 'abierto' ORDER BY p.fecha DESC
    """).fetchall()
    return render_template("factura_detail.html", factura=factura, conceptos=conceptos, pedidos=pedidos)

# ── PEDIDOS ──────────────────────────────────────────────────────────────────────

@app.route("/pedidos")
def pedidos():
    db = get_db()
    rows = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre,
               COUNT(pi.id) as num_items,
               COALESCE(SUM(pi.total_costo),0) as total_costo,
               COALESCE(SUM(pi.total_facturar),0) as total_facturar
        FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id
        LEFT JOIN pedido_items pi ON pi.pedido_id = p.id
        GROUP BY p.id ORDER BY p.fecha DESC
    """).fetchall()
    clientes = db.execute("SELECT * FROM clientes ORDER BY nombre").fetchall()
    return render_template("pedidos.html", pedidos=rows, clientes=clientes)

@app.route("/pedidos/nuevo", methods=["POST"])
def nuevo_pedido():
    db = get_db()
    numero = request.form["numero"]
    cliente_id = request.form["cliente_id"]
    fecha = request.form["fecha"]
    notas = request.form.get("notas", "")
    try:
        db.execute("INSERT INTO pedidos (numero, cliente_id, fecha, notas) VALUES (?,?,?,?)",
                   (numero, cliente_id, fecha, notas))
        db.commit()
        flash(f"Pedido {numero} creado", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("pedidos"))

@app.route("/pedidos/<int:pedido_id>")
def pedido_detail(pedido_id):
    db = get_db()
    pedido = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre, c.markup_default
        FROM pedidos p JOIN clientes c ON p.cliente_id = c.id
        WHERE p.id=?
    """, (pedido_id,)).fetchone()
    if not pedido:
        flash("Pedido no encontrado", "error")
        return redirect(url_for("pedidos"))
    items = db.execute("""
        SELECT pi.*, f.fecha as factura_fecha, f.proveedor_nombre
        FROM pedido_items pi
        LEFT JOIN conceptos_factura cf ON pi.concepto_id = cf.id
        LEFT JOIN facturas f ON cf.factura_id = f.id
        WHERE pi.pedido_id = ?
    """, (pedido_id,)).fetchall()
    # conceptos sin asignar (de facturas recientes)
    sin_asignar = db.execute("""
        SELECT cf.*, f.fecha, f.proveedor_nombre, f.id as factura_id
        FROM conceptos_factura cf
        JOIN facturas f ON cf.factura_id = f.id
        WHERE cf.id NOT IN (SELECT concepto_id FROM pedido_items WHERE concepto_id IS NOT NULL)
        ORDER BY f.fecha DESC, cf.descripcion
    """).fetchall()
    productos_cat = db.execute("SELECT * FROM productos ORDER BY nombre").fetchall()
    totales = {
        "costo": sum(i["total_costo"] for i in items),
        "facturar": sum(i["total_facturar"] for i in items),
        "margen": sum((i["total_facturar"] - i["total_costo"]) for i in items),
    }
    if totales["facturar"] > 0:
        totales["margen_pct"] = totales["margen"] / totales["facturar"] * 100
    else:
        totales["margen_pct"] = 0
    return render_template("pedido_detail.html", pedido=pedido, items=items,
                           sin_asignar=sin_asignar, productos_cat=productos_cat, totales=totales)

@app.route("/pedidos/<int:pedido_id>/agregar_concepto", methods=["POST"])
def agregar_concepto(pedido_id):
    db = get_db()
    concepto_id = request.form.get("concepto_id")
    pedido = db.execute("SELECT p.*, c.markup_default FROM pedidos p JOIN clientes c ON p.cliente_id=c.id WHERE p.id=?",
                        (pedido_id,)).fetchone()
    concepto = db.execute("SELECT cf.*, f.proveedor_nombre FROM conceptos_factura cf JOIN facturas f ON cf.factura_id=f.id WHERE cf.id=?",
                          (concepto_id,)).fetchone()
    if not concepto or not pedido:
        return jsonify({"error": "No encontrado"}), 404

    prod = get_or_create_producto(db, concepto["descripcion"])
    precio_base = prod["precio_base"] or concepto["costo_real_unitario"]
    markup = pedido["markup_default"]

    db.execute("""
        INSERT INTO pedido_items
        (pedido_id, concepto_id, producto_nombre, cantidad, precio_base, costo_real, markup_pct, proveedor_nombre)
        VALUES (?,?,?,?,?,?,?,?)
    """, (pedido_id, concepto_id, concepto["descripcion"], concepto["cantidad"],
          precio_base, concepto["costo_real_unitario"], markup, concepto["proveedor_nombre"]))
    db.commit()
    flash(f"Producto '{concepto['descripcion']}' agregado al pedido", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/agregar_manual", methods=["POST"])
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
def editar_item(pedido_id, item_id):
    db = get_db()
    precio_base = float(request.form["precio_base"])
    markup_pct = float(request.form["markup_pct"])
    cantidad = float(request.form["cantidad"])
    db.execute("""
        UPDATE pedido_items SET precio_base=?, markup_pct=?, cantidad=? WHERE id=? AND pedido_id=?
    """, (precio_base, markup_pct, cantidad, item_id, pedido_id))
    db.commit()
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/item/<int:item_id>/eliminar", methods=["POST"])
def eliminar_item(pedido_id, item_id):
    db = get_db()
    db.execute("DELETE FROM pedido_items WHERE id=? AND pedido_id=?", (item_id, pedido_id))
    db.commit()
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/cerrar", methods=["POST"])
def cerrar_pedido(pedido_id):
    db = get_db()
    db.execute("UPDATE pedidos SET status='cerrado' WHERE id=?", (pedido_id,))
    db.commit()
    flash("Pedido cerrado", "success")
    return redirect(url_for("pedido_detail", pedido_id=pedido_id))

@app.route("/pedidos/<int:pedido_id>/exportar")
def exportar_pedido(pedido_id):
    db = get_db()
    pedido = db.execute("""
        SELECT p.*, c.nombre as cliente_nombre FROM pedidos p
        JOIN clientes c ON p.cliente_id = c.id WHERE p.id=?
    """, (pedido_id,)).fetchone()
    items = db.execute("""
        SELECT pi.*, f.fecha as factura_fecha
        FROM pedido_items pi
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
                     "Precio Base Unit.", "Descuento Proveedor Unit.",
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

# ── CLIENTES ─────────────────────────────────────────────────────────────────────

@app.route("/clientes")
def clientes():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(p.id) as num_pedidos,
               COALESCE(SUM(pi.total_facturar),0) as total_facturado
        FROM clientes c
        LEFT JOIN pedidos p ON p.cliente_id = c.id
        LEFT JOIN pedido_items pi ON pi.pedido_id = p.id
        GROUP BY c.id ORDER BY c.nombre
    """).fetchall()
    return render_template("clientes.html", clientes=rows)

@app.route("/clientes/nuevo", methods=["POST"])
def nuevo_cliente():
    db = get_db()
    nombre = request.form["nombre"]
    markup = float(request.form.get("markup_default", 0.15))
    notas = request.form.get("notas", "")
    try:
        db.execute("INSERT INTO clientes (nombre, markup_default, notas) VALUES (?,?,?)",
                   (nombre, markup, notas))
        db.commit()
        flash(f"Cliente '{nombre}' creado", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("clientes"))

@app.route("/clientes/<int:cliente_id>/editar", methods=["POST"])
def editar_cliente(cliente_id):
    db = get_db()
    markup = float(request.form.get("markup_default", 0.15))
    notas = request.form.get("notas", "")
    db.execute("UPDATE clientes SET markup_default=?, notas=? WHERE id=?", (markup, notas, cliente_id))
    db.commit()
    flash("Cliente actualizado", "success")
    return redirect(url_for("clientes"))

# ── CATÁLOGO DE PRODUCTOS ────────────────────────────────────────────────────────

@app.route("/catalogo")
def catalogo():
    db = get_db()
    q = request.args.get("q", "")
    if q:
        rows = db.execute("""
            SELECT p.*,
                   (SELECT COUNT(*) FROM pedido_items pi WHERE pi.producto_nombre = p.nombre) as veces_pedido,
                   (SELECT MAX(f.fecha) FROM pedido_items pi
                    JOIN conceptos_factura cf ON pi.concepto_id = cf.id
                    JOIN facturas f ON cf.factura_id = f.id
                    WHERE pi.producto_nombre = p.nombre) as ultima_compra
            FROM productos p
            WHERE p.nombre LIKE ?
            ORDER BY p.nombre
        """, (f"%{q}%",)).fetchall()
    else:
        rows = db.execute("""
            SELECT p.*,
                   (SELECT COUNT(*) FROM pedido_items pi WHERE pi.producto_nombre = p.nombre) as veces_pedido,
                   (SELECT MAX(f.fecha) FROM pedido_items pi
                    JOIN conceptos_factura cf ON pi.concepto_id = cf.id
                    JOIN facturas f ON cf.factura_id = f.id
                    WHERE pi.producto_nombre = p.nombre) as ultima_compra
            FROM productos p ORDER BY p.nombre
        """).fetchall()
    return render_template("catalogo.html", productos=rows, q=q)

@app.route("/catalogo/<int:producto_id>/editar", methods=["POST"])
def editar_producto(producto_id):
    db = get_db()
    precio_base_nuevo = request.form.get("precio_base")
    categoria = request.form.get("categoria", "")
    notas = request.form.get("notas", "")

    if precio_base_nuevo:
        precio_base_nuevo = float(precio_base_nuevo)
        # guardar historial
        prod = db.execute("SELECT precio_base FROM productos WHERE id=?", (producto_id,)).fetchone()
        if prod and prod["precio_base"] != precio_base_nuevo:
            db.execute("INSERT INTO precio_base_historial (producto_id, precio_base) VALUES (?,?)",
                       (producto_id, precio_base_nuevo))
        db.execute("UPDATE productos SET precio_base=?, categoria=?, notas=?, actualizado_en=CURRENT_TIMESTAMP WHERE id=?",
                   (precio_base_nuevo, categoria, notas, producto_id))
    else:
        db.execute("UPDATE productos SET categoria=?, notas=? WHERE id=?",
                   (categoria, notas, producto_id))
    db.commit()
    flash("Producto actualizado", "success")
    return redirect(url_for("catalogo"))

# ── CONSULTA EN CAMPO ────────────────────────────────────────────────────────────

@app.route("/consulta")
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

@app.route("/api/productos/buscar")
def api_buscar_productos():
    db = get_db()
    q = request.args.get("q", "")
    rows = db.execute("SELECT nombre, precio_base FROM productos WHERE nombre LIKE ? LIMIT 10",
                      (f"%{q}%",)).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── MAIN ────────────────────────────────────────────────────────────────────────
# ─── INIT DB AL ARRANCAR ────────────────────────────────────────────────────────
init_db()
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
