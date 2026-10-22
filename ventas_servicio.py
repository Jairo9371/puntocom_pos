"""Cobros y pagos manuales atómicos; el llamador decide commit/rollback.

CONFIRMADO representa la confirmación del responsable, sin consultar a un banco.
Las claves pertenecen al usuario y conservan la respuesta original del cobro.
"""

import hashlib
import json
import re
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text

from configuracion import TZ
from caja_servicio import egreso_bloqueado
from datos import one, value
from validaciones import entero, money
from permisos import MENSAJE_ACCESO_DENEGADO, tiene_permiso, usuario_con_permiso


METODOS_PAGO = ('EFECTIVO', 'TARJETA', 'TRANSFERENCIA')
ESTADOS_PAGO = ('PENDIENTE', 'CONFIRMADO', 'FALLIDO')
MAX_IMPORTE = Decimal('9999999999.99')
MAX_ENTERO = 2147483647


class IdempotenciaError(ValueError):
    """La misma clave ya identifica otra solicitud de cobro."""


def _entero_db(valor, etiqueta):
    numero = entero(valor, 1, etiqueta)
    if numero > MAX_ENTERO:
        raise ValueError(f'Cantidad inválida para {etiqueta}.')
    return numero


def _motivo(valor):
    if valor is None:
        return ''
    if not isinstance(valor, str):
        raise ValueError('Indica un motivo de texto válido.')
    return valor.strip()


def _solicitud(data):
    if not isinstance(data, dict):
        raise ValueError('Solicitud de cobro inválida.')
    carrito = data.get('items')
    if not isinstance(carrito, list) or not carrito or len(carrito) > 80:
        raise ValueError('Carrito vacío o inválido.')
    metodo = data.get('metodo')
    if metodo not in METODOS_PAGO:
        raise ValueError('Método de pago inválido.')
    pago = data.get('estado_pago', 'CONFIRMADO')
    if pago not in ('PENDIENTE', 'CONFIRMADO'):
        raise ValueError('El pago inicial debe ser pendiente o confirmado.')
    if metodo == 'EFECTIVO' and pago != 'CONFIRMADO':
        raise ValueError('Las ventas en efectivo requieren pago confirmado.')
    cliente_id = _entero_db(data.get('cliente_id'), 'cliente')
    efectivo = money(data.get('recibido', 0))
    if efectivo < 0 or efectivo > MAX_IMPORTE:
        raise ValueError('Monto recibido inválido.')
    cantidades, descuentos = {}, {}
    for item in carrito:
        if not isinstance(item, dict):
            raise ValueError('Renglón del carrito inválido.')
        pid = _entero_db(item.get('id'), 'producto')
        cantidad = _entero_db(item.get('cantidad'), 'cantidad')
        descuento = money(item.get('descuento', 0))
        if pid in cantidades or descuento < 0 or descuento > MAX_IMPORTE:
            raise ValueError('Productos duplicados o descuento inválido.')
        cantidades[pid], descuentos[pid] = cantidad, descuento
    return carrito, metodo, pago, cliente_id, efectivo, cantidades, descuentos


def _identificar_solicitud(data, clave_operacion):
    # HTTP exige clave; omitirla queda reservado a llamadas internas anteriores.
    clave = clave_operacion if clave_operacion is not None else data.get('clave_operacion')
    if clave is None:
        return None, None
    if not isinstance(clave, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', clave):
        raise ValueError('Clave de operación inválida (16 a 100 letras, números, guiones o guiones bajos).')
    if data.get('clave_operacion', clave) != clave:
        raise ValueError('Las claves de operación no coinciden.')
    # El hash cubre todo el payload, incluyendo campos ignorados del cliente.
    contenido = {k: v for k, v in data.items() if k != 'clave_operacion'}
    try:
        serializado = json.dumps(contenido, sort_keys=True, separators=(',', ':'),
                                 ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('Solicitud de cobro inválida.') from None
    return clave, hashlib.sha256(serializado.encode('utf-8')).hexdigest()


def _salida(session, producto, cantidad, numero, actor_id):
    session.execute(text('''INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
                       VALUES(:p,'SALIDA',:n,:costo,:motivo,:usuario)'''), {
        'p': producto['id_producto'], 'n': cantidad, 'costo': producto['costo'],
        'motivo': f'Venta {numero}', 'usuario': actor_id,
    })


def _ingreso(session, metodo, total, turno_id, numero):
    if metodo == 'EFECTIVO' and total > 0:
        session.execute(text('''INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
                           VALUES(:turno,'INGRESO',:concepto,:monto)'''), {
            'turno': turno_id, 'concepto': f'Venta {numero}', 'monto': total,
        })


def _producto_con_stock(session, pid, cantidad):
    producto = one('''SELECT id_producto,nombre,precio_venta,costo FROM producto
                      WHERE id_producto=:id AND estado=TRUE FOR UPDATE''', session=session, id=pid)
    if not producto:
        raise ValueError('Producto inactivo o inexistente.')
    saldo = value('SELECT existencia_actual FROM vista_inventario WHERE id_producto=:id',
                  session=session, id=pid)
    if saldo is None or cantidad > saldo:
        raise ValueError(f'Stock insuficiente para {producto["nombre"]}: {saldo or 0}.')
    return producto


def registrar_venta(session, data, usuario_id, clave_operacion=None):
    """Escribe una sola venta y sus efectos dentro de la transacción actual.

    El bloqueo asesor PostgreSQL serializa claves iguales hasta commit/rollback.
    Un reintento autorizado recupera la respuesta aunque ya se cerrara el turno.
    """
    carrito, metodo, pago, cliente_id, efectivo, cantidades, descuentos = _solicitud(data)
    clave, huella = _identificar_solicitud(data, clave_operacion)
    actor = usuario_con_permiso(session, usuario_id, 'ventas.registrar')
    if not tiene_permiso('descuentos.modificar', actor['rol']):
        campos = {'precio', 'precio_unitario', 'precio_venta', 'costo', 'costo_unitario',
                  'costo_unitario_historico', 'subtotal', 'total'}
        if any(descuentos.values()) or campos.intersection(data) or any(campos.intersection(i) for i in carrito):
            raise PermissionError(MENSAJE_ACCESO_DENEGADO)
    if clave:
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:operacion,0))'),
                        {'operacion': f'puntocom:cobro:{usuario_id}:{clave}'})
        previa = one('''SELECT hash_solicitud,resultado_cobro FROM venta
                       WHERE id_usuario=:usuario AND clave_operacion=:clave''',
                     session=session, usuario=usuario_id, clave=clave)
        if previa:
            if previa['hash_solicitud'] != huella:
                raise IdempotenciaError('La clave de operación ya se utilizó con otro cobro. Revisa el intento antes de crear otra venta.')
            resultado = previa['resultado_cobro']
            return json.loads(resultado) if isinstance(resultado, str) else dict(resultado)
    turno = one('''SELECT id_turno FROM turno_caja
                   WHERE id_usuario=:u AND estado='ABIERTO'
                   ORDER BY id_turno DESC LIMIT 1 FOR UPDATE''', session=session, u=usuario_id)
    if not turno:
        raise ValueError('Debes abrir caja para vender.')
    if not one('SELECT id_cliente FROM cliente WHERE id_cliente=:id AND estado=TRUE FOR SHARE',
               session=session, id=cliente_id):
        raise ValueError('Cliente inactivo o inexistente.')
    productos, total = [], Decimal('0.00')
    # Todas las operaciones bloquean productos en orden común.
    for pid in sorted(cantidades):
        producto = _producto_con_stock(session, pid, cantidades[pid])
        cantidad, descuento = cantidades[pid], descuentos[pid]
        bruto = money(producto['precio_venta']) * cantidad
        if descuento > bruto:
            raise ValueError('Descuento superior al importe del producto.')
        total += bruto - descuento
        productos.append((producto, cantidad, descuento))
    total = money(total)
    if total > MAX_IMPORTE:
        raise ValueError('El total supera el importe admitido por venta.')
    if metodo == 'EFECTIVO' and efectivo < total:
        raise ValueError('El efectivo recibido es insuficiente.')
    cambio = money(efectivo - total) if metodo == 'EFECTIVO' else money(0)
    estado = 'COMPLETADA' if pago == 'CONFIRMADO' else 'PENDIENTE'
    numero = 'PC-' + datetime.now(TZ).strftime('%Y%m%d') + '-' + secrets.token_hex(4).upper()
    vid = value('''INSERT INTO venta(id_cliente,id_usuario,id_turno,numero_ticket,metodo_pago,estado,total,
                       estado_pago,clave_operacion,hash_solicitud,recibido,cambio,id_confirmador,fecha_confirmacion)
                   VALUES(:cliente,:usuario,:turno,:ticket,:metodo,:estado,:total,
                       :pago,:clave,:hash,:recibido,:cambio,:confirmador,
                       CASE WHEN :pago='CONFIRMADO' THEN CURRENT_TIMESTAMP ELSE NULL END)
                   RETURNING id_venta''', session=session, cliente=cliente_id, usuario=usuario_id,
                turno=turno['id_turno'], ticket=numero, metodo=metodo, estado=estado, total=total,
                pago=pago, clave=clave, hash=huella, recibido=efectivo, cambio=cambio,
                confirmador=usuario_id if pago == 'CONFIRMADO' else None)
    for producto, cantidad, descuento in productos:
        session.execute(text('''INSERT INTO detalle_venta(id_venta,id_producto,cantidad,precio_unitario,
                           costo_unitario_historico,descuento,tasa_iva)
                           VALUES(:v,:p,:n,:precio,:costo,:desc,12)'''), {
            'v': vid, 'p': producto['id_producto'], 'n': cantidad,
            'precio': producto['precio_venta'], 'costo': producto['costo'], 'desc': descuento,
        })
        if pago == 'CONFIRMADO':
            _salida(session, producto, cantidad, numero, usuario_id)
    if pago == 'CONFIRMADO':
        _ingreso(session, metodo, total, turno['id_turno'], numero)
    resultado = {'id_venta': vid, 'numero': numero, 'total': str(total),
                 'cambio': str(cambio), 'estado_pago': pago, 'estado': estado}
    session.execute(text('UPDATE venta SET resultado_cobro=CAST(:resultado AS JSONB) WHERE id_venta=:id'),
                    {'resultado': json.dumps(resultado), 'id': vid})
    return resultado


def resolver_pago(session, vid, actor_id, estado_pago, motivo=''):
    """Confirma o falla un pendiente; repetir una resolución no duplica efectos.

    Se conserva el precio acordado y se toma el costo al entregar el inventario.
    El turno original debe continuar abierto para confirmar. Cerrado el turno,
    el pendiente puede cerrarse como FALLIDO, sin movimientos.
    """
    vid = _entero_db(vid, 'venta')
    if estado_pago not in ('CONFIRMADO', 'FALLIDO'):
        raise ValueError('Selecciona pago confirmado o fallido.')
    motivo = _motivo(motivo)
    if len(motivo) > 500 or (estado_pago == 'FALLIDO' and not motivo):
        raise ValueError('Indica el motivo del pago fallido (máximo 500 caracteres).')
    actor = usuario_con_permiso(session, actor_id, 'ventas.registrar')
    venta = one('SELECT * FROM venta WHERE id_venta=:id FOR UPDATE', session=session, id=vid)
    if not venta:
        raise ValueError('La venta no existe.')
    if venta['id_usuario'] != actor_id and not tiene_permiso('ventas.todas', actor['rol']):
        raise PermissionError(MENSAJE_ACCESO_DENEGADO)
    if venta['estado'] == 'ANULADA':
        raise ValueError('La venta está anulada y su pago no se puede modificar.')
    if venta['estado_pago'] == estado_pago:
        return {'id_venta': vid, 'estado_pago': estado_pago, 'estado': venta['estado']}
    if venta['estado_pago'] != 'PENDIENTE' or venta['estado'] != 'PENDIENTE':
        raise ValueError('El pago ya tiene un estado definitivo y no se puede modificar.')
    if estado_pago == 'CONFIRMADO':
        turno = one("""SELECT id_turno FROM turno_caja WHERE id_turno=:t
                       AND id_usuario=:u AND estado='ABIERTO' FOR UPDATE""",
                    session=session, t=venta['id_turno'], u=venta['id_usuario'])
        if not turno:
            raise ValueError('El turno original está cerrado. Revisa el pago; este pendiente solo puede cerrarse como fallido.')
        if not one('SELECT id_cliente FROM cliente WHERE id_cliente=:id AND estado=TRUE FOR SHARE',
                   session=session, id=venta['id_cliente']):
            raise ValueError('Cliente inactivo o inexistente.')
        detalles = session.execute(text('''SELECT id_detalle_venta,id_producto,cantidad,descuento
                              FROM detalle_venta WHERE id_venta=:id
                              ORDER BY id_producto,id_detalle_venta'''), {'id': vid}).mappings().all()
        for detalle in detalles:
            producto = _producto_con_stock(session, detalle['id_producto'], detalle['cantidad'])
            session.execute(text('''UPDATE detalle_venta SET costo_unitario_historico=:costo
                                   WHERE id_detalle_venta=:id'''),
                            {'costo': producto['costo'], 'id': detalle['id_detalle_venta']})
            _salida(session, producto, detalle['cantidad'], venta['numero_ticket'], actor_id)
        _ingreso(session, venta['metodo_pago'], venta['total'], venta['id_turno'], venta['numero_ticket'])
    estado = 'COMPLETADA' if estado_pago == 'CONFIRMADO' else 'FALLIDA'
    session.execute(text('''UPDATE venta SET estado=:estado,estado_pago=:pago,
        id_resolutor=:actor,fecha_resolucion=CURRENT_TIMESTAMP,motivo_pago=:motivo,
        id_confirmador=CASE WHEN :pago='CONFIRMADO' THEN :actor ELSE id_confirmador END,
        fecha_confirmacion=CASE WHEN :pago='CONFIRMADO' THEN CURRENT_TIMESTAMP ELSE fecha_confirmacion END
        WHERE id_venta=:id'''), {'estado': estado, 'pago': estado_pago, 'actor': actor_id,
                               'motivo': motivo or None, 'id': vid})
    return {'id_venta': vid, 'estado_pago': estado_pago, 'estado': estado}


def anular_venta(session, vid, actor_id, motivo):
    vid = _entero_db(vid, 'venta')
    usuario_con_permiso(session, actor_id, 'ventas.anular')
    motivo = _motivo(motivo)
    if not motivo or len(motivo) > 500:
        raise ValueError('Indica el motivo de anulación (máximo 500 caracteres).')
    venta = one('SELECT * FROM venta WHERE id_venta=:id FOR UPDATE', session=session, id=vid)
    if not venta:
        raise ValueError('La venta no existe.')
    if venta['estado'] != 'COMPLETADA' or venta['estado_pago'] != 'CONFIRMADO':
        raise ValueError('La venta ya está anulada o no admite anulación.')
    turno = None
    if venta['metodo_pago'] == 'EFECTIVO' and venta['total'] > 0:
        turno = one("""SELECT * FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO'
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
        egreso_bloqueado(session, turno, venta['total'], f'Anulación {venta["numero_ticket"]}: {motivo}')
    session.execute(text("""UPDATE venta SET estado='ANULADA',fecha_anulacion=CURRENT_TIMESTAMP,
                       id_anulador=:u,motivo_anulacion=:motivo WHERE id_venta=:id"""),
                    {'u': actor_id, 'motivo': motivo, 'id': vid})
