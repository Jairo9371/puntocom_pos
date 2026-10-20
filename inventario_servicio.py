"""Movimientos y conteos transaccionales: quien llama confirma o revierte.

Todos los cambios de existencias bloquean primero el producto, igual que las
ventas. Los conteos conservan una fotografía y requieren aprobación explícita.
"""

from sqlalchemy import text

from datos import one, value
from permisos import usuario_con_permiso
from validaciones import entero, solo_letras


MAX_ENTERO = 2_147_483_647


def _actor(session, usuario_id, permiso):
    """Revalida y retiene al responsable durante la transacción de escritura."""
    return usuario_con_permiso(session, usuario_id, permiso)['id_usuario']


def _cantidad(valor, minimo, etiqueta):
    cantidad = entero(valor, minimo, etiqueta)
    if cantidad > MAX_ENTERO:
        raise ValueError(f'Cantidad fuera del rango permitido para {etiqueta}.')
    return cantidad


def _producto(session, producto_id):
    producto = one('''SELECT id_producto,costo,estado FROM producto
                      WHERE id_producto=:id FOR UPDATE''', session=session, id=producto_id)
    if not producto or not producto['estado']:
        raise ValueError('El producto no existe o está inactivo.')
    existencia = value('''SELECT existencia_actual FROM vista_inventario
                          WHERE id_producto=:id''', session=session, id=producto_id)
    return producto, int(existencia or 0)


def _movimiento(session, producto, tipo, cantidad, motivo, usuario_id):
    return value('''INSERT INTO movimiento_inventario
                    (id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                    VALUES(:producto,:tipo,:cantidad,:costo,:motivo,:usuario)
                    RETURNING id_movimiento_inventario''', session=session,
                 producto=producto['id_producto'], tipo=tipo, cantidad=cantidad,
                 costo=producto['costo'], motivo=motivo, usuario=usuario_id)


def registrar_movimiento(session, data, usuario_id):
    usuario_id = _actor(session, usuario_id, 'inventario.mover')
    producto_id = entero(data.get('id_producto'), 1, 'producto')
    cantidad = _cantidad(data.get('cantidad'), 1, 'movimiento')
    tipo = data.get('tipo')
    if tipo not in ('ENTRADA', 'SALIDA'):
        raise ValueError('Tipo de movimiento inválido.')
    motivo = solo_letras(data.get('motivo'), 'Motivo de inventario')
    producto, existencia = _producto(session, producto_id)
    if tipo == 'SALIDA' and cantidad > existencia:
        raise ValueError(f'Existencia insuficiente: {existencia}.')
    return _movimiento(session, producto, tipo, cantidad, motivo, usuario_id)


def registrar_conteo(session, data, usuario_id):
    usuario_id = _actor(session, usuario_id, 'inventario.contar')
    producto_id = entero(data.get('id_producto'), 1, 'producto')
    contada = _cantidad(data.get('cantidad_contada'), 0, 'conteo físico')
    motivo = solo_letras(data.get('motivo'), 'Motivo del conteo')
    _, existencia = _producto(session, producto_id)
    if existencia < 0 or existencia > MAX_ENTERO:
        raise ValueError('La existencia actual está fuera del rango admitido para un conteo.')
    # Incluso un conteo coincidente conserva responsable, fecha y aprobación.
    return value('''INSERT INTO solicitud_ajuste
                    (id_producto,id_solicitante,existencia_sistema,cantidad_contada,diferencia,motivo)
                    VALUES(:producto,:usuario,:existencia,:contada,:diferencia,:motivo)
                    RETURNING id_solicitud''', session=session,
                 producto=producto_id, usuario=usuario_id, existencia=existencia,
                 contada=contada, diferencia=contada - existencia, motivo=motivo)


def _solicitud_pendiente(session, solicitud_id):
    solicitud_id = entero(solicitud_id, 1, 'solicitud')
    solicitud = one('''SELECT * FROM solicitud_ajuste
                       WHERE id_solicitud=:id FOR UPDATE''', session=session, id=solicitud_id)
    if not solicitud:
        raise ValueError('La solicitud de ajuste no existe.')
    if solicitud['estado'] != 'PENDIENTE':
        raise ValueError('La solicitud ya fue resuelta y no puede aplicarse nuevamente.')
    return solicitud


def aprobar_ajuste(session, solicitud_id, usuario_id):
    usuario_id = _actor(session, usuario_id, 'inventario.aprobar')
    solicitud = _solicitud_pendiente(session, solicitud_id)
    producto, existencia = _producto(session, solicitud['id_producto'])
    if existencia != solicitud['existencia_sistema']:
        raise ValueError('Las existencias cambiaron después del conteo. Rechaza esta solicitud y registra un nuevo conteo.')
    diferencia = solicitud['diferencia']
    if diferencia != solicitud['cantidad_contada'] - solicitud['existencia_sistema']:
        raise ValueError('La diferencia de la solicitud no coincide con el conteo registrado.')
    if existencia + diferencia < 0:
        raise ValueError('El ajuste produciría existencias negativas.')
    movimiento_id = None
    if diferencia:
        movimiento_id = _movimiento(
            session, producto, 'ENTRADA' if diferencia > 0 else 'SALIDA',
            abs(diferencia), f'Ajuste aprobado de conteo {solicitud["id_solicitud"]}: {solicitud["motivo"]}',
            usuario_id,
        )
    session.execute(text('''UPDATE solicitud_ajuste SET estado='APROBADA',
                           id_aprobador=:usuario,fecha_resolucion=CURRENT_TIMESTAMP,
                           id_movimiento=:movimiento WHERE id_solicitud=:id'''),
                    {'usuario': usuario_id, 'movimiento': movimiento_id, 'id': solicitud['id_solicitud']})
    return movimiento_id


def rechazar_ajuste(session, solicitud_id, usuario_id, motivo=''):
    usuario_id = _actor(session, usuario_id, 'inventario.aprobar')
    motivo = solo_letras(motivo, 'Motivo del rechazo', obligatorio=False)
    solicitud = _solicitud_pendiente(session, solicitud_id)
    session.execute(text('''UPDATE solicitud_ajuste SET estado='RECHAZADA',
                           id_aprobador=:usuario,fecha_resolucion=CURRENT_TIMESTAMP,
                           motivo_resolucion=:motivo WHERE id_solicitud=:id'''),
                    {'usuario': usuario_id, 'motivo': motivo or None, 'id': solicitud['id_solicitud']})
