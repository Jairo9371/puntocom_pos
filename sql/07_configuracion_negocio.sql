-- Aditiva e idempotente. No modifica ventas, existencias ni datos del catálogo.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SELECT pg_advisory_xact_lock(hashtextextended('puntocom:migracion:07_configuracion_negocio',0));
CREATE TABLE IF NOT EXISTS configuracion_negocio (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK(id=1),
    nombre_comercial VARCHAR(150) NOT NULL DEFAULT 'PUNTO COM' CHECK(LENGTH(TRIM(nombre_comercial))>0),
    giro VARCHAR(150) NOT NULL DEFAULT 'Librería · Guatemala',
    direccion VARCHAR(300) NOT NULL DEFAULT '',
    telefono VARCHAR(20) NOT NULL DEFAULT '',
    correo VARCHAR(150) NOT NULL DEFAULT '',
    nit VARCHAR(30) NOT NULL DEFAULT '',
    mensaje_ticket VARCHAR(255) NOT NULL DEFAULT '¡Gracias por su compra!',
    ancho_ticket SMALLINT NOT NULL DEFAULT 80 CHECK(ancho_ticket IN (58,80)),
    actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_por INTEGER REFERENCES usuario(id_usuario)
);
INSERT INTO configuracion_negocio(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
COMMIT;
