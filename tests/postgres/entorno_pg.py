"""Infraestructura optativa para PostgreSQL real en una base desechable.

Nunca carga el esquema de pruebas sobre la base configurada del negocio.
"""

import importlib
import os
import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from configuracion import cargar_configuracion


ROOT = Path(__file__).resolve().parents[2]
TABLAS = (
    'rol', 'usuario', 'turno_caja', 'movimiento_caja', 'cliente', 'categoria',
    'producto', 'proveedor', 'venta', 'detalle_venta', 'compra', 'detalle_compra',
    'movimiento_inventario', 'solicitud_ajuste',
)
HASH_PRUEBA = generate_password_hash('PruebaPostgres2026!')


def configuracion_origen():
    entorno = {k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None}
    entorno.update(os.environ)
    return cargar_configuracion(entorno)


def motor_origen(config):
    opciones = dict(config['SQLALCHEMY_ENGINE_OPTIONS'])
    opciones['connect_args'] = dict(opciones['connect_args'], connect_timeout=5)
    return create_engine(config['SQLALCHEMY_DATABASE_URI'], **opciones)


def inspeccionar_conexion(engine):
    with engine.connect() as conexion:
        conexion.execute(text('SET TRANSACTION READ ONLY'))
        fila = conexion.execute(text('''SELECT
            current_setting('server_version') AS version,
            current_setting('TimeZone') AS zona,
            current_setting('transaction_isolation') AS aislamiento,
            (SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname=current_user) AS crear_base
        ''')).mappings().one()
        return dict(fila)


def contrato_esquema(engine):
    """Solo metadatos de public: no consulta filas ni modifica tablas del negocio."""
    with engine.connect() as conexion:
        conexion.execute(text('SET TRANSACTION READ ONLY'))
        columnas = conexion.execute(text('''SELECT table_name,column_name,data_type,is_nullable,
            character_maximum_length,numeric_precision,numeric_scale
            FROM information_schema.columns WHERE table_schema='public'
            ORDER BY table_name,ordinal_position''')).all()
        relaciones = conexion.execute(text('''SELECT tc.table_name,kcu.column_name,
            ccu.table_name,ccu.column_name FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_schema=kcu.constraint_schema AND tc.constraint_name=kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_schema=ccu.constraint_schema AND tc.constraint_name=ccu.constraint_name
            WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
            ORDER BY tc.table_name,kcu.column_name''')).all()
        tablas = conexion.execute(text('''SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name''')).scalars().all()
    return {
        'columnas': {(fila[0], fila[1]): tuple(fila[2:]) for fila in columnas
                     if fila[0] in (*TABLAS, 'vista_inventario')},
        'relaciones': {tuple(fila) for fila in relaciones if fila[0] in TABLAS},
        'tablas': set(tablas),
    }


def comparar_contratos(actual, esperado):
    diferencias = []
    for tabla in sorted(set(TABLAS) - actual['tablas']):
        diferencias.append(f'Falta tabla: {tabla}.')
    for clave, definicion in esperado['columnas'].items():
        if clave not in actual['columnas']:
            diferencias.append(f'Falta columna: {clave[0]}.{clave[1]}.')
        elif actual['columnas'][clave] != definicion:
            diferencias.append(f'Tipo, longitud o nulabilidad difieren: {clave[0]}.{clave[1]}.')
    for relacion in sorted(esperado['relaciones'] - actual['relaciones']):
        diferencias.append(f'Falta clave foránea: {relacion[0]}.{relacion[1]} -> {relacion[2]}.{relacion[3]}.')
    return diferencias


class BaseTemporalPostgres:
    """Crea desde template0; elimina exclusivamente la base que acaba de crear."""

    def __init__(self, config):
        self.config = config
        self.nombre = 'puntocom_test_' + uuid.uuid4().hex
        self.marca = 'puntocom-pruebas:' + uuid.uuid4().hex
        self.creada = False
        self.engine = None
        self.modulo = None

    def __enter__(self):
        try:
            admin = motor_origen(self.config)
            try:
                with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                    conn.exec_driver_sql(f'CREATE DATABASE "{self.nombre}" TEMPLATE template0')
                    self.creada = True
                    conn.exec_driver_sql(f'COMMENT ON DATABASE "{self.nombre}" IS \'{self.marca}\'')
            finally:
                admin.dispose()
            config = dict(self.config)
            config.update(
                SQLALCHEMY_DATABASE_URI=self.config['SQLALCHEMY_DATABASE_URI'].set(database=self.nombre),
                SQLALCHEMY_ENGINE_OPTIONS={
                    'pool_pre_ping': True, 'pool_size': 8, 'max_overflow': 8,
                    'isolation_level': self.config['SQLALCHEMY_ENGINE_OPTIONS']['isolation_level'],
                    'connect_args': {
                        'connect_timeout': 5,
                        'options': '-c timezone=America/Guatemala -c statement_timeout=15000 '
                                   '-c lock_timeout=8000 -c idle_in_transaction_session_timeout=20000',
                    },
                },
                TESTING=True, DEBUG=False, SECRET_KEY=uuid.uuid4().hex,
                SESSION_COOKIE_SECURE=False, REMEMBER_COOKIE_SECURE=False,
                WTF_CSRF_ENABLED=True,
            )
            # Este ejecutor se inicia en un proceso independiente de las pruebas unitarias.
            with patch('dotenv.load_dotenv'), patch('configuracion.cargar_configuracion', return_value=config):
                self.modulo = importlib.import_module('app')
            with self.modulo.app.app_context():
                self.engine = self.modulo.db.engine
                if self.engine.url.database != self.nombre:
                    raise RuntimeError('Se rechazó un motor que no apunta a la base temporal.')
                with self.engine.begin() as conn:
                    conn.exec_driver_sql((ROOT / 'sql' / '01_esquema.sql').read_text(encoding='utf-8-sig'))
                from migraciones import aplicar_migracion_roles
                aplicar_migracion_roles(self.engine)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.modulo is not None:
            with self.modulo.app.app_context():
                self.modulo.db.session.remove()
        if self.engine is not None:
            self.engine.dispose()
        if self.creada:
            if not re.fullmatch(r'puntocom_test_[a-f0-9]{32}', self.nombre):
                raise RuntimeError('Nombre temporal rechazado durante la limpieza.')
            admin = motor_origen(self.config)
            try:
                with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                    marca = conn.execute(text('''SELECT shobj_description(oid,'pg_database')
                        FROM pg_database WHERE datname=:nombre'''), {'nombre': self.nombre}).scalar()
                    if marca != self.marca:
                        raise RuntimeError('La marca de la base temporal no coincide; no se eliminó.')
                    conn.exec_driver_sql(f'DROP DATABASE "{self.nombre}" WITH (FORCE)')
                    self.creada = False
            finally:
                admin.dispose()


class CasoPostgres(unittest.TestCase):
    engine = None
    app = None

    def setUp(self):
        super().setUp()
        if self.engine is None:
            raise RuntimeError('Ejecuta estas pruebas mediante tests/postgres/ejecutar.py.')
        with self.app.app.app_context():
            self.app.db.session.remove()
        with self.engine.begin() as conn:
            conn.exec_driver_sql('TRUNCATE ' + ','.join(TABLAS) + ' RESTART IDENTITY CASCADE')
            conn.exec_driver_sql("INSERT INTO rol(nombre) VALUES ('Administrador'),('Cajero'),('Bodeguero')")
            for rol, nombre, correo in (
                (1, 'Administrador', 'admin@example.test'),
                (2, 'Cajero Uno', 'cajero1@example.test'),
                (2, 'Cajero Dos', 'cajero2@example.test'),
                (3, 'Bodeguero', 'bodeguero@example.test'),
            ):
                conn.execute(text('''INSERT INTO usuario(id_rol,nombre,correo,hash_contrasena)
                    VALUES(:rol,:nombre,:correo,:hash)'''),
                    {'rol': rol, 'nombre': nombre, 'correo': correo, 'hash': HASH_PRUEBA})
            conn.exec_driver_sql("INSERT INTO cliente(nombre_razon_social,nit_dpi) VALUES ('Consumidor Final','CF')")
            conn.exec_driver_sql("INSERT INTO categoria(nombre) VALUES ('Cuadernos')")
            conn.exec_driver_sql("INSERT INTO proveedor(razon_social) VALUES ('Proveedor Prueba')")
            conn.exec_driver_sql('''INSERT INTO producto(id_categoria,codigo,nombre,marca,unidad,costo,precio_venta,stock_minimo)
                VALUES (1,'PG001','Cuaderno Uno','Norma','Unidad',9,15,5),
                       (1,'PG002','Cuaderno Dos','Norma','Unidad',9,15,5)''')
            conn.exec_driver_sql('''INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo)
                VALUES (1,'ENTRADA',10,9,'Inventario prueba'),(2,'ENTRADA',10,9,'Inventario prueba')''')
            conn.exec_driver_sql("INSERT INTO turno_caja(id_usuario,fondo_inicial) VALUES (2,100),(3,100)")
        self.ids = {
            'admin': 1, 'cajero1': 2, 'cajero2': 3, 'bodeguero': 4, 'cliente': 1,
            'categoria': 1, 'proveedor': 1, 'producto1': 1, 'producto2': 2,
            'turno1': 1, 'turno2': 2,
        }
        self.client = self.app.app.test_client()

    def tearDown(self):
        with self.app.app.app_context():
            self.app.db.session.remove()
        super().tearDown()
