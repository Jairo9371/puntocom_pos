"""Ejecución programable: copia, restauración temporal y constancia de éxito.

No elimina respaldos antiguos. Impide dos ejecuciones simultáneas sobre la misma
carpeta y guarda errores sin detalles de conexión en respaldos.log.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

from deployment.respaldos import (
    ErrorRespaldo, crear_respaldo, entorno_conexion, leer_manifiesto, restaurar_nueva,
)


class RespaldoEnCurso(ErrorRespaldo):
    pass


def ahora_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def abrir_privado(ruta, modo):
    """Abrir solo archivos regulares; nunca seguir un enlace en el nombre final."""
    ruta = Path(ruta)
    if ruta.is_symlink():
        raise ErrorRespaldo('El archivo de control no puede ser un enlace simbólico.')
    descriptor = os.open(ruta, modo | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ErrorRespaldo('El archivo de control debe ser regular.')
        if os.name != 'nt':
            os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def bloqueo_directorio(directorio):
    """El SO libera el bloqueo al morir el proceso; el archivo no se elimina."""
    descriptor = abrir_privado(Path(directorio) / '.respaldo-diario.lock', os.O_RDWR)
    adquirido = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b'0')
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            adquirido = True
        except OSError as error:
            raise RespaldoEnCurso('Ya hay un respaldo diario en ejecución en esta carpeta.') from error
        yield
    finally:
        if adquirido:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def registrar(directorio, estado, detalle):
    descriptor = abrir_privado(Path(directorio) / 'respaldos.log', os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as archivo:
        archivo.write(f'{ahora_utc()} {estado} {detalle}\n')


def comprobar_clientes(entorno):
    versiones = {}
    for programa in ('pg_dump', 'pg_restore'):
        carpeta = entorno.get('PG_BIN', '').strip()
        nombre = programa + ('.exe' if os.name == 'nt' else '')
        ruta = str(Path(carpeta) / nombre) if carpeta else shutil.which(programa)
        if not ruta:
            raise ErrorRespaldo(f'No se encuentra {programa}; configura PATH o PG_BIN.')
        try:
            resultado = subprocess.run([ruta, '--version'], capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ErrorRespaldo(f'No se pudo ejecutar {programa}.') from error
        version = re.search(rb'PostgreSQL\)\s+(\d+)', resultado.stdout)
        if resultado.returncode or version is None:
            raise ErrorRespaldo(f'No se pudo comprobar la versión de {programa}.')
        versiones[programa] = int(version.group(1))
    if len(set(versiones.values())) != 1:
        raise ErrorRespaldo('pg_dump y pg_restore deben usar la misma versión mayor.')
    return versiones


def guardar_constancia(directorio, informacion):
    directorio = Path(directorio)
    parcial = directorio / f'.verificacion-{uuid.uuid4().hex}.partial'
    destino = directorio / 'ultimo_respaldo_verificado.json'
    if destino.is_symlink():
        raise ErrorRespaldo('La constancia no puede ser un enlace simbólico.')
    descriptor = abrir_privado(parcial, os.O_WRONLY | os.O_EXCL)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as archivo:
            json.dump(informacion, archivo, indent=2, ensure_ascii=False)
            archivo.write('\n')
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(parcial, destino)
    finally:
        parcial.unlink(missing_ok=True)


def ejecutar_diario(entorno, directorio):
    directorio = Path(directorio).resolve()
    directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        directorio.chmod(0o700)
    with bloqueo_directorio(directorio):
        try:
            comprobar_clientes(entorno)
            respaldo = crear_respaldo(entorno, directorio)
            _, filas = restaurar_nueva(entorno, respaldo, verificar=True, limpiar=True)
            _, manifiesto = leer_manifiesto(respaldo)
            informacion = {
                'formato': 1, 'verificado_utc': ahora_utc(),
                'archivo': respaldo.name, 'sha256': manifiesto['sha256'],
                'filas': filas, 'restauracion_temporal_eliminada': True,
            }
            guardar_constancia(directorio, informacion)
            registrar(directorio, 'OK', f'{respaldo.name}; {filas} filas verificadas')
            return informacion
        except BaseException as error:
            # No registrar str(error): un proveedor puede incluir la conexión.
            registrar(directorio, 'ERROR', type(error).__name__)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', help='Archivo privado explícito; no se imprime su contenido.')
    parser.add_argument('--directory', default='backups')
    parser.add_argument('--check', action='store_true', help='Comprobar clientes sin conectar ni crear copias.')
    argumentos = parser.parse_args(argv)
    try:
        entorno = entorno_conexion(argumentos.env_file)
        if argumentos.check:
            versiones = comprobar_clientes(entorno)
            print('Clientes disponibles:', ', '.join(f'{k} {v}' for k, v in versiones.items()))
        else:
            resultado = ejecutar_diario(entorno, argumentos.directory)
            print(f"Respaldo diario verificado: {resultado['archivo']}; {resultado['filas']} filas.")
        return 0
    except RespaldoEnCurso:
        print('Ya hay un respaldo diario en ejecución; no se inició otro.', file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt) as error:
        print(f'El respaldo diario no se completó ({type(error).__name__}); revisa clientes, conexión y permisos.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
