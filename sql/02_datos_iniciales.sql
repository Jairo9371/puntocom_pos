-- SE PUEDE EJECUTAR SOBRE LA BASE DE DATOS DEL PROYECTO YA CREADA.
INSERT INTO rol(nombre) VALUES ('Administrador'),('Cajero'),('Bodeguero') ON CONFLICT(nombre) DO NOTHING;
INSERT INTO categoria(nombre,descripcion) VALUES
('Libros','Libros escolares'),('Cuadernos','Cuadernos y libretas'),('Lapiceros','Bolígrafos'),('Lápices','Lápices de grafito'),('Marcadores','Marcadores escolares'),('Papel','Resmas y hojas'),('Carpetas','Folders y carpetas'),('Material escolar','Suministros escolares'),('Oficina','Material de oficina'),('Tecnología','Tecnología y accesorios') ON CONFLICT(nombre) DO NOTHING;
INSERT INTO cliente(nombre_razon_social,nit_dpi) SELECT 'Consumidor Final','CF' WHERE NOT EXISTS (SELECT 1 FROM cliente WHERE nit_dpi='CF');
-- Opcional: productos ficticios, con códigos DEMO para evitar chocar con PROD001..., etc.
INSERT INTO producto(id_categoria,codigo,codigo_barras,nombre,marca,unidad,costo,precio_venta,stock_minimo)
SELECT c.id_categoria,'DEMO001','900000001','Cuaderno universitario 100 hojas','Norma','Unidad',8.00,12.00,10 FROM categoria c WHERE c.nombre='Cuadernos' ON CONFLICT(codigo) DO NOTHING;
INSERT INTO producto(id_categoria,codigo,codigo_barras,nombre,marca,unidad,costo,precio_venta,stock_minimo)
SELECT c.id_categoria,'DEMO002','900000002','Lapicero azul','BIC','Unidad',1.50,2.50,20 FROM categoria c WHERE c.nombre='Lapiceros' ON CONFLICT(codigo) DO NOTHING;
INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo)
SELECT p.id_producto,'ENTRADA',50,p.costo,'Inventario demo' FROM producto p WHERE p.codigo='DEMO001' AND NOT EXISTS(SELECT 1 FROM movimiento_inventario m WHERE m.id_producto=p.id_producto);
INSERT INTO movimiento_inventario(id_producto,tipo,cantidad,costo_unitario,motivo)
SELECT p.id_producto,'ENTRADA',100,p.costo,'Inventario demo' FROM producto p WHERE p.codigo='DEMO002' AND NOT EXISTS(SELECT 1 FROM movimiento_inventario m WHERE m.id_producto=p.id_producto);
