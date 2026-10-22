"""Instalación inicial explícita; rechaza cualquier tabla existente en public."""

import os
from pathlib import Path
import sys

from deployment.respaldos import conectar


def instalar_nueva(entorno):
    with conectar(entorno) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute("""SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                              WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')""")
            if cursor.fetchone()[0]:
                raise ValueError('La base tiene relaciones existentes; no es una instalación nueva.')
            carpeta = Path(__file__).resolve().parents[1] / 'sql'
            for nombre in ('01_esquema.sql', '02_datos_iniciales.sql'):
                cursor.execute((carpeta / nombre).read_text(encoding='utf-8-sig'))
        conexion.commit()


if __name__ == '__main__':
    try:
        instalar_nueva(os.environ)
        print('Esquema inicial y datos de demostración instalados. Aplica migrar-operacion antes de iniciar web.')
    except Exception as error:
        print(f'Instalación inicial rechazada o incompleta ({type(error).__name__}). No se muestran datos privados.',
              file=sys.stderr)
        raise SystemExit(1)
