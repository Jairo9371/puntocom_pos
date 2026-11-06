"""Datos públicos del negocio; la ruta controla la transacción y su confirmación."""

import unicodedata

from sqlalchemy import text

from permisos import usuario_con_permiso
from validaciones import entero, validar_correo, validar_nit, validar_telefono


NEGOCIO_PREDETERMINADO = {
    'nombre_comercial': 'PUNTO COM',
    'giro': 'Librería · Guatemala',
    'direccion': '',
    'telefono': '',
    'correo': '',
    'nit': '',
    'mensaje_ticket': '¡Gracias por su compra!',
    'ancho_ticket': 80,
}
LIMITES_NEGOCIO = {
    'nombre_comercial': 150, 'giro': 150, 'direccion': 300,
    'telefono': 20, 'correo': 150, 'nit': 30, 'mensaje_ticket': 255,
}
_CAMPOS_SQL = ','.join(NEGOCIO_PREDETERMINADO)


def validar_negocio(datos):
    """Acepta únicamente campos del negocio, sin opciones de conexión ni secretos."""
    resultado = {}
    for campo, limite in LIMITES_NEGOCIO.items():
        valor = datos.get(campo, '')
        if not isinstance(valor, str):
            raise ValueError(f'El campo {campo.replace("_", " ")} debe ser texto.')
        valor = unicodedata.normalize('NFC', valor).replace('\r\n', '\n').strip()
        multilinea = campo in ('direccion', 'mensaje_ticket')
        if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs')
               and not (multilinea and c == '\n') for c in valor):
            raise ValueError(f'El campo {campo.replace("_", " ")} contiene caracteres no permitidos.')
        if len(valor) > limite:
            raise ValueError(f'El campo {campo.replace("_", " ")} supera {limite} caracteres.')
        resultado[campo] = valor
    if not resultado['nombre_comercial']:
        raise ValueError('El nombre comercial es obligatorio.')
    resultado['telefono'] = validar_telefono(resultado['telefono'])
    resultado['correo'] = validar_correo(resultado['correo'])
    resultado['nit'] = validar_nit(resultado['nit'])
    resultado['ancho_ticket'] = entero(datos.get('ancho_ticket', 80), etiqueta='ancho del ticket')
    if resultado['ancho_ticket'] not in (58, 80):
        raise ValueError('El ancho del ticket debe ser 58 u 80 mm.')
    return resultado


def cargar_negocio(session):
    """Devuelve solo datos públicos, sin insertar ni confirmar transacciones."""
    fila = session.execute(text(
        f'SELECT {_CAMPOS_SQL} FROM configuracion_negocio WHERE id=1'
    )).mappings().first()
    return dict(fila) if fila is not None else dict(NEGOCIO_PREDETERMINADO)


def guardar_negocio(session, datos, usuario_id):
    """Revalida al administrador y deja la escritura pendiente de commit del llamador."""
    normalizados = validar_negocio(datos)
    usuario_con_permiso(session, usuario_id, 'configuracion.gestionar')
    asignaciones = ','.join(f'{campo}=EXCLUDED.{campo}' for campo in NEGOCIO_PREDETERMINADO)
    valores = ','.join(f':{campo}' for campo in NEGOCIO_PREDETERMINADO)
    session.execute(text(f'''
        INSERT INTO configuracion_negocio(id,{_CAMPOS_SQL},actualizado_por)
        VALUES(1,{valores},:usuario_id)
        ON CONFLICT(id) DO UPDATE SET {asignaciones},
            actualizado_en=CURRENT_TIMESTAMP,actualizado_por=EXCLUDED.actualizado_por
    '''), {**normalizados, 'usuario_id': usuario_id})
    return normalizados
