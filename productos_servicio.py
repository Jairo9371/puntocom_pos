"""Inserción de productos con códigos internos únicos generados en el servidor."""

from datos import value


def insertar_producto(session, datos):
    """Recibe datos validados y participa en la transacción de quien llama.

    La secuencia existente reserva identificadores distintos entre conexiones.
    Si un código histórico ocupa el candidato, la restricción UNIQUE coordina
    el conflicto y se intenta con el siguiente identificador. No usa MAX + 1.
    Los códigos explícitos siguen disponibles para cargas anteriores.
    """
    codigo = datos.get('codigo')
    if codigo:
        if len(codigo) > 50:
            raise ValueError('El código interno admite hasta 50 caracteres.')
        producto_id = value('''INSERT INTO producto(id_categoria,codigo,codigo_barras,nombre,marca,unidad,
            costo,precio_venta,stock_minimo,estado)
            VALUES(:categoria,:codigo,:barras,:nombre,:marca,:unidad,:costo,:precio,:minimo,:activo)
            RETURNING id_producto''', session=session, **datos)
        return {'id_producto': producto_id, 'codigo': codigo}

    while True:
        producto_id = value("SELECT nextval(pg_get_serial_sequence('producto','id_producto'))",
                            session=session)
        codigo = 'PC-' + str(producto_id).zfill(6)
        insertado = value('''INSERT INTO producto(id_producto,id_categoria,codigo,codigo_barras,nombre,
            marca,unidad,costo,precio_venta,stock_minimo,estado)
            VALUES(:id,:categoria,:codigo,:barras,:nombre,:marca,:unidad,:costo,:precio,:minimo,:activo)
            ON CONFLICT(codigo) DO NOTHING RETURNING id_producto''',
            session=session, **{**datos, 'id': producto_id, 'codigo': codigo})
        if insertado is not None:
            return {'id_producto': insertado, 'codigo': codigo}
