"""Respaldo consistente y restauración exclusivamente en bases nuevas.

Uso: python -m deployment.respaldos --env-file .env backup
     python -m deployment.respaldos --env-file .env verify backups/archivo.dump --cleanup
No carga .env implícitamente y nunca imprime contraseñas o errores del servidor.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from dotenv import dotenv_values
import psycopg2
from psycopg2 import sql


class ErrorRespaldo(RuntimeError):
    pass


def entorno_conexion(archivo=None):
    entorno = {}
    if archivo is not None:
        ruta = Path(archivo).resolve(strict=True)
        entorno.update({k: v for k, v in dotenv_values(ruta).items() if v is not None})
    entorno.update(os.environ)
    return entorno


def parametros(entorno, base=None):
    nombre = base or entorno.get('DB_NAME', 'puntocom_pos')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', nombre):
        raise ErrorRespaldo('DB_NAME debe ser un nombre literal, sin URI ni parámetros de conexión.')
    opciones = dict(host=entorno.get('DB_HOST', '127.0.0.1'),
                    port=int(entorno.get('DB_PORT', '5433')),
                    user=entorno.get('DB_USER', 'postgres'),
                    password=entorno.get('DB_PASSWORD', ''),
                    dbname=nombre,
                    connect_timeout=int(entorno.get('DB_CONNECT_TIMEOUT', '5')),
                    sslmode=entorno.get('DB_SSLMODE', 'prefer'),
                    application_name='puntocom-respaldos')
    if entorno.get('DB_SSLROOTCERT'):
        opciones['sslrootcert'] = entorno['DB_SSLROOTCERT']
    return opciones


@contextmanager
def conectar(entorno, base=None, autocommit=False):
    conexion = psycopg2.connect(**parametros(entorno, base))
    conexion.autocommit = autocommit
    try:
        yield conexion
    finally:
        conexion.close()


def ejecutar_pg(entorno, programa, argumentos, base=None):
    carpeta = entorno.get('PG_BIN', '').strip()
    ejecutable = str(Path(carpeta) / programa) if carpeta else shutil.which(programa)
    if carpeta and os.name == 'nt':
        ejecutable += '.exe'
    if not ejecutable:
        raise ErrorRespaldo(f'No se encuentra {programa}; instala clientes PostgreSQL o configura PG_BIN.')
    opciones = parametros(entorno, base)
    ambiente = os.environ.copy()
    # No heredar un servicio o una URI que pueda cambiar el destino seleccionado.
    for variable in ('PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS', 'PGDATABASE', 'PGHOSTADDR'):
        ambiente.pop(variable, None)
    ambiente.update(PGPASSWORD=opciones['password'], PGSSLMODE=opciones['sslmode'],
                    PGCONNECT_TIMEOUT=str(opciones['connect_timeout']))
    if opciones.get('sslrootcert'):
        ambiente['PGSSLROOTCERT'] = opciones['sslrootcert']
    comando = [ejecutable, '--no-password', '--host', opciones['host'],
               '--port', str(opciones['port']), '--username', opciones['user'],
               *argumentos]
    if base is not False:
        comando += ['--dbname', opciones['dbname']]
    # La contraseña viaja por entorno, nunca en URI, argumentos o salida.
    resultado = subprocess.run(comando, env=ambiente, capture_output=True,
                               timeout=int(entorno.get('BACKUP_TIMEOUT', '3600')), check=False)
    if resultado.returncode:
        raise ErrorRespaldo(f'{programa} falló (código {resultado.returncode}); revisa conexión y permisos.')
    return resultado


def contrato(conexion):
    with conexion.cursor() as cursor:
        cursor.execute("""SELECT tablename FROM pg_tables
                          WHERE schemaname='public' ORDER BY tablename""")
        tablas = [fila[0] for fila in cursor.fetchall()]
        conteos = {}
        for tabla in tablas:
            cursor.execute(sql.SQL('SELECT COUNT(*) FROM public.{}').format(sql.Identifier(tabla)))
            conteos[tabla] = cursor.fetchone()[0]
        cursor.execute("""SELECT table_name,column_name,data_type,is_nullable
                          FROM information_schema.columns WHERE table_schema='public'
                          ORDER BY table_name,ordinal_position""")
        columnas = [list(fila) for fila in cursor.fetchall()]
    return {'filas': conteos, 'columnas': columnas}


def sha256(ruta):
    resumen = hashlib.sha256()
    with Path(ruta).open('rb') as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b''):
            resumen.update(bloque)
    return resumen.hexdigest()


def archivo_privado(ruta):
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def crear_respaldo(entorno, directorio):
    directorio = Path(directorio).resolve()
    directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        directorio.chmod(0o700)
    fecha = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destino = directorio / f'puntocom_{fecha}_{uuid.uuid4().hex[:8]}.dump'
    parcial = destino.with_suffix('.dump.partial')
    manifiesto = Path(str(destino) + '.manifest.json')
    archivo_privado(parcial)
    try:
        with conectar(entorno) as conexion:
            conexion.set_session(isolation_level='REPEATABLE READ', readonly=True)
            with conexion.cursor() as cursor:
                cursor.execute('SELECT pg_export_snapshot(), current_database(), current_setting(%s)',
                               ('server_version',))
                snapshot, origen, version = cursor.fetchone()
            evidencia = contrato(conexion)
            ejecutar_pg(entorno, 'pg_dump', ['--format=custom', '--snapshot', snapshot,
                                            '--file', str(parcial)])
        # Un encabezado correcto no basta: pg_restore también debe leer el índice.
        ejecutar_pg(entorno, 'pg_restore', ['--list', str(parcial)], base=False)
        informacion = {'formato': 1, 'creado_utc': fecha, 'base_origen': origen,
                       'postgresql': version, 'sha256': sha256(parcial), **evidencia}
        archivo_privado(manifiesto)
        manifiesto.write_text(json.dumps(informacion, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        parcial.rename(destino)
        return destino
    except BaseException:
        parcial.unlink(missing_ok=True)
        manifiesto.unlink(missing_ok=True)
        raise


def validar_destino(nombre, origen, prefijo='pos_restore_'):
    if (not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', nombre or '') or
            not nombre.startswith(prefijo) or nombre == origen):
        raise ErrorRespaldo(f'El destino debe ser una base NUEVA con prefijo {prefijo}, distinta del origen.')
    return nombre


def leer_manifiesto(archivo):
    archivo = Path(archivo).resolve(strict=True)
    if not archivo.is_file():
        raise ErrorRespaldo('El respaldo debe ser un archivo.')
    informacion = json.loads(Path(str(archivo) + '.manifest.json').read_text(encoding='utf-8'))
    if informacion.get('formato') != 1 or informacion.get('sha256') != sha256(archivo):
        raise ErrorRespaldo('El respaldo no coincide con su manifiesto SHA-256; no se restauró.')
    if not isinstance(informacion.get('filas'), dict) or not informacion.get('columnas'):
        raise ErrorRespaldo('El manifiesto no contiene evidencia de tablas y columnas.')
    return archivo, informacion


def restaurar_nueva(entorno, archivo, nombre=None, verificar=False, limpiar=False):
    archivo, informacion = leer_manifiesto(archivo)
    prefijo = 'pos_verify_' if verificar else 'pos_restore_'
    generado = prefijo + datetime.now(timezone.utc).strftime('%Y%m%d_') + uuid.uuid4().hex[:12]
    nombre = validar_destino(nombre or generado, informacion['base_origen'], prefijo)
    if nombre == parametros(entorno)['dbname']:
        raise ErrorRespaldo('El destino no puede ser la base configurada en DB_NAME.')
    if limpiar and (not verificar or nombre != generado):
        raise ErrorRespaldo('La limpieza solo admite la base temporal generada por esta verificación.')
    creada = False
    marca = 'puntocom-restore:' + uuid.uuid4().hex
    try:
        with conectar(entorno, 'postgres', autocommit=True) as conexion:
            with conexion.cursor() as cursor:
                cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (nombre,))
                if cursor.fetchone():
                    raise ErrorRespaldo('La base de destino ya existe; no se modificó.')
                cursor.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(nombre)))
                creada = True
                cursor.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                    sql.Identifier(nombre), sql.Literal(marca)))
                cursor.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(nombre)))
        # Sin --clean ni --create: jamás se interpreta el nombre de base del dump como destino.
        ejecutar_pg(entorno, 'pg_restore', ['--single-transaction', '--exit-on-error', '--no-owner',
                                          '--no-privileges', str(archivo)], base=nombre)
        with conectar(entorno, nombre) as conexion:
            conexion.set_session(readonly=True)
            evidencia = contrato(conexion)
        if evidencia != {clave: informacion[clave] for clave in ('filas', 'columnas')}:
            raise ErrorRespaldo('La restauración difiere del contrato de columnas o conteos del respaldo.')
        return nombre, sum(evidencia['filas'].values())
    finally:
        # Solo una base creada aquí, con UUID generado aquí y permiso explícito --cleanup.
        if creada and limpiar and verificar and nombre == generado:
            with conectar(entorno, 'postgres', autocommit=True) as conexion:
                with conexion.cursor() as cursor:
                    cursor.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                                   (nombre,))
                    existente = cursor.fetchone()
                    if not existente or existente[0] != marca:
                        raise ErrorRespaldo('La identidad de la base temporal cambió; no se eliminó.')
                    cursor.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(nombre)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', help='Ruta explícita; variables del proceso tienen prioridad.')
    acciones = parser.add_subparsers(dest='accion', required=True)
    copia = acciones.add_parser('backup')
    copia.add_argument('--directory', default='backups')
    copia.add_argument('--verify-cleanup', action='store_true',
                       help='Restaurar y comprobar en una base temporal nueva; eliminar solo esa base.')
    restaurar = acciones.add_parser('restore')
    restaurar.add_argument('archivo')
    restaurar.add_argument('--database', help='Base nueva con prefijo pos_restore_; se genera si se omite.')
    verificar = acciones.add_parser('verify')
    verificar.add_argument('archivo')
    verificar.add_argument('--cleanup', action='store_true', help='Eliminar solo la base temporal creada aquí.')
    argumentos = parser.parse_args(argv)
    try:
        entorno = entorno_conexion(argumentos.env_file)
        if argumentos.accion == 'backup':
            destino = crear_respaldo(entorno, argumentos.directory)
            print('Respaldo creado:', destino)
            if argumentos.verify_cleanup:
                _, filas = restaurar_nueva(entorno, destino, verificar=True, limpiar=True)
                print(f'Respaldo verificado: {filas} filas; base temporal eliminada.')
        else:
            nombre, filas = restaurar_nueva(entorno, argumentos.archivo,
                                           getattr(argumentos, 'database', None),
                                           verificar=argumentos.accion == 'verify',
                                           limpiar=getattr(argumentos, 'cleanup', False))
            print(f'Restauración verificada: {nombre}; {filas} filas en tablas públicas.')
            if getattr(argumentos, 'cleanup', False):
                print('Base temporal de verificación eliminada.')
        return 0
    except ErrorRespaldo as error:
        print(str(error), file=sys.stderr)
    except (Exception, KeyboardInterrupt) as error:
        print(f'Operación incompleta ({type(error).__name__}); no se muestran datos de conexión.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
