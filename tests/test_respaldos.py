"""Guardas de restauración: rechazan destinos inseguros antes de ejecutar SQL."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.respaldos import (
    ErrorRespaldo, ejecutar_pg, leer_manifiesto, parametros, restaurar_nueva, sha256, validar_destino,
)


class RespaldosTests(unittest.TestCase):
    def test_rechaza_base_negocio_sistema_uri_y_sql(self):
        for nombre in ('puntocom_pos', 'postgres', 'template1', 'pos_restore_;DROP DATABASE x',
                       'postgresql://localhost/pos_restore_x', '../pos_restore_x',
                       'POS_RESTORE_x', 'pos_restore_' + 'a' * 60, 'pos_verify_x'):
            with self.subTest(nombre=nombre), self.assertRaises(ErrorRespaldo):
                validar_destino(nombre, 'puntocom_pos')
        with self.assertRaises(ErrorRespaldo):
            validar_destino('pos_restore_origen', 'pos_restore_origen')
        self.assertEqual(validar_destino('pos_restore_revision_2026', 'puntocom_pos'),
                         'pos_restore_revision_2026')
        for nombre in ('postgresql://localhost/puntocom_pos', 'dbname=puntocom_pos host=otro', ''):
            with self.subTest(nombre=nombre), self.assertRaisesRegex(ErrorRespaldo, 'DB_NAME'):
                parametros({'DB_NAME': nombre})

    def test_manifiesto_corrupto_rechazado_antes_de_abrir_conexion(self):
        with TemporaryDirectory() as carpeta:
            copia = Path(carpeta) / 'archivo.dump'
            copia.write_bytes(b'PGDMP-datos-de-prueba')
            manifiesto = {'formato': 1, 'sha256': 'incorrecto', 'base_origen': 'puntocom_pos',
                          'filas': {'venta': 2}, 'columnas': [['venta', 'id_venta', 'integer', 'NO']]}
            Path(str(copia) + '.manifest.json').write_text(json.dumps(manifiesto), encoding='utf-8')
            with patch('deployment.respaldos.conectar') as conectar:
                with self.assertRaisesRegex(ErrorRespaldo, 'SHA-256'):
                    restaurar_nueva({}, copia, 'pos_restore_revision')
                conectar.assert_not_called()
            manifiesto['sha256'] = sha256(copia)
            Path(str(copia) + '.manifest.json').write_text(json.dumps(manifiesto), encoding='utf-8')
            self.assertEqual(leer_manifiesto(copia)[1]['filas'], {'venta': 2})
            with patch('deployment.respaldos.conectar') as conectar:
                with self.assertRaisesRegex(ErrorRespaldo, 'DB_NAME'):
                    restaurar_nueva({'DB_NAME': 'pos_restore_revision'}, copia, 'pos_restore_revision')
                conectar.assert_not_called()
                with self.assertRaisesRegex(ErrorRespaldo, 'limpieza'):
                    restaurar_nueva({}, copia, 'pos_restore_revision', limpiar=True)
                conectar.assert_not_called()

    def test_password_solo_entorno_y_error_sin_salida_privada(self):
        entorno = {'DB_PASSWORD': 'clave-prueba-no-exponer', 'DB_NAME': 'puntocom_prueba'}
        with patch('deployment.respaldos.shutil.which', return_value='pg_dump'), \
             patch('deployment.respaldos.subprocess.run') as ejecutar:
            ejecutar.return_value.returncode = 1
            ejecutar.return_value.stderr = b'clave-prueba-no-exponer'
            with self.assertRaises(ErrorRespaldo) as captura:
                ejecutar_pg(entorno, 'pg_dump', ['--format=custom'])
            comando = ejecutar.call_args.args[0]
            self.assertNotIn(entorno['DB_PASSWORD'], ' '.join(comando))
            self.assertEqual(ejecutar.call_args.kwargs['env']['PGPASSWORD'], entorno['DB_PASSWORD'])
            self.assertNotIn(entorno['DB_PASSWORD'], str(captura.exception))

    def test_opciones_heredadas_no_cambian_base(self):
        with patch.dict(os.environ, {'PGSERVICE': 'servicio-ajeno', 'PGDATABASE': 'otra_base', 'PGHOSTADDR': '1.2.3.4'}), \
             patch('deployment.respaldos.shutil.which', return_value='pg_restore'), \
             patch('deployment.respaldos.subprocess.run') as ejecutar:
            ejecutar.return_value.returncode = 0
            ejecutar_pg({'DB_NAME': 'puntocom_prueba'}, 'pg_restore', ['--list', 'copia.dump'], base=False)
            ambiente = ejecutar.call_args.kwargs['env']
            for nombre in ('PGSERVICE', 'PGDATABASE', 'PGHOSTADDR'):
                self.assertNotIn(nombre, ambiente)


if __name__ == '__main__':
    unittest.main()
