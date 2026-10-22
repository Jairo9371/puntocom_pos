"""Concesiones de despliegue: no consolidar roles con privilegios previos inseguros."""

import unittest
from unittest.mock import MagicMock, patch

from deployment.roles import preparar_rol


class RolesDespliegueTests(unittest.TestCase):
    def conexion(self, membresia=None, propietario=None):
        conexion = MagicMock()
        cursor = conexion.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(1,), ('puntocom_prueba', 'postgres'), membresia, propietario]
        return conexion, cursor

    def test_propiedad_previa_impide_confirmar_y_conceder_permisos(self):
        conexion, cursor = self.conexion(propietario=(1,))
        with patch('deployment.roles.conectar') as conectar:
            conectar.return_value.__enter__.return_value = conexion
            with self.assertRaisesRegex(ValueError, 'propietario'):
                preparar_rol({'APP_DB_PASSWORD': 'a' * 32})
        conexion.commit.assert_not_called()
        self.assertFalse(any('GRANT ' in str(llamada.args[0])
                             for llamada in cursor.execute.call_args_list))

    def test_membresias_previas_impiden_confirmar_permisos(self):
        conexion, cursor = self.conexion(membresia=(1,))
        with patch('deployment.roles.conectar') as conectar:
            conectar.return_value.__enter__.return_value = conexion
            with self.assertRaisesRegex(ValueError, 'membresías'):
                preparar_rol({'APP_DB_PASSWORD': 'a' * 32})
        conexion.commit.assert_not_called()
        self.assertFalse(any('GRANT ' in str(llamada.args[0])
                             for llamada in cursor.execute.call_args_list))

    def test_rol_existente_pierde_concesiones_directas_antes_de_recibir_runtime(self):
        conexion, cursor = self.conexion()
        with patch('deployment.roles.conectar') as conectar:
            conectar.return_value.__enter__.return_value = conexion
            preparar_rol({'APP_DB_PASSWORD': 'a' * 32})
        sentencias = [str(llamada.args[0]) for llamada in cursor.execute.call_args_list]
        revocar_base = next(i for i, sentencia in enumerate(sentencias)
                            if 'REVOKE ALL ON DATABASE' in sentencia and 'FROM puntocom_app' in sentencia)
        conceder_base = next(i for i, sentencia in enumerate(sentencias) if 'GRANT CONNECT ON DATABASE' in sentencia)
        self.assertLess(revocar_base, conceder_base)
        self.assertLess(sentencias.index('REVOKE ALL ON SCHEMA public FROM puntocom_app'),
                        sentencias.index('GRANT USAGE ON SCHEMA public TO puntocom_app'))
        conexion.commit.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
