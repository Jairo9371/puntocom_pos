# Despliegue y recuperación de PUNTO COM

Esta configuración prepara un servidor Linux con Docker Compose: Caddy publica
HTTPS, Gunicorn atiende Flask y PostgreSQL 18 conserva sus datos en un volumen.
Los puertos de Flask y PostgreSQL no se publican. La aplicación utiliza el rol
`puntocom_app`, sin permisos de crear bases, roles o tablas. Las tareas de
instalación y migración usan el perfil administrativo `maintenance` explícito.
El contenedor web no modifica el esquema al arrancar.

## Preparación del servidor

1. Instala Docker Engine y el complemento Docker Compose en Linux. Reserva
   inicialmente al menos 2 CPU y 4 GB de RAM; mide consumo con las ventas reales.
2. Copia el repositorio a `/srv/puntocom_pos`, conservando `.env` locales fuera
   de Git. Configura DNS de un dominio hacia el servidor y abre únicamente
   80/443 más el acceso administrativo restringido. PostgreSQL queda privado.
3. Copia `deployment/.env.example` a `deployment/.env`, con permisos `0600` y
   acceso solo al administrador. Define dominio, correo y tres claves aleatorias
   diferentes para `SECRET_KEY`, `POSTGRES_PASSWORD` y `APP_DB_PASSWORD`.
   Genera cada valor con `python -c "import secrets; print(secrets.token_hex(32))"`
   en una terminal privada; no lo envíes por chat ni lo pegues en comandos.
4. Crea `/srv/puntocom-backups` con permisos `0700`. Verifica que `BACKUP_DIR`
   apunta a esa ruta absoluta. No guardes respaldos en la carpeta pública web.
5. En la raíz del proyecto, valida sin imprimir el entorno y construye:

```bash
docker compose --env-file deployment/.env config --quiet
docker compose --env-file deployment/.env build app maintenance
docker compose --env-file deployment/.env up -d db
```

La imagen excluye los archivos `.env`, respaldos, Git y archivos locales. No uses
`docker compose config` sin `--quiet` en logs compartidos: expande secretos.
Las versiones Python se fijan mediante `requirements.lock`; las imágenes usan
etiquetas de versión mayor, que reciben actualizaciones. Para un despliegue
reproducible, registra y fija sus digest SHA-256 después de validar la imagen en
el servidor de pruebas. No se ha ejecutado Docker en el entorno Windows de este
repositorio; falta construir y comprobar la imagen en ese servidor.

## Instalación nueva o traslado de la base actual

Para un negocio sin datos previos, ejecuta una sola vez:

```bash
docker compose --env-file deployment/.env run --rm maintenance python -m deployment.instalar_nueva
```

Este comando exige una base vacía, instala `01_esquema.sql` y los datos de ejemplo
de `02_datos_iniciales.sql` en una transacción. Revisa los productos DEMO antes de
trabajar. No lo ejecutes para trasladar una base con ventas.

Para trasladar la instalación existente:

1. Cierra los cobros durante el respaldo final para evitar ventas posteriores a
   la copia. Crea y verifica el respaldo como explica la siguiente sección.
2. Transfiere el `.dump` y su `.dump.manifest.json` a `BACKUP_DIR` por un canal
   privado. Conserva también una copia fuera del servidor.
3. En el servidor, restaura exclusivamente en una base nueva:

```bash
docker compose --env-file deployment/.env run --rm maintenance python -m deployment.respaldos restore /backups/ARCHIVO.dump --database pos_restore_produccion
```

4. Cambia `DB_NAME=pos_restore_produccion` en `deployment/.env`. La base inicial
   queda intacta. El rol propietario existe en el servidor; el dump no restaura
   propietarios ni contraseñas de roles. Comprueba permisos antes de abrir acceso.

En ambos casos, aplica migraciones con el propietario, prepara los permisos y
crea el administrador si aún no existe:

```bash
docker compose --env-file deployment/.env run --rm maintenance python -m flask --app app migrar-operacion
docker compose --env-file deployment/.env run --rm maintenance python -m deployment.roles
docker compose --env-file deployment/.env run --rm app python -m flask --app app crear-admin
docker compose --env-file deployment/.env up -d app caddy
docker compose --env-file deployment/.env ps
```

`migrar-operacion` aplica 04, 05 y 06; nunca vuelve a ejecutar el esquema inicial.
El servidor web recibe únicamente la contraseña runtime. El perfil de mantenimiento
recibe la contraseña administrativa cuando se ejecuta una tarea explícita.
Si cambias `APP_DB_PASSWORD`, repite `deployment.roles` y recrea el contenedor web.
Cambiar `POSTGRES_PASSWORD` en el archivo no cambia la contraseña de un volumen ya
inicializado: coordina la rotación en PostgreSQL y la configuración administrativa.

## Comprobación antes de habilitar cajeros

Abre `https://TU_DOMINIO/health/live` y `/health/ready`: ambos deben responder 200;
readiness comprueba conexión y estructura requerida. Revisa inicio de sesión,
permisos, una venta, su reintento, estados de pago, ticket, compra y cierre de caja.
Valida los totales contra el respaldo restaurado. Confirma que el navegador marca
HTTPS válido y que PostgreSQL no acepta conexiones desde Internet.

Compose reinicia procesos que terminan y muestra el estado del healthcheck.
Un contenedor marcado `unhealthy` no se reinicia automáticamente solo por esa
marca: configura monitoreo externo con alerta sobre `/health/ready`, espacio de
disco, procesos y resultado del respaldo. Los logs Docker rotan a 3 archivos de
10 MB por servicio. Revisa los permisos y la retención del log de cron por separado.

Producción rechaza depuración, cookies sin HTTPS, claves vacías/cortas/con valores
de ejemplo y hosts vacíos. `TRUSTED_HOSTS` limita los dominios aceptados e incluye
localhost para el healthcheck interno. No publiques 8000 ni amplíes la red del
proxy: Gunicorn confía en el encabezado de HTTPS de Caddy en esa red privada.
Si usas PostgreSQL remoto, cambia el diseño de red y usa `DB_SSLMODE=verify-full`
con `DB_SSLROOTCERT` y un certificado válido; el `disable` del Compose corresponde
solo a su red privada dentro del mismo servidor.

## Respaldos verificables

En Windows o Linux, los clientes `pg_dump` y `pg_restore` deben estar en PATH
(o configura `PG_BIN`). Usa la misma versión mayor que el servidor, o una posterior
compatible. La conexión toma las variables del proceso; `--env-file` es explícito:

```powershell
.\venv\Scripts\python.exe -m deployment.respaldos --env-file .env backup --directory backups --verify-cleanup
```

En el servidor:

```bash
docker compose --env-file deployment/.env run --rm -T maintenance python -m deployment.respaldos backup --directory /backups --verify-cleanup
```

Cada copia utiliza `pg_dump --format=custom` y una instantánea exportada de solo
lectura. El manifiesto registra SHA-256, columnas y conteos desde la misma
instantánea, incluso si hay escrituras simultáneas. La verificación crea una base
`pos_verify_...` nueva, restaura en una transacción, compara los conteos y columnas,
y elimina únicamente esa base generada al pedir `--verify-cleanup`.
Los errores devuelven código distinto de cero sin mostrar contraseñas ni mensajes
del servidor. La contraseña se pasa a las herramientas por entorno, nunca por URI
ni por argumentos. El respaldo se renombra de `.partial` al finalizar.

Instala el ejemplo `deployment/backup-cron.example` ajustando la ruta y hora del
servidor. Hace una copia diaria y prueba su restauración; crea previamente el
log con permisos `0600`. Configura alertas por fallo y vigila que llegue una copia
nueva cada día. Un archivo cron en el repositorio no instala el planificador.

Un respaldo contiene datos personales y hashes de contraseñas. En Linux las
carpetas y archivos se crean con `0700`/`0600`; en Windows configura las ACL de
la carpeta para tu cuenta y administradores mediante las propiedades de seguridad
o `icacls`. `chmod` no garantiza ACL privadas en Windows. El SHA-256 detecta
corrupción accidental; no autentica una copia frente a un atacante que cambia
también su manifiesto. Restaura solo copias de procedencia confiable.

## Copia fuera del servidor y retención

La copia local, aunque esté verificada, se pierde si se pierde el servidor.
Configura además una transferencia diaria a otro equipo o almacenamiento privado,
por ejemplo `rsync` sobre SSH con clave restringida y directorio remoto privado.
Transfiere siempre los dos archivos, cifra el almacenamiento remoto y verifica
después de descargar una copia. Conserva una copia offline o inmutable si es posible.
Esta integración requiere elegir destino y credenciales; no queda activada por
añadir estos scripts.

Como política inicial, conserva 14 copias diarias, 8 semanales y 12 mensuales
fuera del servidor. Ajusta la frecuencia al máximo de ventas que puedas perder:
una copia diaria permite perder hasta 24 horas. El script no borra copias por
antigüedad; configura retención en el destino y comprueba antes que hay otra copia
válida. También respalda por separado los secretos de configuración y la clave
de acceso remoto en un gestor seguro, con acceso de recuperación documentado.

Cada mes, descarga una copia remota, verifica SHA y restaura sin tocar producción:

```bash
docker compose --env-file deployment/.env run --rm maintenance python -m deployment.respaldos verify /backups/ARCHIVO.dump --cleanup
```

Para una recuperación que debas inspeccionar, usa `restore` en lugar de `verify`:
genera `pos_restore_...` y la conserva. El script rechaza una base ya existente,
la base de origen, `DB_NAME`, nombres sin prefijo y cadenas de conexión. Nunca
usa `--clean`, `--create` o una restauración sobre tablas del negocio.

Antes de cambiar la aplicación a la base recuperada, aplica migraciones y permisos,
verifica tickets/totales/usuarios y acuerda el corte con los cajeros. Conserva la
base anterior hasta completar la revisión. No uses `docker compose down -v` para
actualizar: elimina los volúmenes persistentes.

## Actualizaciones y validación continua

GitHub Actions ejecuta pruebas aisladas y PostgreSQL en una base desechable; no
despliega. Localmente utiliza:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\venv\Scripts\python.exe tests/postgres/ejecutar.py --sin-comparar-instalacion
```

Antes de actualizar producción, realiza y verifica una copia, prueba la nueva
versión y sus migraciones en una restauración, construye las imágenes y programa
el corte. Ejecuta `migrar-operacion` y luego `deployment.roles` con el servicio web
detenido; vuelve a iniciar y comprueba readiness y una operación de caja.
La vuelta a código anterior solo es segura si acepta el esquema nuevo; revisa
compatibilidad antes de decidir restaurar datos y perder operaciones posteriores.

Referencias oficiales: [Flask en producción](https://flask.palletsprojects.com/en/stable/deploying/),
[Gunicorn](https://gunicorn.org/reference/settings/),
[Caddy HTTPS](https://caddyserver.com/docs/automatic-https),
[pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) y
[pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html).
