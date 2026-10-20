"""Estados, borrado sin cascadas y administración serializada de usuarios."""

from sqlalchemy import text
from datos import one, rows, value
from permisos import ROLES, usuario_con_permiso

ENTIDADES = {
    'productos': ('producto', 'id_producto'), 'categorias': ('categoria', 'id_categoria'),
    'clientes': ('cliente', 'id_cliente'), 'proveedores': ('proveedor', 'id_proveedor'),
    'usuarios': ('usuario', 'id_usuario'),
}


class RegistroNoEncontrado(ValueError):
    pass


def bloquear_admin(session, actor_id):
    session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
                    {'key': 'usuarios:administracion'})
    return usuario_con_permiso(session, actor_id, 'usuarios.gestionar')


def referencias(session, modulo, rid=None):
    tabla, pk = ENTIDADES[modulo]
    citar = session.get_bind().dialect.identifier_preparer.quote
    relaciones = rows('''SELECT c.oid,n.nspname AS esquema,t.relname AS tabla,
        array_agg(h.attname::text ORDER BY k.orden) AS hijas,
        array_agg(p.attname::text ORDER BY k.orden) AS padres
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        CROSS JOIN LATERAL unnest(c.conkey,c.confkey) WITH ORDINALITY AS k(hija,padre,orden)
        JOIN pg_attribute h ON h.attrelid=c.conrelid AND h.attnum=k.hija
        JOIN pg_attribute p ON p.attrelid=c.confrelid AND p.attnum=k.padre
        WHERE c.contype='f' AND c.confrelid=to_regclass(:tabla)
        GROUP BY c.oid,n.nspname,t.relname''', session=session, tabla='public.'+tabla)
    resultado = {}
    for relacion in relaciones:
        union = ' AND '.join(f'h.{citar(h)}=p.{citar(p)}' for h, p in zip(relacion['hijas'], relacion['padres']))
        filtro = f' WHERE p.{citar(pk)}=:id' if rid is not None else ''
        sql = (f'SELECT DISTINCT p.{citar(pk)} AS id FROM {citar(relacion["esquema"])}.'
               f'{citar(relacion["tabla"])} h JOIN public.{citar(tabla)} p ON {union}' + filtro)
        for registro in rows(sql, session=session, **({'id': rid} if rid is not None else {})):
            resultado.setdefault(registro['id'], set()).add(relacion['tabla'])
    return {key: 'Tiene historial o relaciones en: '+', '.join(sorted(tablas))+'.'
            for key, tablas in resultado.items()}


def protegido(modulo, fila, actor_id):
    if modulo == 'clientes' and (fila.get('nit_dpi') or '').strip().upper() == 'CF':
        return 'Consumidor Final es un registro protegido.'
    if modulo == 'usuarios' and fila['id_usuario'] == actor_id:
        return 'No puedes desactivar ni eliminar tu propia cuenta.'
    return ''


def preparar_listado(session, modulo, registros, actor_id):
    motivos = referencias(session, modulo)
    abiertos = set()
    if modulo == 'usuarios':
        abiertos = {r['id_usuario'] for r in rows(
            "SELECT id_usuario FROM turno_caja WHERE estado='ABIERTO'", session=session)}
    for fila in registros:
        rid = fila[ENTIDADES[modulo][1]]
        proteccion = protegido(modulo, fila, actor_id)
        fila.update(protegido=bool(proteccion), motivo_proteccion=proteccion,
                    motivo_eliminacion=proteccion or motivos.get(rid, ''),
                    puede_eliminar=not (proteccion or motivos.get(rid)))
        motivo = proteccion or ('Cierra el turno de caja antes de desactivar.' if rid in abiertos else '')
        fila.update(puede_desactivar=not motivo, motivo_desactivacion=motivo)
    return registros


def _ultimo_admin(session, fila, nuevo_rol=None):
    rol = value('SELECT nombre FROM rol WHERE id_rol=:id', session=session, id=fila['id_rol'])
    if fila['estado'] and rol == 'Administrador' and nuevo_rol != 'Administrador':
        cantidad = value('''SELECT COUNT(*) FROM usuario u JOIN rol r USING(id_rol)
            WHERE u.estado=TRUE AND r.nombre='Administrador' ''', session=session)
        if cantidad <= 1:
            raise ValueError('Debe permanecer al menos un administrador activo.')


def operar_registro(session, modulo, rid, actor_id, accion):
    if modulo == 'usuarios':
        bloquear_admin(session, actor_id)
    else:
        permiso = 'productos.gestionar' if modulo in ('productos', 'categorias') else modulo+'.gestionar'
        usuario_con_permiso(session, actor_id, permiso)
    tabla, pk = ENTIDADES[modulo]
    fila = one(f'SELECT * FROM {tabla} WHERE {pk}=:id FOR UPDATE', session=session, id=rid)
    if not fila:
        raise RegistroNoEncontrado('El registro no existe.')
    if accion not in ('eliminar', 'activar', 'desactivar'):
        raise ValueError('Selecciona activar o desactivar.')
    proteccion = protegido(modulo, fila, actor_id)
    if proteccion:
        raise ValueError(proteccion)
    if accion == 'eliminar':
        motivo = referencias(session, modulo, rid).get(rid)
        if motivo:
            raise ValueError(motivo+' Puedes desactivarlo para conservar su historial.')
        if modulo == 'usuarios':
            _ultimo_admin(session, fila)
        session.execute(text(f'DELETE FROM {tabla} WHERE {pk}=:id'), {'id': rid})
    else:
        if modulo == 'usuarios' and accion == 'desactivar' and fila['estado']:
            _ultimo_admin(session, fila)
            if value("SELECT EXISTS(SELECT 1 FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO')",
                     session=session, id=rid):
                raise ValueError('Cierra el turno de caja antes de desactivar este usuario.')
        session.execute(text(f'UPDATE {tabla} SET estado=:estado WHERE {pk}=:id'),
                        {'id': rid, 'estado': accion == 'activar'})


def asignar_rol(session, uid, id_rol, actor_id):
    bloquear_admin(session, actor_id)
    fila = one('SELECT * FROM usuario WHERE id_usuario=:id FOR UPDATE', session=session, id=uid)
    if not fila:
        raise RegistroNoEncontrado('El usuario no existe.')
    nombre = value('SELECT nombre FROM rol WHERE id_rol=:id', session=session, id=id_rol)
    if nombre not in ROLES:
        raise ValueError('Selecciona un rol válido: Administrador, Cajero o Bodeguero.')
    _ultimo_admin(session, fila, nombre)
    if nombre not in ('Administrador', 'Cajero') and value(
            "SELECT EXISTS(SELECT 1 FROM turno_caja WHERE id_usuario=:id AND estado='ABIERTO')",
            session=session, id=uid):
        raise ValueError('Cierra el turno de caja antes de asignar el rol Bodeguero.')
    session.execute(text('UPDATE usuario SET id_rol=:rol WHERE id_usuario=:id'), {'rol': id_rol, 'id': uid})
    return nombre
