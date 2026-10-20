"""Una solicitud ya autenticada debe revalidar al administrador antes de un alta."""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from flask_login import current_user
from sqlalchemy import text

from entorno_pg import CasoPostgres, HASH_PRUEBA


class AdministradorRevocadoPostgres(CasoPostgres):
    ESPERA = 6

    def _valor(self, consulta, **parametros):
        with self.engine.connect() as conexion:
            return conexion.execute(text(consulta), parametros).scalar_one()

    def _csrf(self, cliente, pagina):
        respuesta = cliente.get(pagina)
        self.assertEqual(respuesta.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          respuesta.get_data(as_text=True))
        self.assertIsNotNone(token)
        return token.group(1)

    def _login(self, cliente, correo):
        respuesta = cliente.post('/login', data={
            'csrf_token': self._csrf(cliente, '/login'),
            'correo': correo, 'password': 'PruebaPostgres2026!',
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.location, '/')

    def test_alta_empezada_antes_de_revocacion_no_crea_otro_administrador(self):
        with self.engine.begin() as conexion:
            segundo_admin = conexion.execute(text('''
                INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena)
                VALUES(1,'Administrador Revisor','revisor@example.test',:hash)
                RETURNING id_usuario
            '''), {'hash': HASH_PRUEBA}).scalar_one()
        actor = self.ids['admin']
        self.assertNotEqual(actor, segundo_admin)
        self.assertEqual(self._valor("""SELECT COUNT(*) FROM turno_caja
            WHERE id_usuario=:id AND estado='ABIERTO'""", id=actor), 0)
        self._login(self.client, 'admin@example.test')
        revisor = self.app.app.test_client()
        self._login(revisor, 'revisor@example.test')
        token_alta = self._csrf(self.client, '/usuarios')
        token_baja = self._csrf(revisor, '/usuarios')
        correo_nuevo = 'administrador.creado@example.test'
        cantidad_antes = self._valor('SELECT COUNT(*) FROM usuario')
        autenticada, continuar = Event(), Event()
        validar_original = self.app.validar_correo

        def validar_y_retener(correo, *args, **kwargs):
            resultado = validar_original(correo, *args, **kwargs)
            if correo == correo_nuevo:
                # permiso_requerido ya cargó y autorizó al administrador. Retener
                # antes de la guardia transaccional permite ganar a la baja.
                self.assertEqual(int(current_user.id), actor)
                self.assertEqual(current_user.rol, 'Administrador')
                autenticada.set()
                if not continuar.wait(self.ESPERA):
                    raise AssertionError('No se liberó la solicitud de alta autenticada.')
            return resultado

        with patch.object(self.app, 'validar_correo', side_effect=validar_y_retener):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='admin-revocado') as pool:
                alta = pool.submit(self.client.post, '/usuarios', data={
                    'csrf_token': token_alta, 'nombre': 'Administrador Posterior',
                    'correo': correo_nuevo, 'id_rol': 1, 'password': 'PruebaPostgres2026!',
                })
                try:
                    if not autenticada.wait(self.ESPERA):
                        if alta.done():
                            alta.result()
                        self.fail('El alta no alcanzó la coordinación después de autenticarse.')
                    respuesta_baja = revisor.post(f'/usuarios/{actor}/estado', data={
                        'csrf_token': token_baja, 'estado': 'desactivar',
                    })
                    self.assertEqual(respuesta_baja.status_code, 302)
                    self.assertFalse(self._valor('SELECT estado FROM usuario WHERE id_usuario=:id',
                                                 id=actor))
                finally:
                    continuar.set()
                respuesta_alta = alta.result(timeout=self.ESPERA)
                self.assertEqual(respuesta_alta.status_code, 403)
                self.assertIn('No tienes permiso para realizar esta acción',
                              respuesta_alta.get_data(as_text=True))

        self.assertEqual(self._valor('SELECT COUNT(*) FROM usuario WHERE correo=:correo',
                                     correo=correo_nuevo), 0)
        self.assertEqual(self._valor('SELECT COUNT(*) FROM usuario'), cantidad_antes)
