"""Ejecutor explícito de integración y concurrencia PostgreSQL (base desechable)."""

import argparse
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path


CARPETA = Path(__file__).resolve().parent
ROOT = CARPETA.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CARPETA))

from configuracion import TZ
from entorno_pg import (
    BaseTemporalPostgres, CasoPostgres, comparar_contratos, configuracion_origen,
    contrato_esquema, inspeccionar_conexion, motor_origen,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solo-conexion', action='store_true')
    parser.add_argument('--patron', default='test_*_pg.py')
    parser.add_argument('--sin-comparar-instalacion', action='store_true',
                        help='Prueba el esquema del repositorio antes de migrar la instalación actual.')
    argumentos = parser.parse_args()
    config = configuracion_origen()
    origen = motor_origen(config)
    reporte = {'fecha': datetime.now(TZ).isoformat(), 'base_temporal_eliminada': False}
    resultado = None
    base = None
    try:
        info = inspeccionar_conexion(origen)
        reporte['servidor'] = info
        print(f"PostgreSQL {info['version']} | {info['zona']} | {info['aislamiento']}", flush=True)
        print('Permiso de crear base temporal:', bool(info['crear_base']), flush=True)
        if argumentos.solo_conexion:
            return 0
        esquema_actual = None if argumentos.sin_comparar_instalacion else contrato_esquema(origen)
        with BaseTemporalPostgres(config) as base:
            print('Base desechable creada desde template0, con el esquema del repositorio.', flush=True)
            diferencias = [] if esquema_actual is None else comparar_contratos(esquema_actual, contrato_esquema(base.engine))
            reporte['comparacion_instalacion_omitida'] = esquema_actual is None
            reporte['diferencias_esquema_instalado'] = diferencias
            print('Diferencias de contrato en la instalación:', len(diferencias), flush=True)
            for diferencia in diferencias:
                print('  ' + diferencia, flush=True)
            CasoPostgres.engine = base.engine
            CasoPostgres.app = base.modulo
            suite = unittest.defaultTestLoader.discover(str(CARPETA), pattern=argumentos.patron)
            resultado = unittest.TextTestRunner(verbosity=2).run(suite)
            reporte.update(
                pruebas=resultado.testsRun, errores=len(resultado.errors),
                fallos=len(resultado.failures), omitidas=len(resultado.skipped),
                casos_con_fallo=[caso.id() for caso, _ in resultado.errors + resultado.failures],
            )
        reporte['base_temporal_eliminada'] = not base.creada
        print('Base temporal eliminada:', reporte['base_temporal_eliminada'], flush=True)
        return 0 if resultado.wasSuccessful() and resultado.testsRun > 0 and not diferencias else 1
    except Exception as error:
        # No mostrar URLs, contraseñas ni mensajes de conexión que puedan revelar datos privados.
        reporte['error_infraestructura'] = type(error).__name__
        reporte['sqlstate'] = getattr(getattr(error, 'orig', None), 'pgcode', None)
        reporte['base_temporal_eliminada'] = bool(base is not None and not base.creada)
        print('No se completó la validación:', reporte['error_infraestructura'],
              'SQLSTATE:', reporte['sqlstate'], flush=True)
        return 1
    finally:
        origen.dispose()
        if not argumentos.solo_conexion:
            destino = ROOT / 'documentacion' / 'resultado_postgresql.json'
            destino.parent.mkdir(exist_ok=True)
            destino.write_text(json.dumps(reporte, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
