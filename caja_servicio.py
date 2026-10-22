"""Operaciones de caja serializadas. La transacción pertenece a la ruta."""

from decimal import Decimal

from sqlalchemy import text

from datos import one, value
from permisos import usuario_con_permiso
from validaciones import money


MAX_IMPORTE = Decimal('9999999999.99')


def importe(valor, etiqueta='Importe', minimo=Decimal('0.00')):
    if valor is None or str(valor).strip() == '':
        raise ValueError(f'{etiqueta} es obligatorio.')
    numero = money(valor)
    if numero < minimo or numero > MAX_IMPORTE:
        raise ValueError(f'{etiqueta} fuera del rango permitido.')
    return numero


def turno_propio_bloqueado(session, usuario_id):
    turno = one("""SELECT * FROM turno_caja WHERE id_usuario=:u AND estado='ABIERTO'
        ORDER BY id_turno DESC LIMIT 1 FOR UPDATE""", session=session, u=usuario_id)
    if not turno:
        raise ValueError('Abre tu caja primero.')
    return turno


def saldo_turno(session, turno):
    movimientos = value("""SELECT COALESCE(SUM(CASE WHEN tipo='INGRESO'
        THEN monto ELSE -monto END),0) FROM movimiento_caja WHERE id_turno=:t""",
        session=session, t=turno['id_turno'])
    return money(turno['fondo_inicial'] + movimientos)


def egreso_bloqueado(session, turno, monto, concepto):
    """Quien llama debe poseer el bloqueo del turno y comprobar al propietario."""
    if saldo_turno(session, turno) < monto:
        raise ValueError('Efectivo insuficiente en tu caja para realizar este pago.')
    return value("""INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
        VALUES(:t,'EGRESO',:concepto,:monto) RETURNING id_movimiento_caja""",
        session=session, t=turno['id_turno'], concepto=concepto[:255], monto=monto)


def operar_caja(session, datos, usuario_id):
    accion = datos.get('accion')
    usuario_con_permiso(session, usuario_id, 'ventas.registrar',
                        bloqueo='NO KEY UPDATE' if accion == 'abrir' else 'SHARE')
    if accion == 'abrir':
        if one("""SELECT id_turno FROM turno_caja
            WHERE id_usuario=:u AND estado='ABIERTO' FOR UPDATE""",
            session=session, u=usuario_id):
            raise ValueError('Ya tienes un turno abierto.')
        return value("""INSERT INTO turno_caja(id_usuario,fondo_inicial,estado)
            VALUES(:u,:fondo,'ABIERTO') RETURNING id_turno""", session=session,
            u=usuario_id, fondo=importe(datos.get('fondo_inicial'), 'Fondo inicial'))
    if accion not in ('movimiento', 'cerrar'):
        raise ValueError('Acción de caja no válida.')
    turno = turno_propio_bloqueado(session, usuario_id)
    if accion == 'movimiento':
        tipo = datos.get('tipo')
        concepto = (datos.get('concepto') or '').strip()
        if tipo not in ('INGRESO', 'EGRESO'):
            raise ValueError('Tipo de movimiento inválido.')
        if not concepto or len(concepto) > 255:
            raise ValueError('Indica un concepto de hasta 255 caracteres.')
        monto = importe(datos.get('monto'), 'Monto', Decimal('0.01'))
        if tipo == 'EGRESO':
            return egreso_bloqueado(session, turno, monto, concepto)
        return value("""INSERT INTO movimiento_caja(id_turno,tipo,concepto,monto)
            VALUES(:t,'INGRESO',:concepto,:monto) RETURNING id_movimiento_caja""",
            session=session, t=turno['id_turno'], concepto=concepto, monto=monto)
    contado = importe(datos.get('efectivo_contado'), 'Efectivo contado')
    esperado = saldo_turno(session, turno)
    diferencia = money(contado - esperado)
    if abs(esperado) > MAX_IMPORTE or abs(diferencia) > MAX_IMPORTE:
        raise ValueError('El saldo o diferencia supera el importe permitido para el cierre.')
    motivo = (datos.get('motivo_diferencia') or '').strip()
    if len(motivo) > 500 or (diferencia != 0 and not motivo):
        raise ValueError('Indica el motivo del faltante o sobrante (máximo 500 caracteres).')
    session.execute(text("""UPDATE turno_caja SET cierre=NOW(),estado='CERRADO',
        efectivo_contado=:contado,efectivo_esperado=:esperado,diferencia=:diferencia,
        motivo_diferencia=:motivo WHERE id_turno=:id"""),
        {'contado': contado, 'esperado': esperado, 'diferencia': diferencia,
         'motivo': motivo or None, 'id': turno['id_turno']})
    return {'id_turno': turno['id_turno'], 'efectivo_esperado': esperado,
            'efectivo_contado': contado, 'diferencia': diferencia}
