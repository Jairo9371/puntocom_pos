"""Compras y abonos atómicos, con bloqueos e idempotencia de operaciones."""

import hashlib
import json
import re
from decimal import Decimal

from sqlalchemy import text

from caja_servicio import MAX_IMPORTE, egreso_bloqueado, importe, turno_propio_bloqueado
from datos import one, value
from permisos import usuario_con_permiso
from validaciones import entero, money


METODOS_COMPRA = ('EFECTIVO', 'TARJETA', 'TRANSFERENCIA', 'CRÉDITO')


def _identificador(valor, etiqueta):
    numero = entero(valor, 1, etiqueta)
    if numero > 2147483647:
        raise ValueError(f'{etiqueta.capitalize()} fuera del rango permitido.')
    return numero


def _clave(datos, obligatoria=False):
    clave = datos.get('clave_operacion')
    if clave in (None, '') and not obligatoria:
        return None
    if not isinstance(clave, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', clave):
        raise ValueError('Clave de operación inválida; actualiza el formulario.')
    return clave


def _huella(datos):
    return hashlib.sha256(json.dumps(datos, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _bloquear_operacion(session, usuario_id, clave, tipo):
    if clave:
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
                        {'key': f'{tipo}:{usuario_id}:{clave}'})


def _origen(datos, metodo, legacy=False):
    origen = datos.get('origen_efectivo')
    if metodo != 'EFECTIVO':
        # El selector del formulario conserva el valor al cambiar el método.
        return None
    if not origen and legacy:
        return 'EXTERNO'
    if origen not in ('CAJA', 'EXTERNO'):
        raise ValueError('Indica si el efectivo sale de tu caja o de fondos externos.')
    return origen


def _renglones(datos):
    items = datos.get('items')
    if items is None and hasattr(datos, 'getlist'):
        productos = datos.getlist('id_producto')
        cantidades = datos.getlist('cantidad')
        costos = datos.getlist('costo_unitario')
        if len(productos) != len(cantidades) or len(productos) != len(costos):
            raise ValueError('Los renglones de la compra están incompletos.')
        items = [dict(id_producto=p, cantidad=n, costo_unitario=c)
                 for p, n, c in zip(productos, cantidades, costos)]
    if items is None:
        items = [dict(id_producto=datos.get('id_producto'), cantidad=datos.get('cantidad'),
                      costo_unitario=datos.get('costo_unitario'))]
    if not isinstance(items, list) or not items or len(items) > 80:
        raise ValueError('Agrega entre 1 y 80 artículos a la compra.')
    normalizados = []
    vistos = set()
    total = Decimal('0.00')
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Renglón de compra inválido.')
        producto = _identificador(item.get('id_producto'), 'producto')
        cantidad = _identificador(item.get('cantidad'), 'cantidad')
        costo = importe(item.get('costo_unitario'), 'Costo unitario')
        if producto in vistos:
            raise ValueError('Cada producto debe aparecer una sola vez en la compra.')
        vistos.add(producto)
        total += costo * cantidad
        if total > MAX_IMPORTE:
            raise ValueError('El total de compra supera el importe permitido.')
        normalizados.append({'id_producto': producto, 'cantidad': cantidad,
                             'costo_unitario': str(costo)})
    return sorted(normalizados, key=lambda i: i['id_producto']), money(total)


def _pago(session, compra_id, usuario_id, monto, metodo, origen, turno,
          clave=None, huella=None):
    movimiento = None
    if origen == 'CAJA':
        movimiento = egreso_bloqueado(session, turno, monto, f'Pago compra #{compra_id}')
    return value('''INSERT INTO pago_compra(id_compra,id_usuario,monto,metodo_pago,
        origen_efectivo,id_movimiento_caja,clave_operacion,huella_operacion)
        VALUES(:compra,:usuario,:monto,:metodo,:origen,:movimiento,:clave,:huella)
        RETURNING id_pago_compra''', session=session, compra=compra_id, usuario=usuario_id,
        monto=monto, metodo=metodo, origen=origen, movimiento=movimiento,
        clave=clave, huella=huella)


def registrar_compra(session, datos, usuario_id):
    """Acepta artículos repetidos de formulario o lista JSON; nunca hace commit.

    Un formulario histórico de un artículo sin clave ni origen conserva la
    semántica de fondos externos. Los formularios actuales exigen el origen.
    """
    if not hasattr(datos, 'get'):
        raise ValueError('Solicitud de compra inválida.')
    proveedor = _identificador(datos.get('id_proveedor'), 'proveedor')
    items, total = _renglones(datos)
    metodo = datos.get('metodo_pago')
    if metodo not in METODOS_COMPRA:
        raise ValueError('Método de pago inválido.')
    factura = (datos.get('numero_factura') or '').strip()
    if len(factura) > 50:
        raise ValueError('El número de factura admite hasta 50 caracteres.')
    clave = _clave(datos)
    origen = _origen(datos, metodo, legacy=hasattr(datos, 'getlist') and not clave and len(items) == 1
                     and datos.get('items') is None)
    huella = _huella({'proveedor': proveedor, 'items': items, 'metodo': metodo,
                      'factura': factura, 'origen': origen}) if clave else None
    usuario_con_permiso(session, usuario_id, 'proveedores.gestionar')
    _bloquear_operacion(session, usuario_id, clave, 'compra')
    if clave:
        anterior = one('''SELECT id_compra,total,saldo_pendiente,huella_operacion
            FROM compra WHERE id_usuario=:u AND clave_operacion=:clave''',
            session=session, u=usuario_id, clave=clave)
        if anterior:
            if anterior['huella_operacion'] != huella:
                raise ValueError('Esta clave ya corresponde a una compra diferente.')
            return {'id_compra': anterior['id_compra'], 'total': anterior['total'],
                    'saldo_pendiente': anterior['saldo_pendiente'], 'repetida': True}
    if not one('''SELECT id_proveedor FROM proveedor
        WHERE id_proveedor=:id AND estado=TRUE FOR SHARE''', session=session, id=proveedor):
        raise ValueError('Proveedor inactivo o inexistente.')
    # El orden turno -> productos coincide con el cobro de ventas.
    turno = turno_propio_bloqueado(session, usuario_id) if origen == 'CAJA' else None
    for item in items:
        if not one('''SELECT id_producto FROM producto
            WHERE id_producto=:id AND estado=TRUE FOR UPDATE''',
            session=session, id=item['id_producto']):
            raise ValueError('Producto inactivo o inexistente.')
    saldo = total if metodo == 'CRÉDITO' else Decimal('0.00')
    compra_id = value('''INSERT INTO compra(id_proveedor,id_usuario,numero_factura,
        metodo_pago,total,saldo_pendiente,clave_operacion,huella_operacion,origen_efectivo)
        VALUES(:proveedor,:usuario,:factura,:metodo,:total,:saldo,:clave,:huella,:origen)
        RETURNING id_compra''', session=session, proveedor=proveedor, usuario=usuario_id,
        factura=factura, metodo=metodo, total=total, saldo=saldo, clave=clave,
        huella=huella, origen=origen)
    for item in items:
        campos = {'compra': compra_id, 'producto': item['id_producto'],
                  'cantidad': item['cantidad'], 'costo': Decimal(item['costo_unitario'])}
        session.execute(text('''INSERT INTO detalle_compra(id_compra,id_producto,cantidad,costo_unitario)
            VALUES(:compra,:producto,:cantidad,:costo)'''), campos)
        session.execute(text('''INSERT INTO movimiento_inventario
            (id_producto,tipo,cantidad,costo_unitario,motivo,id_usuario)
            VALUES(:producto,'ENTRADA',:cantidad,:costo,:motivo,:usuario)'''),
            {**campos, 'motivo': f'Compra #{compra_id}', 'usuario': usuario_id})
        session.execute(text('UPDATE producto SET costo=:costo WHERE id_producto=:id'),
                        {'costo': campos['costo'], 'id': item['id_producto']})
    if metodo != 'CRÉDITO' and total > 0:
        _pago(session, compra_id, usuario_id, total, metodo, origen, turno)
    return {'id_compra': compra_id, 'total': total, 'saldo_pendiente': saldo, 'repetida': False}


def registrar_abono(session, compra_id, datos, usuario_id):
    if not hasattr(datos, 'get'):
        raise ValueError('Solicitud de abono inválida.')
    compra_id = _identificador(compra_id, 'compra')
    clave = _clave(datos, obligatoria=True)
    monto = importe(datos.get('monto'), 'Monto', Decimal('0.01'))
    metodo = datos.get('metodo_pago')
    if metodo not in METODOS_COMPRA[:3]:
        raise ValueError('Selecciona efectivo, tarjeta o transferencia para el abono.')
    origen = _origen(datos, metodo)
    huella = _huella({'compra': compra_id, 'monto': str(monto), 'metodo': metodo, 'origen': origen})
    usuario_con_permiso(session, usuario_id, 'proveedores.gestionar')
    _bloquear_operacion(session, usuario_id, clave, 'abono')
    anterior = one('''SELECT id_pago_compra,id_compra,huella_operacion FROM pago_compra
        WHERE id_usuario=:u AND clave_operacion=:clave''', session=session,
        u=usuario_id, clave=clave)
    if anterior and anterior['huella_operacion'] != huella:
        raise ValueError('Esta clave ya corresponde a un pago diferente.')
    compra = one('SELECT * FROM compra WHERE id_compra=:id FOR UPDATE',
                 session=session, id=compra_id)
    if not compra:
        raise ValueError('La compra no existe.')
    if anterior:
        return {'id_pago_compra': anterior['id_pago_compra'],
                'saldo_pendiente': compra['saldo_pendiente'], 'repetida': True}
    if compra['saldo_pendiente'] is None:
        raise ValueError('Esta compra histórica necesita conciliarse antes de registrar abonos.')
    if monto > compra['saldo_pendiente']:
        raise ValueError('El abono supera el saldo pendiente de la compra.')
    turno = turno_propio_bloqueado(session, usuario_id) if origen == 'CAJA' else None
    pago_id = _pago(session, compra_id, usuario_id, monto, metodo, origen, turno, clave, huella)
    saldo = money(compra['saldo_pendiente'] - monto)
    session.execute(text('UPDATE compra SET saldo_pendiente=:saldo WHERE id_compra=:id'),
                    {'saldo': saldo, 'id': compra_id})
    return {'id_pago_compra': pago_id, 'saldo_pendiente': saldo, 'repetida': False}
