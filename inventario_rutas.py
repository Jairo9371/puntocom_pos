"""Rutas de almacén, conteos y aprobación de ajustes de existencias."""

from io import BytesIO

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from openpyxl import Workbook
from sqlalchemy.exc import IntegrityError

from datos import db, rows
from inventario_servicio import aprobar_ajuste, rechazar_ajuste, registrar_conteo, registrar_movimiento
from permisos import permiso_requerido, tiene_permiso


inventario_bp = Blueprint('inventario_api', __name__)


def _existencias():
    # Los costos solo se consultan cuando el perfil puede verlos.
    costo = ',p.costo' if tiene_permiso('costos.ver') else ''
    return rows(f'''SELECT vi.id_producto,vi.codigo,vi.nombre,vi.precio_venta,
                    vi.stock_minimo,vi.existencia_actual,p.estado,c.nombre AS categoria{costo}
                    FROM vista_inventario vi JOIN producto p USING(id_producto)
                    JOIN categoria c USING(id_categoria) ORDER BY vi.codigo''')


@inventario_bp.get('/inventario')
@permiso_requerido('inventario.ver')
def inventario():
    costo = ',mi.costo_unitario' if tiene_permiso('costos.ver') else ''
    movimientos = rows(f'''SELECT mi.id_movimiento_inventario,mi.fecha_hora,mi.tipo,
                          mi.cantidad,mi.motivo,p.codigo,p.nombre,u.nombre AS responsable{costo}
                          FROM movimiento_inventario mi JOIN producto p USING(id_producto)
                          LEFT JOIN usuario u ON u.id_usuario=mi.id_usuario
                          ORDER BY mi.id_movimiento_inventario DESC LIMIT 80''')
    return render_template('inventario.html', data=_existencias(), movimientos=movimientos)


def _guardar(operacion, mensaje, destino, *args):
    try:
        operacion(db.session, *args)
        db.session.commit()
        flash(mensaje, 'success')
    except PermissionError as error:
        db.session.rollback()
        abort(403, description=str(error))
    except (ValueError, IntegrityError) as error:
        db.session.rollback()
        flash(str(error) if isinstance(error, ValueError) else 'Los datos del inventario no son válidos.', 'danger')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Operación de inventario')
        flash('No fue posible guardar el cambio de inventario.', 'danger')
    return redirect(url_for(destino))


@inventario_bp.post('/inventario/movimiento')
@permiso_requerido('inventario.mover')
def inventario_movimiento():
    return _guardar(registrar_movimiento, 'Movimiento guardado.', 'inventario_api.inventario',
                    request.form, int(current_user.id))


@inventario_bp.post('/inventario/conteos')
@permiso_requerido('inventario.contar')
def conteo():
    return _guardar(registrar_conteo, 'Conteo registrado. La solicitud está pendiente de aprobación y no modifica existencias.',
                    'inventario_api.ajustes', request.form, int(current_user.id))


@inventario_bp.get('/inventario/ajustes')
@permiso_requerido('inventario.ver')
def ajustes():
    solicitudes = rows('''SELECT s.*,p.codigo,p.nombre,u.nombre AS solicitante,
                         a.nombre AS aprobador FROM solicitud_ajuste s
                         JOIN producto p USING(id_producto)
                         JOIN usuario u ON u.id_usuario=s.id_solicitante
                         LEFT JOIN usuario a ON a.id_usuario=s.id_aprobador
                         ORDER BY s.id_solicitud DESC LIMIT 200''')
    return render_template('ajustes_inventario.html', solicitudes=solicitudes)


@inventario_bp.post('/inventario/ajustes/<int:aid>/aprobar')
@permiso_requerido('inventario.aprobar')
def aprobar(aid):
    return _guardar(aprobar_ajuste, 'Conteo aprobado y ajuste aplicado.',
                    'inventario_api.ajustes', aid, int(current_user.id))


@inventario_bp.post('/inventario/ajustes/<int:aid>/rechazar')
@permiso_requerido('inventario.aprobar')
def rechazar(aid):
    return _guardar(rechazar_ajuste, 'Solicitud rechazada. Las existencias no se modificaron.',
                    'inventario_api.ajustes', aid, int(current_user.id), request.form.get('motivo', ''))


@inventario_bp.get('/inventario/excel')
@permiso_requerido('inventario.ver')
def excel():
    costos = tiene_permiso('costos.ver')
    libro = Workbook()
    hoja = libro.active
    hoja.title = 'Inventario'
    hoja.append(['Código', 'Producto', 'Categoría', 'Precio de venta', 'Existencia', 'Mínimo', 'Activo']
                + (['Costo unitario'] if costos else []))
    for producto in _existencias():
        hoja.append([producto['codigo'], producto['nombre'], producto['categoria'],
                     producto['precio_venta'], producto['existencia_actual'], producto['stock_minimo'],
                     'Sí' if producto['estado'] else 'No'] + ([producto['costo']] if costos else []))
        # Los nombres y códigos provienen de formularios: nunca ejecutar fórmulas.
        for celda in hoja[hoja.max_row]:
            if isinstance(celda.value, str):
                celda.data_type = 's'
    hoja.freeze_panes = 'A2'
    hoja.auto_filter.ref = hoja.dimensions
    salida = BytesIO()
    libro.save(salida)
    salida.seek(0)
    return send_file(salida, as_attachment=True, download_name='Inventario_PUNTO_COM.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
