"""Permisos de la aplicación, compartidos por rutas y plantillas.

El rol se obtiene de ``current_user``, cargado desde PostgreSQL en cada
solicitud. Los datos enviados por el navegador nunca conceden permisos.
"""

from functools import wraps

from flask import abort
from flask_login import current_user, login_required
from sqlalchemy import text


ROLES = ('Administrador', 'Cajero', 'Bodeguero')
MENSAJE_ACCESO_DENEGADO = 'No tienes permiso para realizar esta acción'

_ADMIN = frozenset({'Administrador'})
_VENTAS = frozenset({'Administrador', 'Cajero'})
_INVENTARIO = frozenset({'Administrador', 'Bodeguero'})

PERMISOS = {
    'productos.ver': frozenset(ROLES),
    'productos.gestionar': _ADMIN,
    'costos.ver': _ADMIN,
    'ventas.registrar': _VENTAS,
    'ventas.ver': _VENTAS,
    'ventas.todas': _ADMIN,
    'ventas.anular': _ADMIN,
    'descuentos.modificar': _ADMIN,
    'clientes.ver': _VENTAS,
    'clientes.crear': _VENTAS,
    'clientes.gestionar': _ADMIN,
    'proveedores.ver': _INVENTARIO,
    'proveedores.gestionar': _ADMIN,
    'inventario.ver': _INVENTARIO,
    'inventario.mover': _INVENTARIO,
    'inventario.contar': _INVENTARIO,
    'inventario.aprobar': _ADMIN,
    'reportes.ventas': _VENTAS,
    'usuarios.gestionar': _ADMIN,
    'configuracion.gestionar': _ADMIN,
}


def tiene_permiso(permiso, rol=None):
    """Consulta un permiso. Los permisos y roles desconocidos se deniegan."""
    if rol is None:
        if not getattr(current_user, 'is_authenticated', False):
            return False
        rol = current_user.rol
    return rol in PERMISOS.get(permiso, frozenset())


def exigir_permiso(permiso):
    """Deniega una operación interna protegida si no está autorizada."""
    if not tiene_permiso(permiso):
        abort(403, description=MENSAJE_ACCESO_DENEGADO)


def permiso_requerido(permiso):
    """Autentica al usuario y exige el permiso antes de ejecutar una ruta."""
    def decorar(funcion):
        @wraps(funcion)
        @login_required
        def protegida(*args, **kwargs):
            exigir_permiso(permiso)
            return funcion(*args, **kwargs)
        protegida.permiso = permiso
        return protegida
    return decorar


def destino_inicio(rol):
    """Nombre del endpoint inicial correspondiente a cada rol."""
    return {
        'Administrador': 'dashboard',
        'Cajero': 'pos',
        'Bodeguero': 'inventario',
    }.get(rol, 'login')


def usuario_con_permiso(session, uid, permiso, bloqueo='SHARE'):
    """Comprueba el estado y rol vigentes dentro de la transacción de trabajo.

    El bloqueo evita que se cambie el rol o se desactive al responsable antes
    de terminar la operación autorizada. Solo se admiten modos fijos.
    """
    if bloqueo not in {'SHARE', 'NO KEY UPDATE'}:
        raise ValueError('Modo de bloqueo de usuario inválido.')
    usuario = session.execute(text(f'''
        SELECT u.id_usuario, r.nombre AS rol
        FROM usuario u JOIN rol r USING(id_rol)
        WHERE u.id_usuario=:uid AND u.estado=TRUE
        FOR {bloqueo} OF u
    '''), {'uid': uid}).mappings().first()
    if not usuario or not tiene_permiso(permiso, usuario['rol']):
        raise PermissionError(MENSAJE_ACCESO_DENEGADO)
    return dict(usuario)
