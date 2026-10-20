
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for, abort
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from configuracion import TZ, cargar_configuracion
from datos import db, one, rows, value
from reportes_excel import generar_excel_ventas, resolver_rango_fechas
from validaciones import entero, money, solo_letras, validar_correo, validar_datos_gestion
from ventas_servicio import METODOS_PAGO, registrar_venta, anular_venta
from permisos import (ROLES, MENSAJE_ACCESO_DENEGADO, tiene_permiso, permiso_requerido,
                      exigir_permiso, destino_inicio, usuario_con_permiso)
from administracion_servicio import (RegistroNoEncontrado, preparar_listado, operar_registro,
                                    asignar_rol, bloquear_admin)

load_dotenv(Path(__file__).with_name('.env'))
app = Flask(__name__)
app.config.update(cargar_configuracion())
csrf = CSRFProtect()
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class Usuario(UserMixin):
    def __init__(self, row):
        self.id = str(row['id_usuario'])
        self.nombre = row['nombre']
        self.correo = row['correo']
        self.rol = row['rol']

@login_manager.user_loader
def load_user(user_id):
    try:
        row = one('''SELECT u.id_usuario,u.nombre,u.correo,r.nombre AS rol
                     FROM usuario u JOIN rol r USING(id_rol)
                     WHERE u.id_usuario=:id AND u.estado=TRUE''', id=int(user_id))
        return Usuario(row) if row and row['rol'] in ROLES else None
    except (ValueError, TypeError):
        return None

def numeric(key, minimum=0):
    try:
        result = money(request.form.get(key, '0'))
        if result < minimum: raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise ValueError(f'Valor inválido para {key}.')

def number(key, minimum=0):
    return entero(request.form.get(key, ''), minimum, key)

def required(key):
    x = request.form.get(key, '').strip()
    if not x: raise ValueError(f'El campo {key} es obligatorio.')
    return x



ADMIN = ('Administrador',)
INV = ('Administrador', 'Bodeguero')
VENTA = ('Administrador', 'Cajero')
METODOS_COMPRA = (*METODOS_PAGO, 'CRÉDITO')

@app.template_filter('q')
def quetzales(v):
    return 'Q {:,.2f}'.format(money(v))

@app.template_filter('fecha')
def fecha(v):
    return v.strftime('%d/%m/%Y %H:%M') if v else '-'

@app.context_processor
def global_vars():
    alertas = []
    total_alertas = 0

    if tiene_permiso('inventario.ver'):
        alertas = rows("""
            SELECT
                vi.codigo,
                vi.nombre,
                vi.existencia_actual,
                vi.stock_minimo
            FROM vista_inventario vi
            JOIN producto p
                ON p.id_producto = vi.id_producto
            WHERE p.estado = TRUE
              AND vi.existencia_actual <= vi.stock_minimo
            ORDER BY vi.existencia_actual ASC
            LIMIT 20
        """)

        total_alertas = len(alertas)
        if total_alertas == 20:
            total_alertas = value('''SELECT COUNT(*) FROM vista_inventario vi
                                    JOIN producto p USING(id_producto)
                                    WHERE p.estado=TRUE
                                      AND vi.existencia_actual <= vi.stock_minimo''')

    return {
        'shop_name': 'PUNTO COM',
        'clock_today': datetime.now(TZ).strftime('%d/%m/%Y'),
        'total_alertas': total_alertas,
        'alertas': alertas,
        'puede': tiene_permiso
    }


@app.errorhandler(403)
def no_permisos(_):
    if request.is_json:
        return jsonify(ok=False,error=MENSAJE_ACCESO_DENEGADO),403
    return render_template('message.html', titulo='Acceso denegado',
                           mensaje=MENSAJE_ACCESO_DENEGADO),403


@app.errorhandler(PermissionError)
def permiso_revocado(_):
    db.session.rollback()
    return no_permisos(None)

@app.errorhandler(404)
def no_existe(_):
    return render_template('message.html', titulo='No encontrado', mensaje='El registro o la página no existe.'), 404

@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for(destino_inicio(current_user.rol)))
    if request.method == 'POST':
        correo = request.form.get('correo','').strip().lower()
        row = one('''SELECT u.id_usuario,u.nombre,u.correo,u.hash_contrasena,r.nombre AS rol
                     FROM usuario u JOIN rol r USING(id_rol)
                     WHERE LOWER(u.correo)=:correo AND u.estado=TRUE''', correo=correo)
        if row and row['rol'] in ROLES and check_password_hash(row['hash_contrasena'], request.form.get('password','')):
            login_user(Usuario(row), remember=bool(request.form.get('remember')))
            return redirect(url_for(destino_inicio(current_user.rol)))
        flash('Correo o contraseña incorrectos.', 'danger')
    return render_template('login.html')

@app.post('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.', 'success')
    return redirect(url_for('login'))


@app.get('/')
@login_required
def dashboard():
    if current_user.rol != 'Administrador':
        return redirect(url_for(destino_inicio(current_user.rol)))

    # Estadísticas principales
    stats = one("""
        SELECT
            (SELECT COALESCE(SUM(total), 0)
             FROM venta
             WHERE fecha_hora::date = CURRENT_DATE
             AND estado = 'COMPLETADA') AS ventas,

            (SELECT COUNT(*)
             FROM venta
             WHERE fecha_hora::date = CURRENT_DATE
             AND estado = 'COMPLETADA') AS nventas,

            (SELECT COUNT(*)
             FROM producto
             WHERE estado = TRUE) AS productos,

            (SELECT COUNT(*)
             FROM vista_inventario vi
             JOIN producto p USING(id_producto)
             WHERE p.estado=TRUE
             AND vi.existencia_actual <= vi.stock_minimo) AS bajos,

            (SELECT COUNT(*)
             FROM cliente) AS clientes,

            (SELECT COALESCE(SUM(
                (dv.precio_unitario * dv.cantidad - dv.descuento)
                - (dv.costo_unitario_historico * dv.cantidad)
             ), 0)
             FROM detalle_venta dv
             JOIN venta v USING(id_venta)
             WHERE v.fecha_hora::date = CURRENT_DATE
             AND v.estado = 'COMPLETADA') AS ganancia
    """)

    # Últimas ventas realizadas
    recientes = rows("""
        SELECT
            v.numero_ticket,
            v.fecha_hora,
            v.total,
            c.nombre_razon_social
        FROM venta v
        JOIN cliente c USING(id_cliente)
        ORDER BY v.id_venta DESC
        LIMIT 8
    """)

    # Productos más vendidos
    top = rows("""
        SELECT
            p.nombre,
            SUM(d.cantidad) AS cantidad
        FROM detalle_venta d
        JOIN venta v USING(id_venta)
        JOIN producto p USING(id_producto)
        WHERE v.estado = 'COMPLETADA'
        GROUP BY p.id_producto, p.nombre
        ORDER BY cantidad DESC
        LIMIT 5
    """)

    # Ventas de los últimos 7 días
    dias = rows("""
        SELECT
            d::date AS fecha,
            COALESCE(SUM(v.total), 0) AS total
        FROM generate_series(
            CURRENT_DATE - INTERVAL '6 days',
            CURRENT_DATE,
            INTERVAL '1 day'
        ) d
        LEFT JOIN venta v
            ON v.fecha_hora::date = d::date
            AND v.estado = 'COMPLETADA'
        GROUP BY d
        ORDER BY d
    """)
    
    # Ventas mensuales del año actual
    ventas_mensuales = rows("""
        SELECT
            m.mes,
            COALESCE(SUM(v.total), 0) AS total
        FROM generate_series(1, 12) AS m(mes)
        LEFT JOIN venta v
            ON EXTRACT(MONTH FROM v.fecha_hora) = m.mes
            AND EXTRACT(YEAR FROM v.fecha_hora)
                = EXTRACT(YEAR FROM CURRENT_DATE)
            AND v.estado = 'COMPLETADA'
        GROUP BY m.mes
        ORDER BY m.mes
    """)

    meses = [
        "Enero", "Febrero", "Marzo", "Abril",
        "Mayo", "Junio", "Julio", "Agosto",
        "Septiembre", "Octubre", "Noviembre",
        "Diciembre"
    ]

    totales_mensuales = [
        float(fila["total"])
        for fila in ventas_mensuales
    ]

    # Enviar los resultados a la plantilla HTML
    return render_template(
        'dashboard.html',
        stats=stats,
        recientes=recientes,
        top=top,
        dias=dias,
        meses=meses,
        totales_mensuales=totales_mensuales
    )

@app.get('/productos')
@permiso_requerido('productos.ver')
def productos():
    buscar=request.args.get('q','').strip()
    categoria=request.args.get('categoria','')
    filtros={'q':f'%{buscar}%'}
    cond="(p.codigo ILIKE :q OR p.codigo_barras ILIKE :q OR p.nombre ILIKE :q)"
    if categoria.isdigit(): cond+=' AND p.id_categoria=:categoria'; filtros['categoria']=int(categoria)
    campos = 'p.*' if tiene_permiso('costos.ver') else ('p.id_producto,p.id_categoria,p.codigo,p.codigo_barras,'
              'p.nombre,p.marca,p.unidad,p.precio_venta,p.stock_minimo,p.estado')
    data=rows(f'''SELECT {campos},c.nombre AS categoria,vi.existencia_actual
                   FROM producto p JOIN categoria c USING(id_categoria)
                   JOIN vista_inventario vi USING(id_producto)
                   WHERE {cond} ORDER BY p.codigo''',**filtros)
    if tiene_permiso('productos.gestionar'):
        preparar_listado(db.session, 'productos', data, int(current_user.id))
    return render_template('productos.html', data=data, categorias=rows('SELECT * FROM categoria ORDER BY nombre'))

@app.route('/productos/nuevo',methods=['GET','POST'])
@app.route('/productos/<int:pid>/editar',methods=['GET','POST'])
@permiso_requerido('productos.gestionar')
def producto_form(pid=None):
    bloqueo = ' FOR UPDATE' if request.method == 'POST' else ''
    producto=one('SELECT * FROM producto WHERE id_producto=:id'+bloqueo,id=pid) if pid else None
    if pid and not producto: abort(404)
    if request.method=='POST':
        try:
            payload={'categoria':number('id_categoria',1),'codigo':required('codigo'),
                     'barras':request.form.get('codigo_barras','').strip() or None,
                     'nombre':required('nombre'),
                     'marca':solo_letras(request.form.get('marca',''), 'Marca', obligatorio=False),
                     'unidad':required('unidad'),'costo':numeric('costo'),
                     'precio':numeric('precio_venta'),'minimo':number('stock_minimo'),
                     'activo': bool(request.form.get('estado'))}
            usuario_con_permiso(db.session, int(current_user.id), 'productos.gestionar')
            cat = one('SELECT id_categoria,estado FROM categoria WHERE id_categoria=:id FOR SHARE',id=payload['categoria'])
            if not cat or (not cat['estado'] and (not producto or producto['id_categoria']!=payload['categoria'])):
                raise ValueError('Selecciona una categoría activa.')
            if pid:
                db.session.execute(text('''UPDATE producto SET id_categoria=:categoria,codigo=:codigo,
                  codigo_barras=:barras,nombre=:nombre,marca=:marca,unidad=:unidad,costo=:costo,
                  precio_venta=:precio,stock_minimo=:minimo,estado=:activo WHERE id_producto=:id'''),
                  {**payload,'id':pid})
            else:
                pid_nuevo=value('''INSERT INTO producto(id_categoria,codigo,codigo_barras,nombre,marca,unidad,
                          costo,precio_venta,stock_minimo,estado)
                          VALUES(:categoria,:codigo,:barras,:nombre,:marca,:unidad,:costo,:precio,:minimo,:activo)
                          RETURNING id_producto''',**payload)
                stock=number('stock_inicial')
                if stock:
                    db.session.execute(text('''INSERT INTO movimiento_inventario
                     (id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                     VALUES(:id,'ENTRADA',:cantidad,:costo,'Inventario inicial',:usuario)'''),
                     {'id':pid_nuevo,'cantidad':stock,'costo':payload['costo'],'usuario':int(current_user.id)})
            db.session.commit(); flash('Producto guardado correctamente.','success')
            return redirect(url_for('productos'))
        except (ValueError,IntegrityError) as e:
            db.session.rollback(); flash(str(e) if isinstance(e, ValueError) else 'Revisa la categoría o los códigos duplicados.', 'danger')
        except PermissionError:
            db.session.rollback(); abort(403)
        except Exception:
            db.session.rollback(); app.logger.exception('Guardar producto'); flash('Error al guardar producto.','danger')
    return render_template('producto_form.html',item=producto,categorias=rows(
        'SELECT * FROM categoria WHERE estado=TRUE OR id_categoria=:actual ORDER BY nombre',
        actual=producto['id_categoria'] if producto else -1))

@app.post('/productos/<int:pid>/estado')
@permiso_requerido('productos.gestionar')
def producto_estado(pid):
    return ejecutar_baja('productos',pid)


@app.post('/productos/<int:pid>/eliminar')
@permiso_requerido('productos.gestionar')
def producto_eliminar(pid):
    return ejecutar_baja('productos',pid,True)

# CRUD sencillo de categorías, clientes y proveedores usando formularios genéricos.
CRUD = {
 'categorias': {'tabla':'categoria','pk':'id_categoria','campos':['nombre','descripcion'],'titulo':'Categorías','roles':ADMIN,'permiso_ver':'productos.gestionar','permiso_crear':'productos.gestionar','permiso_editar':'productos.gestionar'},
 'clientes': {'tabla':'cliente','pk':'id_cliente','campos':['nombre_razon_social','nit_dpi','telefono','correo','direccion_fiscal'],'titulo':'Clientes','roles':ADMIN,'permiso_ver':'clientes.ver','permiso_crear':'clientes.crear','permiso_editar':'clientes.gestionar'},
 'proveedores': {'tabla':'proveedor','pk':'id_proveedor','campos':['razon_social','nit','contacto','telefono','correo','direccion'],'titulo':'Proveedores','roles':ADMIN,'permiso_ver':'proveedores.ver','permiso_crear':'proveedores.gestionar','permiso_editar':'proveedores.gestionar'},
}

@app.route('/gestion/<modulo>', methods=['GET', 'POST'])
@login_required
def gestion(modulo):
    config = CRUD.get(modulo)
    if not config:
        abort(404)

    exigir_permiso(config['permiso_ver'])
    if request.method == 'POST':
        permiso = config['permiso_editar'] if request.form.get('id','').strip() else config['permiso_crear']
        exigir_permiso(permiso)
        try:
            campos = config['campos']
            data = {c: request.form.get(c, '').strip() or None for c in campos}

            usuario_con_permiso(db.session, int(current_user.id), permiso)
            data = validar_datos_gestion(modulo, data)
            rid = request.form.get('id', '').strip()

            if rid:
                if not rid.isdigit():
                    raise ValueError('El identificador del registro no es válido.')
                existente = one(
                    f"SELECT * FROM {config['tabla']} WHERE {config['pk']}=:rid FOR UPDATE",
                    rid=int(rid)
                )
                if not existente:
                    raise ValueError('El registro que intentas editar no existe.')
                if modulo=='clientes' and (existente.get('nit_dpi') or '').upper()=='CF' and (
                    data.get('nit_dpi')!='CF' or data.get('nombre_razon_social','').casefold()!='consumidor final'):
                    raise ValueError('No se puede cambiar la identidad de Consumidor Final.')

            # Evitar duplicados de documentos, sin impedir editar el mismo registro.
            if modulo in ('clientes', 'proveedores'):
                documento = 'nit_dpi' if modulo == 'clientes' else 'nit'
                nit = data.get(documento)
                if nit:
                    # Dos solicitudes con el mismo documento deben consultar
                    # duplicados por turno, incluso cuando aún no existe una fila.
                    # El bloqueo termina automáticamente al confirmar o revertir.
                    db.session.execute(
                        text('SELECT pg_advisory_xact_lock(hashtextextended(:recurso, 0))'),
                        {'recurso': f'gestion:{modulo}:{nit}'}
                    )
                    duplicado = one(
                        f"SELECT {config['pk']} FROM {config['tabla']} "
                        f"WHERE UPPER({documento})=:documento "
                        f"AND {config['pk']}<>:actual LIMIT 1",
                        documento=nit,
                        actual=int(rid) if rid else -1
                    )
                    if duplicado:
                        raise ValueError('Ya existe un registro con ese NIT o DPI.')

            if rid:
                changes = ','.join(f'{c}=:{c}' for c in campos)
                db.session.execute(
                    text(f"UPDATE {config['tabla']} SET {changes} WHERE {config['pk']}=:rid"),
                    {**data, 'rid': int(rid)}
                )
            else:
                db.session.execute(
                    text(
                        f"INSERT INTO {config['tabla']}({','.join(campos)}) "
                        f"VALUES({','.join(':'+c for c in campos)})"
                    ), data
                )

            db.session.commit()
            flash('Registro guardado correctamente.', 'success')
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'danger')
        except IntegrityError:
            db.session.rollback()
            flash('No se pudo guardar: un dato está duplicado o no es válido.', 'danger')
        except PermissionError:
            db.session.rollback(); abort(403)
        except Exception:
            db.session.rollback()
            app.logger.exception('Error al guardar en gestión: %s', modulo)
            flash('No fue posible guardar el registro.', 'danger')
        return redirect(url_for('gestion', modulo=modulo))

    registros = rows(f"SELECT * FROM {config['tabla']} ORDER BY {config['pk']} DESC")
    preparar_listado(db.session,modulo,registros,int(current_user.id))
    return render_template('gestion.html', modulo=modulo, config=config, data=registros)

@app.get('/pos')
@permiso_requerido('ventas.registrar')
def pos():
    products=rows('''SELECT p.id_producto,p.codigo,p.codigo_barras,p.nombre,p.precio_venta,p.id_categoria,
                     vi.existencia_actual FROM producto p JOIN vista_inventario vi USING(id_producto)
                     WHERE p.estado=TRUE ORDER BY p.codigo''')
    clientes=rows('SELECT id_cliente,nombre_razon_social FROM cliente WHERE estado=TRUE ORDER BY nombre_razon_social')
    turno=one('''SELECT * FROM turno_caja WHERE id_usuario=:uid AND estado='ABIERTO'
                 ORDER BY id_turno DESC LIMIT 1''',uid=int(current_user.id))
    return render_template('pos.html',products=products,clientes=clientes,turno=turno)

@app.post('/pos/cobrar')
@permiso_requerido('ventas.registrar')
def cobrar():
    """Recalcula montos en el servidor y protege stock con bloqueos de filas."""
    try:
        resultado = registrar_venta(db.session, request.get_json(silent=True), int(current_user.id))
        db.session.commit()
        return jsonify(ok=True, **resultado)
    except PermissionError:
        db.session.rollback(); abort(403)
    except ValueError as e:
        db.session.rollback(); return jsonify(ok=False,error=str(e)),400
    except PermissionError:
        db.session.rollback(); abort(403)
    except Exception:
        db.session.rollback(); app.logger.exception('Cobrar venta'); return jsonify(ok=False,error='No se pudo guardar la venta.'),500

@app.get('/ventas')
@permiso_requerido('ventas.ver')
def ventas():
    filtro=request.args.get('q','').strip()
    condicion, parametros = alcance_ventas('v')
    data=rows(f'''SELECT v.*,c.nombre_razon_social,u.nombre AS cajero
                 FROM venta v JOIN cliente c USING(id_cliente) JOIN usuario u USING(id_usuario)
                 WHERE v.numero_ticket ILIKE :q {condicion} ORDER BY v.id_venta DESC LIMIT 250''',q=f'%{filtro}%',**parametros)
    return render_template('ventas.html',data=data)

@app.get('/ventas/<int:vid>/ticket')
@permiso_requerido('ventas.ver')
def ticket(vid):
    venta=one('''SELECT v.*,c.nombre_razon_social,c.nit_dpi,u.nombre AS cajero
             FROM venta v JOIN cliente c USING(id_cliente) JOIN usuario u USING(id_usuario)
             WHERE v.id_venta=:id''',id=vid)
    if not venta: abort(404)
    if not tiene_permiso('ventas.todas') and venta['id_usuario']!=int(current_user.id): abort(403)
    columnas = 'd.*' if tiene_permiso('costos.ver') else 'd.cantidad,d.precio_unitario,d.descuento'
    detalle=rows(f'''SELECT {columnas},p.nombre FROM detalle_venta d JOIN producto p USING(id_producto)
                   WHERE d.id_venta=:id ORDER BY d.id_detalle_venta''',id=vid)
    return render_template('ticket.html',venta=venta,detalle=detalle)

@app.get('/compras')
@permiso_requerido('proveedores.gestionar')
def compras():
    data=rows('''SELECT c.*,p.razon_social,u.nombre AS usuario
                FROM compra c JOIN proveedor p USING(id_proveedor) JOIN usuario u USING(id_usuario)
                ORDER BY c.id_compra DESC LIMIT 150''')
    return render_template('compras.html',data=data,proveedores=rows('SELECT * FROM proveedor WHERE estado=TRUE ORDER BY razon_social'),
                           productos=rows('SELECT id_producto,codigo,nombre,costo FROM producto WHERE estado=TRUE ORDER BY codigo'),
                           metodos_pago=METODOS_COMPRA)

@app.post('/compras/nueva')
@permiso_requerido('proveedores.gestionar')
def compra_nueva():
    try:
        proveedor=number('id_proveedor',1); producto=number('id_producto',1)
        cantidad=number('cantidad',1); costo=numeric('costo_unitario')
        metodo=required('metodo_pago')
        if metodo not in METODOS_COMPRA: raise ValueError('Método de pago inválido.')
        factura=request.form.get('numero_factura','').strip()
        usuario_con_permiso(db.session,int(current_user.id),'proveedores.gestionar')
        if not one('SELECT id_proveedor FROM proveedor WHERE id_proveedor=:id AND estado=TRUE FOR SHARE',id=proveedor): raise ValueError('Proveedor inactivo o inexistente.')
        p=one('SELECT id_producto FROM producto WHERE id_producto=:id AND estado=TRUE FOR UPDATE',id=producto)
        if not p: raise ValueError('Producto no encontrado.')
        total=money(costo*cantidad)
        cid=value('''INSERT INTO compra(id_proveedor,id_usuario,numero_factura,metodo_pago,total)
                     VALUES(:prov,:user,:factura,:metodo,:total) RETURNING id_compra''',
                  prov=proveedor,user=int(current_user.id),factura=factura,metodo=metodo,total=total)
        db.session.execute(text('''INSERT INTO detalle_compra(id_compra,id_producto,cantidad,costo_unitario)
                       VALUES(:compra,:producto,:cantidad,:costo)'''),{
                       'compra':cid,'producto':producto,'cantidad':cantidad,'costo':costo})
        db.session.execute(text('''INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                    VALUES(:producto,'ENTRADA',:cantidad,:costo,:motivo,:usuario)'''),{
                    'producto':producto,'cantidad':cantidad,'costo':costo,'motivo':f'Compra #{cid}','usuario':int(current_user.id)})
        db.session.execute(text('UPDATE producto SET costo=:costo WHERE id_producto=:id'),{'costo':costo,'id':producto})
        db.session.commit(); flash('Compra registrada y stock actualizado.','success')
    except (ValueError,IntegrityError) as e:
        db.session.rollback(); flash(str(e) if isinstance(e,ValueError) else 'Datos de compra incorrectos.','danger')
    except PermissionError:
        db.session.rollback(); abort(403)
    except Exception:
        db.session.rollback(); app.logger.exception('Compra'); flash('Error al guardar la compra.','danger')
    return redirect(url_for('compras'))

@app.route('/caja',methods=['GET','POST'])
@permiso_requerido('ventas.registrar')
def caja():
    if request.method=='POST':
        accion=request.form.get('accion')
        try:
            if accion == 'abrir':
                # Un SELECT de turnos vacíos no bloquea la ausencia de un turno.
                # Serializar por usuario impide dos aperturas simultáneas.
                # NO KEY UPDATE es compatible con las referencias FK de ventas.
                usuario_con_permiso(db.session,int(current_user.id),'ventas.registrar',bloqueo='NO KEY UPDATE')
            if accion!='abrir': usuario_con_permiso(db.session,int(current_user.id),'ventas.registrar')
            turno=one('''SELECT * FROM turno_caja WHERE id_usuario=:u AND estado='ABIERTO'
                         ORDER BY id_turno DESC LIMIT 1 FOR UPDATE''',u=int(current_user.id))
            if accion=='abrir':
                if turno: raise ValueError('Ya tienes un turno abierto.')
                db.session.execute(text('''INSERT INTO turno_caja(id_usuario,fondo_inicial,estado)
                          VALUES(:u,:fondo,'ABIERTO')'''),{'u':int(current_user.id),'fondo':numeric('fondo_inicial')})
            elif accion=='movimiento':
                if not turno: raise ValueError('Abre caja primero.')
                tipo=required('tipo')
                if tipo not in ('INGRESO','EGRESO'): raise ValueError('Tipo inválido.')
                db.session.execute(text('''INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
                            VALUES(:turno,:tipo,:concepto,:monto)'''),{'turno':turno['id_turno'],'tipo':tipo,
                            'concepto':required('concepto'),'monto':numeric('monto',Decimal('0.01'))})
            elif accion=='cerrar':
                if not turno: raise ValueError('No tienes un turno abierto.')
                contado=numeric('efectivo_contado')
                db.session.execute(text('''UPDATE turno_caja SET cierre=NOW(),estado='CERRADO',
                                    efectivo_contado=:contado WHERE id_turno=:id'''),
                                    {'contado':contado,'id':turno['id_turno']})
            else: raise ValueError('Acción no válida.')
            db.session.commit(); flash('Operación de caja guardada.','success')
        except (ValueError,IntegrityError) as e:
            db.session.rollback(); flash(str(e) if isinstance(e,ValueError) else 'Datos incorrectos.','danger')
        except PermissionError:
            db.session.rollback(); abort(403)
        except Exception:
            db.session.rollback(); app.logger.exception('Caja'); flash('No se pudo actualizar caja.','danger')
        return redirect(url_for('caja'))
    turno=one('''SELECT * FROM turno_caja WHERE id_usuario=:u AND estado='ABIERTO'
                 ORDER BY id_turno DESC LIMIT 1''',u=int(current_user.id))
    mov=[]; saldo=None
    if turno:
        mov=rows('SELECT * FROM movimiento_caja WHERE id_turno=:t ORDER BY fecha_hora DESC',t=turno['id_turno'])
        total_in=sum(m['monto'] for m in mov if m['tipo']=='INGRESO')
        total_out=sum(m['monto'] for m in mov if m['tipo']=='EGRESO')
        saldo=turno['fondo_inicial']+total_in-total_out
    condicion, parametros = ('',{}) if tiene_permiso('ventas.todas') else ('WHERE t.id_usuario=:propietario',{'propietario':int(current_user.id)})
    ultimos=rows(f'''SELECT t.*,u.nombre AS usuario FROM turno_caja t JOIN usuario u USING(id_usuario)
                    {condicion} ORDER BY t.id_turno DESC LIMIT 30''',**parametros)
    return render_template('caja.html',turno=turno,mov=mov,saldo=saldo,ultimos=ultimos)

def rango_reporte():
    return resolver_rango_fechas(request.args, hoy=datetime.now(TZ).date())


@app.get('/reportes')
@permiso_requerido('reportes.ventas')
def reportes():
    try:
        inicio, fin = rango_reporte()
    except ValueError as error:
        flash(str(error), 'danger')
        return redirect(url_for('reportes'))
    condicion, parametros = alcance_ventas('v')
    limites = {'inicio': inicio, 'fin': fin + timedelta(days=1),**parametros}
    resumen = one(f"""SELECT COUNT(*) AS cantidad,COALESCE(SUM(total),0) AS total FROM venta v
                     WHERE fecha_hora >= :inicio AND fecha_hora < :fin AND estado='COMPLETADA' {condicion}""", **limites)
    dias = rows(f"""SELECT fecha_hora::date AS fecha,SUM(total) AS total FROM venta v
                   WHERE fecha_hora >= :inicio AND fecha_hora < :fin AND estado='COMPLETADA' {condicion}
                   GROUP BY fecha_hora::date ORDER BY fecha""", **limites)
    productos = rows(f"""SELECT p.nombre,SUM(d.cantidad) AS cantidad,
                        SUM(d.precio_unitario*d.cantidad-d.descuento) AS total
                        FROM detalle_venta d JOIN venta v USING(id_venta) JOIN producto p USING(id_producto)
                        WHERE v.fecha_hora >= :inicio AND v.fecha_hora < :fin AND v.estado='COMPLETADA' {condicion}
                        GROUP BY p.id_producto,p.nombre ORDER BY cantidad DESC LIMIT 15""", **limites)
    return render_template('reportes.html', resumen=resumen, dias=dias, productos=productos,
                           desde=inicio.isoformat(), hasta=fin.isoformat())


@app.get('/reportes/excel', endpoint='reporte_excel')
@app.get('/reportes/ventas/excel', endpoint='exportar_ventas_excel')
@permiso_requerido('reportes.ventas')
def descargar_ventas_excel():
    """Ambas URLs históricas comparten filtros, consulta y generación."""
    try:
        inicio, fin = rango_reporte()
        condicion, parametros = alcance_ventas('v')
        ventas = rows(f"""SELECT v.numero_ticket,v.fecha_hora,c.nombre_razon_social AS cliente,
                         u.nombre AS cajero,v.metodo_pago,v.total,v.estado
                         FROM venta v JOIN cliente c USING(id_cliente) JOIN usuario u USING(id_usuario)
                         WHERE v.fecha_hora >= :inicio AND v.fecha_hora < :fin
                           AND v.estado='COMPLETADA' {condicion}
                         ORDER BY v.fecha_hora DESC,v.id_venta DESC""",
                      inicio=inicio, fin=fin + timedelta(days=1),**parametros)
        archivo = generar_excel_ventas(ventas, zona_horaria=TZ)
    except ValueError as error:
        flash(str(error), 'danger')
        return redirect(url_for('reportes'))
    return send_file(archivo, as_attachment=True, download_name='reporte_ventas_punto_com.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

def alcance_ventas(alias):
    if tiene_permiso('ventas.todas'):
        return '',{}
    return f' AND {alias}.id_usuario=:propietario',{'propietario':int(current_user.id)}


def ejecutar_baja(modulo,rid,eliminar=False):
    try:
        operar_registro(db.session,modulo,rid,int(current_user.id),
                         'eliminar' if eliminar else request.form.get('estado'))
        db.session.commit()
        flash('Registro eliminado.' if eliminar else 'Estado actualizado. El historial se conserva.','success')
    except RegistroNoEncontrado:
        db.session.rollback(); abort(404)
    except PermissionError:
        db.session.rollback(); abort(403)
    except (ValueError,IntegrityError) as error:
        db.session.rollback()
        flash(str(error) if isinstance(error,ValueError) else
              'El registro tiene relaciones o historial. Puedes desactivarlo.','danger')
    destino = 'productos' if modulo=='productos' else 'usuarios' if modulo=='usuarios' else 'gestion'
    return redirect(url_for(destino,**({'modulo':modulo} if destino=='gestion' else {})))


@app.post('/gestion/<modulo>/<int:rid>/estado')
@login_required
def gestion_estado(modulo,rid):
    config=CRUD.get(modulo)
    if not config: abort(404)
    exigir_permiso(config['permiso_editar'])
    return ejecutar_baja(modulo,rid)


@app.post('/gestion/<modulo>/<int:rid>/eliminar')
@login_required
def gestion_eliminar(modulo,rid):
    config=CRUD.get(modulo)
    if not config: abort(404)
    exigir_permiso(config['permiso_editar'])
    return ejecutar_baja(modulo,rid,True)


@app.post('/ventas/<int:vid>/anular')
@permiso_requerido('ventas.anular')
def venta_anular(vid):
    try:
        anular_venta(db.session,vid,int(current_user.id),request.form.get('motivo'))
        db.session.commit()
        flash('Venta anulada. Se conservó el comprobante y se registraron los movimientos de reversión.','success')
    except ValueError as error:
        db.session.rollback(); flash(str(error),'danger')
    return redirect(url_for('ventas'))


@app.get('/usuarios')
@permiso_requerido('usuarios.gestionar')
def usuarios():
    registros=rows("""SELECT u.id_usuario,u.id_rol,u.nombre,u.correo,u.estado,r.nombre AS rol
                       FROM usuario u JOIN rol r USING(id_rol) ORDER BY u.id_usuario""")
    preparar_listado(db.session,'usuarios',registros,int(current_user.id))
    return render_template('usuarios.html',data=registros,
        roles=rows("SELECT * FROM rol WHERE nombre IN ('Administrador','Cajero','Bodeguero') ORDER BY nombre"))


@app.post('/usuarios')
@permiso_requerido('usuarios.gestionar')
def usuario_crear():
    try:
        nombre=solo_letras(required('nombre'),'Nombre de usuario')
        correo=validar_correo(required('correo')); rol=number('id_rol',1); pw=required('password')
        if len(nombre)>100 or len(correo)>150: raise ValueError('El nombre o correo supera la longitud permitida.')
        if len(pw)<12: raise ValueError('La contraseña debe tener al menos 12 caracteres.')
        bloquear_admin(db.session,int(current_user.id))
        if value('SELECT nombre FROM rol WHERE id_rol=:id',id=rol) not in ROLES:
            raise ValueError('Selecciona un rol válido.')
        db.session.execute(text("""INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena,estado)
            VALUES(:rol,:nombre,:correo,:hash,TRUE)"""),
            {'rol':rol,'nombre':nombre,'correo':correo,'hash':generate_password_hash(pw)})
        db.session.commit(); flash('Usuario creado.','success')
    except (ValueError,IntegrityError) as error:
        db.session.rollback(); flash(str(error) if isinstance(error,ValueError) else 'Correo duplicado o rol inválido.','danger')
    return redirect(url_for('usuarios'))


@app.post('/usuarios/<int:uid>/estado')
@permiso_requerido('usuarios.gestionar')
def usuario_estado(uid):
    return ejecutar_baja('usuarios',uid)


@app.post('/usuarios/<int:uid>/eliminar')
@permiso_requerido('usuarios.gestionar')
def usuario_eliminar(uid):
    return ejecutar_baja('usuarios',uid,True)


@app.post('/usuarios/<int:uid>/rol')
@permiso_requerido('usuarios.gestionar')
def usuario_rol(uid):
    try:
        nuevo=asignar_rol(db.session,uid,number('id_rol',1),int(current_user.id))
        db.session.commit(); flash('Rol actualizado.','success')
        if uid==int(current_user.id):
            return redirect(url_for(destino_inicio(nuevo)))
    except RegistroNoEncontrado:
        db.session.rollback(); abort(404)
    except ValueError as error:
        db.session.rollback(); flash(str(error),'danger')
    return redirect(url_for('usuarios'))


@app.get('/configuracion')
@permiso_requerido('configuracion.gestionar')
def configuracion():
    return render_template('message.html',titulo='Configuración',mensaje='Nombre comercial: PUNTO COM · Moneda: Quetzal (Q) · País: Guatemala. La edición de datos fiscales e impresora estará disponible en una siguiente versión.')

@app.cli.command('crear-admin')
def crear_admin():
    """Comando CLI: flask --app app crear-admin"""
    from getpass import getpass
    nombre=solo_letras(input('Nombre del administrador: '), 'Nombre de usuario')
    correo=validar_correo(input('Correo electrónico: '))
    if len(nombre)>100 or len(correo)>150: raise ValueError('El nombre o correo supera la longitud permitida.')
    password=getpass('Contraseña (mínimo 12 caracteres): ')
    if not nombre or not correo or len(password)<12: raise ValueError('Nombre, correo y contraseña >=12 son obligatorios.')
    rol=value("SELECT id_rol FROM rol WHERE nombre='Administrador'")
    if not rol: raise ValueError('Primero inserta el rol Administrador en PostgreSQL.')
    db.session.execute(text('''INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena,estado)
                    VALUES(:rol,:nombre,:correo,:hash,TRUE)'''),{'rol':rol,'nombre':nombre,'correo':correo,'hash':generate_password_hash(password)})
    db.session.commit();print('Administrador registrado correctamente.')



@app.cli.command('migrar-accesos')
def migrar_accesos():
    """Migra roles, estados, responsables de inventario y solicitudes de ajuste."""
    from migraciones import aplicar_migracion_roles
    aplicar_migracion_roles(db.engine)
    print('Migración de accesos completada; los datos e historiales se conservaron.')


from inventario_rutas import inventario_bp
app.register_blueprint(inventario_bp)
# Compatibilidad con los nombres utilizados por las pantallas anteriores.
app.add_url_rule('/inventario',endpoint='inventario',build_only=True)
app.add_url_rule('/inventario/movimiento',endpoint='inventario_movimiento',build_only=True)


@app.before_request
def comprobar_acceso_antes_de_csrf():
    """Deniega permisos antes de procesar formularios o sus datos."""
    vista=app.view_functions.get(request.endpoint)
    if vista and request.endpoint not in ('login','static') and not current_user.is_authenticated:
        return login_manager.unauthorized()
    permiso=getattr(vista,'permiso',None)
    if request.endpoint in ('gestion','gestion_estado','gestion_eliminar'):
        config=CRUD.get((request.view_args or {}).get('modulo'))
        if config:
            exigir_permiso(config['permiso_ver'])
            permiso=config['permiso_ver']
            if request.method=='POST':
                permiso=(config['permiso_editar'] if request.endpoint!='gestion' or
                         request.form.get('id','').strip() else config['permiso_crear'])
    if permiso:
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        exigir_permiso(permiso)


csrf.init_app(app)


if __name__=='__main__':
    app.run(debug=app.config['DEBUG'])
