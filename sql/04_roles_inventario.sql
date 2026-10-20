-- Migración aditiva e idempotente. No elimina usuarios, roles ni historial.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SELECT pg_advisory_xact_lock(hashtextextended('puntocom:migracion:04_roles_inventario',0));
ALTER TABLE categoria ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE cliente ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE proveedor ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
DO $$
DECLARE nombre_rol TEXT; elegido INTEGER;
BEGIN
  FOR nombre_rol IN SELECT unnest(ARRAY['Administrador','Cajero','Bodeguero']) LOOP
    SELECT id_rol INTO elegido FROM rol
      WHERE lower(trim(nombre))=lower(nombre_rol)
      ORDER BY (nombre=nombre_rol) DESC,id_rol LIMIT 1;
    IF elegido IS NULL THEN
      INSERT INTO rol(nombre) VALUES(nombre_rol) RETURNING id_rol INTO elegido;
    ELSE
      UPDATE rol SET nombre=nombre_rol WHERE id_rol=elegido AND nombre<>nombre_rol;
    END IF;
    UPDATE usuario SET id_rol=elegido WHERE id_rol<>elegido AND id_rol IN
      (SELECT id_rol FROM rol WHERE lower(trim(nombre))=lower(nombre_rol));
  END LOOP;
  UPDATE usuario SET id_rol=(SELECT id_rol FROM rol WHERE nombre='Bodeguero')
    WHERE id_rol IN (SELECT id_rol FROM rol WHERE lower(trim(nombre))='encargado de inventario');
  IF EXISTS(SELECT 1 FROM usuario u JOIN rol r USING(id_rol)
            WHERE r.nombre NOT IN ('Administrador','Cajero','Bodeguero')) THEN
    RAISE EXCEPTION 'Existen usuarios con roles no reconocidos. Asigne un rol admitido antes de migrar.';
  END IF;
END $$;
ALTER TABLE movimiento_inventario ADD COLUMN IF NOT EXISTS id_usuario INTEGER REFERENCES usuario(id_usuario);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS id_anulador INTEGER REFERENCES usuario(id_usuario);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS fecha_anulacion TIMESTAMP;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT;
CREATE TABLE IF NOT EXISTS solicitud_ajuste (
 id_solicitud SERIAL PRIMARY KEY,
 id_producto INTEGER NOT NULL REFERENCES producto(id_producto),
 id_solicitante INTEGER NOT NULL REFERENCES usuario(id_usuario),
 fecha_solicitud TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 existencia_sistema INTEGER NOT NULL CHECK(existencia_sistema>=0),
 cantidad_contada INTEGER NOT NULL CHECK(cantidad_contada>=0),
 diferencia INTEGER NOT NULL,
 motivo TEXT NOT NULL,
 estado VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE' CHECK(estado IN ('PENDIENTE','APROBADA','RECHAZADA')),
 id_aprobador INTEGER REFERENCES usuario(id_usuario),
 fecha_resolucion TIMESTAMP,
 motivo_resolucion TEXT,
 id_movimiento INTEGER UNIQUE REFERENCES movimiento_inventario(id_movimiento_inventario),
 CHECK(diferencia=cantidad_contada-existencia_sistema)
);
CREATE INDEX IF NOT EXISTS ix_ajuste_estado ON solicitud_ajuste(estado);
CREATE INDEX IF NOT EXISTS ix_movimiento_usuario ON movimiento_inventario(id_usuario);
CREATE OR REPLACE FUNCTION validar_rol_usuario() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM rol WHERE id_rol=NEW.id_rol
               AND nombre IN ('Administrador','Cajero','Bodeguero')) THEN
   RAISE EXCEPTION 'El rol de usuario no está permitido' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS usuario_rol_permitido ON usuario;
CREATE TRIGGER usuario_rol_permitido BEFORE INSERT OR UPDATE OF id_rol ON usuario
 FOR EACH ROW EXECUTE FUNCTION validar_rol_usuario();
COMMIT;
