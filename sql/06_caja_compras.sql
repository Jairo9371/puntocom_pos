-- Aditiva e idempotente. Los cierres y las compras anteriores no se reinterpretan.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SELECT pg_advisory_xact_lock(hashtextextended('puntocom:migracion:06_caja_compras',0));
ALTER TABLE turno_caja ADD COLUMN IF NOT EXISTS efectivo_esperado NUMERIC(12,2);
ALTER TABLE turno_caja ADD COLUMN IF NOT EXISTS diferencia NUMERIC(12,2);
ALTER TABLE turno_caja ADD COLUMN IF NOT EXISTS motivo_diferencia VARCHAR(500);

ALTER TABLE compra ADD COLUMN IF NOT EXISTS saldo_pendiente NUMERIC(12,2);
ALTER TABLE compra ADD COLUMN IF NOT EXISTS clave_operacion VARCHAR(80);
ALTER TABLE compra ADD COLUMN IF NOT EXISTS huella_operacion VARCHAR(64);
ALTER TABLE compra ADD COLUMN IF NOT EXISTS origen_efectivo VARCHAR(20);
CREATE UNIQUE INDEX IF NOT EXISTS uq_compra_operacion ON compra(id_usuario,clave_operacion)
    WHERE clave_operacion IS NOT NULL;

CREATE TABLE IF NOT EXISTS pago_compra (
    id_pago_compra SERIAL PRIMARY KEY,
    id_compra INTEGER NOT NULL REFERENCES compra(id_compra),
    id_usuario INTEGER NOT NULL REFERENCES usuario(id_usuario),
    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    monto NUMERIC(12,2) NOT NULL CHECK(monto>0),
    metodo_pago VARCHAR(30) NOT NULL CHECK(metodo_pago IN ('EFECTIVO','TARJETA','TRANSFERENCIA')),
    origen_efectivo VARCHAR(20),
    id_movimiento_caja INTEGER UNIQUE REFERENCES movimiento_caja(id_movimiento_caja),
    clave_operacion VARCHAR(80),
    huella_operacion VARCHAR(64),
    UNIQUE(id_usuario,clave_operacion),
    CHECK((clave_operacion IS NULL)=(huella_operacion IS NULL)),
    CHECK((metodo_pago='EFECTIVO' AND origen_efectivo IS NOT NULL AND origen_efectivo IN ('CAJA','EXTERNO'))
       OR (metodo_pago<>'EFECTIVO' AND origen_efectivo IS NULL)),
    CHECK((id_movimiento_caja IS NOT NULL)=(origen_efectivo IS NOT DISTINCT FROM 'CAJA'))
);
CREATE INDEX IF NOT EXISTS ix_pago_compra_compra ON pago_compra(id_compra);

DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_turno_snapshot_cierre'
                  AND conrelid='turno_caja'::regclass) THEN
        ALTER TABLE turno_caja ADD CONSTRAINT ck_turno_snapshot_cierre CHECK(
            (efectivo_esperado IS NULL AND diferencia IS NULL AND motivo_diferencia IS NULL)
            OR (efectivo_esperado IS NOT NULL AND diferencia IS NOT NULL
                AND estado='CERRADO' AND efectivo_contado IS NOT NULL
                AND diferencia=efectivo_contado-efectivo_esperado
                AND (diferencia=0 OR LENGTH(TRIM(COALESCE(motivo_diferencia,'')))>0)));
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_compra_saldo_pendiente'
                  AND conrelid='compra'::regclass) THEN
        ALTER TABLE compra ADD CONSTRAINT ck_compra_saldo_pendiente CHECK(
            saldo_pendiente IS NULL OR (saldo_pendiente>=0 AND saldo_pendiente<=total));
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_compra_clave_huella'
                  AND conrelid='compra'::regclass) THEN
        ALTER TABLE compra ADD CONSTRAINT ck_compra_clave_huella CHECK(
            (clave_operacion IS NULL)=(huella_operacion IS NULL));
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_compra_origen_efectivo'
                  AND conrelid='compra'::regclass) THEN
        ALTER TABLE compra ADD CONSTRAINT ck_compra_origen_efectivo CHECK(
            origen_efectivo IS NULL OR
            (metodo_pago='EFECTIVO' AND origen_efectivo IN ('CAJA','EXTERNO')));
    END IF;
END $$;
COMMIT;
