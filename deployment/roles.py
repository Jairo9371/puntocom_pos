"""Concesiones runtime explícitas después de instalar/migrar con el propietario.

Ejecutar con conexión administrativa y APP_DB_PASSWORD en el entorno.
No crea esquemas ni ejecuta migraciones; es idempotente y no imprime secretos.
"""

import os
import sys

from psycopg2 import sql

from deployment.respaldos import conectar


def preparar_rol(entorno):
    clave = entorno.get('APP_DB_PASSWORD', '')
    if len(clave) < 24:
        raise ValueError('APP_DB_PASSWORD debe ser aleatoria y de al menos 24 caracteres.')
    with conectar(entorno) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='puntocom_app'")
            if not cursor.fetchone():
                cursor.execute('CREATE ROLE puntocom_app LOGIN')
            cursor.execute(sql.SQL('ALTER ROLE puntocom_app WITH LOGIN NOSUPERUSER NOCREATEDB '
                                   'NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Literal(clave)))
            cursor.execute('SELECT current_database(), current_user')
            base, propietario = cursor.fetchone()
            if propietario == 'puntocom_app':
                raise ValueError('Las concesiones requieren una conexión del propietario, no del runtime.')
            cursor.execute("""SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                              WHERE r.rolname='puntocom_app' LIMIT 1""")
            if cursor.fetchone():
                raise ValueError('puntocom_app tiene membresías previas; revísalas antes de usarlo como runtime.')
            # La propiedad concede DDL de forma implícita: REVOKE no la elimina.
            # Rechazar antes de confirmar cambios, también para un rol ya existente.
            cursor.execute("""SELECT 1 FROM pg_roles r WHERE r.rolname='puntocom_app' AND (
                EXISTS(SELECT 1 FROM pg_database d
                       WHERE d.datname=current_database() AND d.datdba=r.oid)
                OR EXISTS(SELECT 1 FROM pg_namespace n
                          WHERE n.nspname='public' AND n.nspowner=r.oid)
                OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE n.nspname='public' AND c.relowner=r.oid)
                OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                          WHERE n.nspname='public' AND p.proowner=r.oid))""")
            if cursor.fetchone():
                raise ValueError('El rol runtime no debe ser propietario de la base, del esquema '
                                 'public ni de sus relaciones o funciones.')
            # También elimina concesiones directas anteriores, como CREATE o TEMP.
            cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM puntocom_app').format(sql.Identifier(base)))
            cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(base)))
            cursor.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO puntocom_app').format(sql.Identifier(base)))
            cursor.execute('REVOKE ALL ON SCHEMA public FROM puntocom_app')
            cursor.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            cursor.execute('GRANT USAGE ON SCHEMA public TO puntocom_app')
            cursor.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO puntocom_app')
            cursor.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO puntocom_app')
            cursor.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public '
                                   'GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO puntocom_app').format(
                                       sql.Identifier(propietario)))
            cursor.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public '
                                   'GRANT USAGE,SELECT ON SEQUENCES TO puntocom_app').format(sql.Identifier(propietario)))
        conexion.commit()


if __name__ == '__main__':
    try:
        preparar_rol(os.environ)
        print('Permisos runtime preparados; utiliza puntocom_app para el servicio web.')
    except Exception as error:
        print(f'No se prepararon permisos ({type(error).__name__}); revisa conexión administrativa.', file=sys.stderr)
        raise SystemExit(1)
