"""Validación y control transaccional sin conexiones a la base del negocio."""

import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

from configuracion_negocio import (
    NEGOCIO_PREDETERMINADO, cargar_negocio, guardar_negocio, validar_negocio,
)


class ValidacionNegocioTests(unittest.TestCase):
    def test_normaliza_datos_publicos_y_descarta_secretos(self):
        datos = {**NEGOCIO_PREDETERMINADO, 'nombre_comercial': '  Librería 2026 & Papelería  ',
                 'direccion': 'Zona 1\r\nGuatemala', 'telefono': '23456789',
                 'correo': 'VENTAS@EJEMPLO.COM', 'nit': '12345-k', 'ancho_ticket': '58',
                 'SECRET_KEY': 'nunca guardar', 'DB_PASSWORD': 'tampoco'}
        resultado = validar_negocio(datos)
        self.assertEqual(resultado['nombre_comercial'], 'Librería 2026 & Papelería')
        self.assertEqual(resultado['direccion'], 'Zona 1\nGuatemala')
        self.assertEqual(resultado['correo'], 'ventas@ejemplo.com')
        self.assertEqual(resultado['nit'], '12345-K')
        self.assertEqual(resultado['ancho_ticket'], 58)
        self.assertEqual(set(resultado), set(NEGOCIO_PREDETERMINADO))

    def test_opcionales_pueden_vaciarse_y_nombre_es_obligatorio(self):
        resultado = validar_negocio({'nombre_comercial': 'Negocio'})
        self.assertEqual(resultado['telefono'], '')
        self.assertEqual(resultado['mensaje_ticket'], '')
        self.assertEqual(resultado['ancho_ticket'], 80)
        with self.assertRaises(ValueError):
            validar_negocio({'nombre_comercial': '   '})

    def test_rechaza_longitud_contactos_controles_y_anchos_invalidos(self):
        for campo, valor in (
            ('nombre_comercial', 'a' * 151), ('giro', 'a' * 151),
            ('direccion', 'a' * 301), ('mensaje_ticket', 'a' * 256),
            ('telefono', '1234567'), ('correo', 'sin-arroba'), ('nit', 'CF'),
            ('direccion', 'Texto\x00oculto'), ('nombre_comercial', 'Texto\nOtro'),
            ('mensaje_ticket', 'Texto\u202eoculto'), ('nombre_comercial', None),
            ('ancho_ticket', '72'), ('ancho_ticket', '58.5'), ('ancho_ticket', True),
        ):
            with self.subTest(campo=campo, valor=valor), self.assertRaises(ValueError):
                validar_negocio({**NEGOCIO_PREDETERMINADO, campo: valor})

    def test_cargar_sin_fila_no_escribe_y_retorna_copia_predeterminada(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.first.return_value = None
        negocio = cargar_negocio(session)
        negocio['nombre_comercial'] = 'Cambio local'
        self.assertEqual(NEGOCIO_PREDETERMINADO['nombre_comercial'], 'PUNTO COM')
        session.commit.assert_not_called()
        self.assertEqual(session.execute.call_count, 1)
        self.assertIn('SELECT', str(session.execute.call_args.args[0]))

    def test_guardar_revalida_permiso_y_deja_commit_al_llamador(self):
        session = MagicMock()
        with patch('configuracion_negocio.usuario_con_permiso') as permiso:
            resultado = guardar_negocio(session, NEGOCIO_PREDETERMINADO, 7)
        permiso.assert_called_once_with(session, 7, 'configuracion.gestionar')
        self.assertEqual(resultado, NEGOCIO_PREDETERMINADO)
        self.assertEqual(session.execute.call_args.args[1]['usuario_id'], 7)
        session.commit.assert_not_called()

    def test_permiso_revocado_no_escribe(self):
        session = MagicMock()
        with patch('configuracion_negocio.usuario_con_permiso', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                guardar_negocio(session, NEGOCIO_PREDETERMINADO, 7)
        session.execute.assert_not_called()
        session.commit.assert_not_called()


class RutaConfiguracionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('dotenv.load_dotenv'), patch.dict(os.environ, {
            'APP_ENV': 'testing', 'APP_DEBUG': 'false', 'SECRET_KEY': 'pruebas-configuracion',
            'DB_HOST': '127.0.0.1', 'DB_PORT': '5433', 'DB_NAME': 'no_conectar',
            'SESSION_COOKIE_SECURE': 'false',
        }):
            cls.modulo = importlib.import_module('app')

    def setUp(self):
        ajustes = patch.dict(self.modulo.app.config, {'TESTING': True, 'WTF_CSRF_ENABLED': False})
        ajustes.start()
        self.addCleanup(ajustes.stop)
        self.session = MagicMock()
        self.usuario = {'id_usuario': 1, 'nombre': 'Admin', 'correo': 'admin@example.test',
                        'rol': 'Administrador'}
        parches = (
            patch.object(self.modulo.db, 'session', self.session),
            patch.object(self.modulo, 'one', return_value=self.usuario),
            patch.object(self.modulo, 'render_template', return_value='Formulario'),
        )
        for parche in parches:
            parche.start()
            self.addCleanup(parche.stop)
        self.client = self.modulo.app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = '1'
            sesion['_fresh'] = True

    def test_guardar_exitoso_confirma_una_vez(self):
        with patch.object(self.modulo, 'guardar_negocio') as guardar:
            respuesta = self.client.post('/configuracion', data=NEGOCIO_PREDETERMINADO)
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.location, '/configuracion')
        guardar.assert_called_once()
        self.session.commit.assert_called_once()

    def test_error_retiene_datos_y_revierte(self):
        with patch.object(self.modulo, 'guardar_negocio', side_effect=ValueError('Teléfono inválido')):
            respuesta = self.client.post('/configuracion', data={
                **NEGOCIO_PREDETERMINADO, 'telefono': '123', 'nombre_comercial': 'Mi negocio',
            })
        self.assertEqual(respuesta.status_code, 400)
        argumentos = self.modulo.render_template.call_args.kwargs
        self.assertEqual(argumentos['negocio']['nombre_comercial'], 'Mi negocio')
        self.assertEqual(argumentos['errores'], ['Teléfono inválido'])
        self.session.rollback.assert_called_once()
        self.session.commit.assert_not_called()

    def test_csrf_y_roles_impiden_entrar_al_servicio(self):
        with patch.object(self.modulo, 'guardar_negocio') as guardar:
            with patch.dict(self.modulo.app.config, {'WTF_CSRF_ENABLED': True}):
                self.assertEqual(self.client.post('/configuracion', data={}).status_code, 400)
            for rol in ('Cajero', 'Bodeguero'):
                self.usuario['rol'] = rol
                self.assertEqual(self.client.get('/configuracion').status_code, 403)
                self.assertEqual(self.client.post('/configuracion', data={}).status_code, 403)
            guardar.assert_not_called()
        self.session.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
