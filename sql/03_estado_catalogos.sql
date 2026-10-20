-- Migración para una instalación existente. Conserva registros y sus relaciones.
-- Puede ejecutarse nuevamente: no reactiva registros previamente desactivados.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT pg_advisory_xact_lock(hashtextextended('puntocom:migracion:03_estado_catalogos', 0));
ALTER TABLE public.categoria ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.cliente ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.proveedor ADD COLUMN IF NOT EXISTS estado BOOLEAN NOT NULL DEFAULT TRUE;
COMMIT;
