"""Pruebas del respaldo programado, sin conectar a la base del negocio."""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.respaldo_diario import (
    RespaldoEnCurso, bloqueo_directorio, comprobar_clientes, ejecutar_diario, main,
)
from deployment.respaldos import ErrorRespaldo, sha256


class RespaldoDiarioTests(unittest.TestCase):
    def preparar_copia(self, carpeta):
        respaldo = Path(carpeta) / 'puntocom_prueba.dump'
        respaldo.write_bytes(b'PGDMP-copia-de-prueba')
        manifiesto = {'formato': 1, 'sha256': sha256(respaldo), 'base_origen': 'negocio',
                      'filas': {'producto': 3}, 'columnas': [['producto', 'id', 'integer', 'NO']]}
        Path(str(respaldo) + '.manifest.json').write_text(json.dumps(manifiesto), encoding='utf-8')
        return respaldo

    def mocks_copia(self, carpeta):
        pila = ExitStack()
        pila.enter_context(patch('deployment.respaldo_diario.comprobar_clientes'))
        crear = pila.enter_context(patch('deployment.respaldo_diario.crear_respaldo',
                                        return_value=self.preparar_copia(carpeta)))
        verificar = pila.enter_context(patch('deployment.respaldo_diario.restaurar_nueva',
                                             return_value=('pos_verify_temporal', 3)))
        return pila, crear, verificar

    def test_bloqueo_impide_dos_ejecuciones_y_se_libera_sin_borrar_archivo(self):
        with TemporaryDirectory() as carpeta:
            with bloqueo_directorio(carpeta):
                with self.assertRaises(RespaldoEnCurso):
                    with bloqueo_directorio(carpeta):
                        self.fail('No debe adquirir dos veces el mismo bloqueo.')
            self.assertTrue((Path(carpeta) / '.respaldo-diario.lock').is_file())
            with bloqueo_directorio(carpeta):
                pass

    def test_exito_solo_despues_de_restaurar_y_eliminar_temporal(self):
        with TemporaryDirectory() as carpeta:
            pila, crear, verificar = self.mocks_copia(carpeta)
            with pila:
                resultado = ejecutar_diario({}, carpeta)
            crear.assert_called_once()
            verificar.assert_called_once_with({}, crear.return_value, verificar=True, limpiar=True)
            constancia = json.loads((Path(carpeta) / 'ultimo_respaldo_verificado.json').read_text(encoding='utf-8'))
            self.assertEqual(constancia, resultado)
            self.assertEqual(constancia['archivo'], crear.return_value.name)
            self.assertEqual(constancia['sha256'], sha256(crear.return_value))
            self.assertEqual(constancia['filas'], 3)
            self.assertTrue(constancia['restauracion_temporal_eliminada'])
            self.assertIn('+00:00', constancia['verificado_utc'])
            self.assertIn(' OK ', (Path(carpeta) / 'respaldos.log').read_text(encoding='utf-8'))

    def test_fallo_no_reemplaza_constancia_y_no_registra_detalles_privados(self):
        with TemporaryDirectory() as carpeta:
            constancia = Path(carpeta) / 'ultimo_respaldo_verificado.json'
            constancia.write_text('{"anterior": true}', encoding='utf-8')
            pila, _, verificar = self.mocks_copia(carpeta)
            verificar.side_effect = RuntimeError('password=secreto-no-exponer')
            with pila, self.assertRaises(RuntimeError):
                ejecutar_diario({}, carpeta)
            self.assertEqual(json.loads(constancia.read_text(encoding='utf-8')), {'anterior': True})
            registro = (Path(carpeta) / 'respaldos.log').read_text(encoding='utf-8')
            self.assertIn('ERROR RuntimeError', registro)
            self.assertNotIn('secreto', registro)
            self.assertNotIn(' OK ', registro)
            self.assertFalse(list(Path(carpeta).glob('*.partial')))

    def test_copia_con_manifiesto_alterado_no_genera_constancia(self):
        with TemporaryDirectory() as carpeta:
            pila, crear, _ = self.mocks_copia(carpeta)
            crear.return_value.write_bytes(b'copia-alterada')
            with pila, self.assertRaisesRegex(ErrorRespaldo, 'SHA-256'):
                ejecutar_diario({}, carpeta)
            self.assertFalse((Path(carpeta) / 'ultimo_respaldo_verificado.json').exists())

    def test_cli_fallo_devuelve_codigo_sin_mensaje_del_proveedor(self):
        salida = io.StringIO()
        with patch('deployment.respaldo_diario.entorno_conexion', return_value={}), \
             patch('deployment.respaldo_diario.ejecutar_diario', side_effect=RuntimeError('clave privada')), \
             patch('sys.stderr', salida):
            self.assertEqual(main([]), 1)
        self.assertIn('RuntimeError', salida.getvalue())
        self.assertNotIn('clave privada', salida.getvalue())

    def test_cli_check_no_crea_copia_directorio_ni_conecta(self):
        with TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / 'no-creado'
            with patch('deployment.respaldo_diario.entorno_conexion', return_value={}), \
                 patch('deployment.respaldo_diario.comprobar_clientes', return_value={'pg_dump': 18, 'pg_restore': 18}), \
                 patch('deployment.respaldo_diario.ejecutar_diario') as ejecutar, \
                 patch('sys.stdout', io.StringIO()):
                self.assertEqual(main(['--check', '--directory', str(destino)]), 0)
            ejecutar.assert_not_called()
            self.assertFalse(destino.exists())

    def test_rechaza_versiones_distintas_sin_exponer_salida(self):
        with patch('deployment.respaldo_diario.shutil.which', return_value='cliente'), \
             patch('deployment.respaldo_diario.subprocess.run') as ejecutar:
            ejecutar.side_effect = [
                type('Resultado', (), {'stdout': b'pg_dump (PostgreSQL) 18.0', 'returncode': 0})(),
                type('Resultado', (), {'stdout': b'pg_restore (PostgreSQL) 17.4', 'returncode': 0})(),
            ]
            with self.assertRaisesRegex(ErrorRespaldo, 'misma versi'):
                comprobar_clientes({})

    def test_cli_simultaneo_devuelve_dos_y_no_inicia_copia(self):
        with TemporaryDirectory() as carpeta:
            with bloqueo_directorio(carpeta), \
                 patch('deployment.respaldo_diario.entorno_conexion', return_value={}), \
                 patch('deployment.respaldo_diario.crear_respaldo') as crear, \
                 patch('sys.stderr', io.StringIO()):
                self.assertEqual(main(['--directory', carpeta]), 2)
                crear.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'Crear symlinks en Windows requiere privilegios adicionales.')
    def test_no_sigue_enlace_del_archivo_de_bloqueo(self):
        with TemporaryDirectory() as carpeta:
            privado = Path(carpeta) / 'privado.txt'
            privado.write_text('conservar', encoding='utf-8')
            (Path(carpeta) / '.respaldo-diario.lock').symlink_to(privado)
            with self.assertRaisesRegex(ErrorRespaldo, 'enlace'):
                with bloqueo_directorio(carpeta):
                    pass
            self.assertEqual(privado.read_text(encoding='utf-8'), 'conservar')


if __name__ == '__main__':
    unittest.main()
