-- Migración aditiva, transaccional e idempotente. No consulta servicios bancarios.
-- Las ventas históricas conservan su estado y se consideran confirmadas manualmente.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SELECT pg_advisory_xact_lock(hashtextextended('puntocom:migracion:05_ventas_pagos',0));
ALTER TABLE venta ADD COLUMN IF NOT EXISTS estado_pago VARCHAR(20) NOT NULL DEFAULT 'CONFIRMADO';
ALTER TABLE venta ADD COLUMN IF NOT EXISTS clave_operacion VARCHAR(100);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS hash_solicitud VARCHAR(64);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS resultado_cobro JSONB;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS recibido NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS cambio NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS id_confirmador INTEGER REFERENCES usuario(id_usuario);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS fecha_confirmacion TIMESTAMP;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS id_resolutor INTEGER REFERENCES usuario(id_usuario);
ALTER TABLE venta ADD COLUMN IF NOT EXISTS fecha_resolucion TIMESTAMP;
ALTER TABLE venta ADD COLUMN IF NOT EXISTS motivo_pago TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_venta_usuario_operacion ON venta(id_usuario,clave_operacion);
CREATE INDEX IF NOT EXISTS ix_venta_pago ON venta(estado_pago,fecha_hora);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='venta'::regclass AND conname='venta_estado_pago_valido') THEN
  ALTER TABLE venta ADD CONSTRAINT venta_estado_pago_valido CHECK(
   (estado_pago='CONFIRMADO' AND estado IN ('COMPLETADA','ANULADA')) OR
   (estado_pago='PENDIENTE' AND estado='PENDIENTE' AND metodo_pago IN ('TARJETA','TRANSFERENCIA')) OR
   (estado_pago='FALLIDO' AND estado='FALLIDA' AND metodo_pago IN ('TARJETA','TRANSFERENCIA')));
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='venta'::regclass AND conname='venta_importes_pago_validos') THEN
  ALTER TABLE venta ADD CONSTRAINT venta_importes_pago_validos CHECK(recibido>=0 AND cambio>=0);
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='venta'::regclass AND conname='venta_operacion_completa') THEN
  ALTER TABLE venta ADD CONSTRAINT venta_operacion_completa CHECK(
   (clave_operacion IS NULL AND hash_solicitud IS NULL) OR
   (clave_operacion IS NOT NULL AND hash_solicitud IS NOT NULL AND
    clave_operacion ~ '^[A-Za-z0-9_-]{16,100}$' AND hash_solicitud ~ '^[a-f0-9]{64}$'));
 END IF;
END $$;
COMMIT;
