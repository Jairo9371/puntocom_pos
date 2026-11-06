"""Configuración del entorno, sin conexiones ni lectura de secretos al importar."""

import os
import re
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool


TZ = ZoneInfo('America/Guatemala')


def _booleano(entorno, nombre, predeterminado=False):
    valor = entorno.get(nombre)
    if valor is None or not valor.strip():
        return predeterminado
    valor = valor.strip().lower()
    if valor in ('1', 'true', 'yes', 'on'):
        return True
    if valor in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'{nombre} debe ser true o false.')


def cargar_configuracion(entorno=None):
    """Permite pasar un entorno explícito para probar sin acceder al .env real."""
    entorno = os.environ if entorno is None else entorno
    en_vercel = entorno.get('VERCEL') == '1'
    ambiente = entorno.get('APP_ENV', 'production' if en_vercel else 'development').strip().lower()
    if ambiente not in ('development', 'testing', 'production'):
        raise ValueError('APP_ENV debe ser development, testing o production.')
    produccion = ambiente == 'production'
    clave = entorno.get('SECRET_KEY', '').strip()
    if produccion and not clave:
        raise ValueError('Configura SECRET_KEY antes de iniciar en producción.')
    debug = _booleano(entorno, 'APP_DEBUG')
    if produccion and debug:
        raise ValueError('APP_DEBUG no se puede habilitar en producción.')
    if produccion and (len(clave) < 32 or len(set(clave)) < 8 or
                       any(marca in clave.lower() for marca in
                           ('dev-only-', 'cambiar', 'change-me', 'changeme', 'example', 'ejemplo'))):
        raise ValueError('SECRET_KEY debe ser aleatoria, de al menos 32 caracteres y sin valores de ejemplo.')
    cookies_seguras = _booleano(entorno, 'SESSION_COOKIE_SECURE', produccion)
    if produccion and not cookies_seguras:
        raise ValueError('SESSION_COOKIE_SECURE debe ser true en producción.')
    hosts = [host.strip() for host in entorno.get('TRUSTED_HOSTS', '').split(',') if host.strip()]
    if en_vercel:
        # Dominios de este proyecto y despliegue, sin permitir todo .vercel.app.
        for variable in ('VERCEL_URL', 'VERCEL_BRANCH_URL', 'VERCEL_PROJECT_PRODUCTION_URL'):
            host = entorno.get(variable, '').strip()
            if host and host not in hosts:
                hosts.append(host)
    if any(not re.fullmatch(r'\.?[A-Za-z0-9][A-Za-z0-9.-]*', host) or '..' in host for host in hosts):
        raise ValueError('TRUSTED_HOSTS debe contener nombres de host separados por comas, sin esquema ni puerto.')
    if produccion and not hosts:
        raise ValueError('Configura TRUSTED_HOSTS antes de iniciar en producción.')
    try:
        puerto = int(entorno.get('DB_PORT', '5433'))
        if not 1 <= puerto <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('DB_PORT debe ser un puerto válido.') from None
    try:
        timeout = int(entorno.get('DB_CONNECT_TIMEOUT', '5'))
        if not 1 <= timeout <= 120:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('DB_CONNECT_TIMEOUT debe ser un entero entre 1 y 120.') from None
    sslmode = entorno.get('DB_SSLMODE', 'prefer').strip().lower()
    if sslmode not in ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'):
        raise ValueError('DB_SSLMODE no es un modo SSL válido de PostgreSQL.')
    argumentos_conexion = {
        'options': '-c timezone=America/Guatemala',
        'connect_timeout': timeout,
        'sslmode': sslmode,
    }
    if entorno.get('DB_SSLROOTCERT', '').strip():
        argumentos_conexion['sslrootcert'] = entorno['DB_SSLROOTCERT'].strip()
    opciones_motor = {
        'pool_pre_ping': True,
        # Una consulta posterior a un bloqueo debe ver los cambios confirmados.
        'isolation_level': 'READ COMMITTED',
        'connect_args': argumentos_conexion,
    }
    if en_vercel:
        # Cada instancia libera las conexiones al devolverlas a SQLAlchemy.
        opciones_motor['poolclass'] = NullPool
    return {
        'APP_ENV': ambiente,
        'SECRET_KEY': clave or ('dev-only-' + secrets.token_hex(32)),
        'DEBUG': debug,
        'SQLALCHEMY_DATABASE_URI': URL.create(
            'postgresql+psycopg2',
            username=entorno.get('DB_USER', 'postgres'),
            password=entorno.get('DB_PASSWORD', ''),
            host=entorno.get('DB_HOST', '127.0.0.1'),
            port=puerto,
            database=entorno.get('DB_NAME', 'puntocom_pos'),
        ),
        # TIMESTAMP/CURRENT_DATE/NOW() comparten el horario del negocio.
        'SQLALCHEMY_ENGINE_OPTIONS': opciones_motor,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': cookies_seguras,
        'REMEMBER_COOKIE_HTTPONLY': True,
        'REMEMBER_COOKIE_SAMESITE': 'Lax',
        'REMEMBER_COOKIE_SECURE': cookies_seguras,
        'TRUSTED_HOSTS': hosts or None,
        'MAX_CONTENT_LENGTH': 4 * 1024 * 1024,
    }
