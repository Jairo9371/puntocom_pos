"""Reglas del cobro independientes de HTTP. La ruta confirma o revierte la venta."""

import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from configuracion import TZ
from datos import one, value
from validaciones import entero, money
from permisos import MENSAJE_ACCESO_DENEGADO, tiene_permiso, usuario_con_permiso


METODOS_PAGO = ('EFECTIVO', 'TARJETA', 'TRANSFERENCIA')
MAX_IMPORTE = Decimal('9999999999.99')  # NUMERIC(12, 2) del esquema.


def registrar_venta(session, data, usuario_id):
    """Escribe una venta completa sin commit: un fallo debe revertir el conjunto."""
    if not isinstance(data, dict):
        raise ValueError('Solicitud de cobro inválida.')
    carrito = data.get('items')
    if not isinstance(carrito, list) or not carrito or len(carrito) > 80:
        raise ValueError('Carrito vacío o inválido.')
    metodo = data.get('metodo')
    if metodo not in METODOS_PAGO:
        raise ValueError('Método de pago inválido.')
    cliente_id = entero(data.get('cliente_id'), 1, 'cliente')
    efectivo = money(data.get('recibido', 0))
    if efectivo < 0 or efectivo > MAX_IMPORTE:
        raise ValueError('Monto recibido inválido.')

    cantidades = {}
    descuentos = {}
    for item in carrito:
        if not isinstance(item, dict):
            raise ValueError('Renglón del carrito inválido.')
        pid = entero(item.get('id'), 1, 'producto')
        cantidad = entero(item.get('cantidad'), 1, 'cantidad')
        descuento = money(item.get('descuento', 0))
        if pid in cantidades or descuento < 0 or descuento > MAX_IMPORTE:
            raise ValueError('Productos duplicados o descuento inválido.')
        cantidades[pid] = cantidad
        descuentos[pid] = descuento

    actor = usuario_con_permiso(session, usuario_id, 'ventas.registrar')
    if not tiene_permiso('descuentos.modificar', actor['rol']):
        campos = {'precio', 'precio_unitario', 'precio_venta', 'costo', 'costo_unitario',
                  'costo_unitario_historico', 'subtotal', 'total'}
        if any(descuentos.values()) or campos.intersection(data) or any(campos.intersection(i) for i in carrito):
            raise PermissionError(MENSAJE_ACCESO_DENEGADO)
    turno = one('''SELECT id_turno FROM turno_caja
                   WHERE id_usuario=:u AND estado='ABIERTO'
                   ORDER BY id_turno DESC LIMIT 1 FOR UPDATE''', session=session, u=usuario_id)
    if not turno:
        raise ValueError('Debes abrir caja para vender.')
    if not one('SELECT id_cliente FROM cliente WHERE id_cliente=:id AND estado=TRUE FOR SHARE',
               session=session, id=cliente_id):
        raise ValueError('Cliente inactivo o inexistente.')

    productos = []
    total = Decimal('0.00')
    # Todas las ventas bloquean productos en el mismo orden para reducir interbloqueos.
    for pid in sorted(cantidades):
        producto = one('''SELECT id_producto,nombre,precio_venta,costo FROM producto
                          WHERE id_producto=:id AND estado=TRUE FOR UPDATE''', session=session, id=pid)
        if not producto:
            raise ValueError('Producto inactivo o inexistente.')
        saldo = value('SELECT existencia_actual FROM vista_inventario WHERE id_producto=:id',
                      session=session, id=pid)
        cantidad = cantidades[pid]
        descuento = descuentos[pid]
        bruto = money(producto['precio_venta']) * cantidad
        if saldo is None or cantidad > saldo:
            raise ValueError(f'Stock insuficiente para {producto["nombre"]}: {saldo or 0}.')
        if descuento > bruto:
            raise ValueError('Descuento superior al importe del producto.')
        total += bruto - descuento
        productos.append((producto, cantidad, descuento))
    total = money(total)
    if total > MAX_IMPORTE:
        raise ValueError('El total supera el importe admitido por venta.')
    if metodo == 'EFECTIVO' and efectivo < total:
        raise ValueError('El efectivo recibido es insuficiente.')

    numero = 'PC-' + datetime.now(TZ).strftime('%Y%m%d') + '-' + secrets.token_hex(4).upper()
    vid = value('''INSERT INTO venta(id_cliente,id_usuario,id_turno,numero_ticket,metodo_pago,estado,total)
                   VALUES(:cliente,:usuario,:turno,:ticket,:metodo,'COMPLETADA',:total) RETURNING id_venta''',
                session=session, cliente=cliente_id, usuario=usuario_id, turno=turno['id_turno'],
                ticket=numero, metodo=metodo, total=total)
    for producto, cantidad, descuento in productos:
        session.execute(text('''INSERT INTO detalle_venta(id_venta,id_producto,cantidad,precio_unitario,
                           costo_unitario_historico,descuento,tasa_iva)
                           VALUES(:v,:p,:n,:precio,:costo,:desc,12)'''), {
            'v': vid, 'p': producto['id_producto'], 'n': cantidad,
            'precio': producto['precio_venta'], 'costo': producto['costo'], 'desc': descuento,
        })
        session.execute(text('''INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                           VALUES(:p,'SALIDA',:n,:costo,:motivo,:usuario)'''), {
            'p': producto['id_producto'], 'n': cantidad, 'costo': producto['costo'], 'motivo': f'Venta {numero}',
            'usuario': usuario_id,
        })
    # Una venta con descuento del 100% no representa un ingreso de efectivo.
    if metodo == 'EFECTIVO' and total > 0:
        session.execute(text('''INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
                           VALUES(:turno,'INGRESO',:concepto,:monto)'''), {
            'turno': turno['id_turno'], 'concepto': f'Venta {numero}', 'monto': total,
        })
    return {
        'id_venta': vid, 'numero': numero, 'total': str(total),
        'cambio': str(money(efectivo - total) if metodo == 'EFECTIVO' else money(0)),
    }


def anular_venta(session, vid, actor_id, motivo):
    usuario_con_permiso(session, actor_id, 'ventas.anular')
    motivo = (motivo or '').strip()
    if not motivo or len(motivo) > 500:
        raise ValueError('Indica el motivo de anulación (máximo 500 caracteres).')
    venta = one('SELECT * FROM venta WHERE id_venta=:id FOR UPDATE', session=session, id=vid)
    if not venta:
        raise ValueError('La venta no existe.')
    if venta['estado'] != 'COMPLETADA':
        raise ValueError('La venta ya está anulada o no admite anulación.')
    turno = None
    if venta['metodo_pago'] == 'EFECTIVO' and venta['total'] > 0:
        turno = one("""SELECT id_turno FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO'
                       ORDER BY id_turno DESC LIMIT 1 FOR UPDATE""", session=session, id=actor_id)
        if not turno:
            raise ValueError('Abre tu caja para registrar el reembolso en efectivo.')
    detalles = session.execute(text('''SELECT id_producto,cantidad,
                      costo_unitario_historico AS costo FROM detalle_venta
                      WHERE id_venta=:id ORDER BY id_producto,id_detalle_venta'''), {'id': vid}).mappings().all()
    for detalle in detalles:
        one('SELECT id_producto FROM producto WHERE id_producto=:id FOR UPDATE', session=session, id=detalle['id_producto'])
        session.execute(text('''INSERT INTO movimiento_inventario
            (id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
            VALUES(:p,'ENTRADA',:n,:costo,:motivo,:u)'''), {
                'p': detalle['id_producto'], 'n': detalle['cantidad'], 'costo': detalle['costo'],
                'motivo': f'Anulación {venta["numero_ticket"]}: {motivo}', 'u': actor_id})
    if turno:
        session.execute(text('''INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
            VALUES(:t,'EGRESO',:concepto,:monto)'''), {'t': turno['id_turno'],
            'concepto': f'Anulación {venta["numero_ticket"]}: {motivo}'[:255], 'monto': venta['total']})
    session.execute(text("""UPDATE venta SET estado='ANULADA',fecha_anulacion=NOW(),
                       id_anulador=:u,motivo_anulacion=:motivo WHERE id_venta=:id"""),
                    {'u': actor_id, 'motivo': motivo, 'id': vid})
