# Pruebas de PUNTO COM en Vercel

El repositorio incluye la configuración para desplegar Flask en Vercel con
Python 3.12. PostgreSQL debe estar alojado aparte y ser accesible desde Vercel.
Esta preparación no crea una cuenta, una base remota ni un despliegue.

## Qué hace la configuración

- `vercel.json` selecciona Flask y ejecuta `python -m deployment.preparar_vercel`
  durante la compilación. Vercel detecta la instancia `app` de `app.py`.
- El paso de compilación copia `static/` a `public/static/`. Así Vercel sirve
  `/static/style.css` desde su CDN y las pantallas conservan sus direcciones.
  Para trabajar localmente y con Docker se sigue utilizando `static/`.
- `.python-version` fija Python 3.12 y `requirements.txt` aplica las versiones
  restringidas por `requirements.lock`.
- Con `VERCEL=1`, la aplicación usa `production` como ambiente predeterminado,
  también en las vistas previas. Sigue exigiendo una `SECRET_KEY` segura,
  depuración desactivada y cookies HTTPS.
- Los hosts exactos de `VERCEL_URL`, `VERCEL_BRANCH_URL` y
  `VERCEL_PROJECT_PRODUCTION_URL` se agregan a `TRUSTED_HOSTS`. Los dominios
  personalizados adicionales se configuran en `TRUSTED_HOSTS`, sin esquema ni
  puerto. No se permite de forma general cualquier dominio `vercel.app`.
- En Vercel se usa `NullPool`: las conexiones PostgreSQL se cierran al devolverlas,
  evitando mantener un grupo de conexiones inactivas en cada instancia.
- `.vercelignore` y las exclusiones de la función apartan secretos, respaldos,
  entornos virtuales y otros archivos locales del despliegue.

La compilación no crea tablas, no aplica migraciones ni crea usuarios. La
administración de la base se ejecuta por separado desde un equipo autorizado.

## 1. Preparar PostgreSQL para pruebas

Crea una base **nueva y separada de los datos del negocio**, por ejemplo
`puntocom_pruebas`. Anota host, puerto, nombre de base, usuario y contraseña.
Habilita el acceso de red necesario desde Vercel según las opciones del proveedor.

Usa preferiblemente una conexión directa o un pooler en modo sesión. La aplicación
envía la opción de inicio `timezone=America/Guatemala`, utiliza transacciones y
aislamiento `READ COMMITTED`; comprueba que el servicio los admita. Los poolers en
modo transacción requieren una validación específica y no deben asumirse
compatibles con esta configuración.

Configura TLS conforme al proveedor. La plantilla propone `DB_SSLMODE=verify-full`;
si requiere un certificado CA, guarda el certificado público en una ruta como
`certs/postgres-ca.crt` y configura `DB_SSLROOTCERT` para que exista tanto en el
equipo de mantenimiento como en Vercel. Un archivo que solo existe en tu PC no
estará disponible en la función. No uses `disable` para una conexión por Internet.

En el editor SQL del proveedor, conectado como propietario de la base nueva,
ejecuta una sola vez y en este orden:

1. `sql/01_esquema.sql`.
2. `sql/02_datos_iniciales.sql`.

El segundo archivo incluye roles, catálogos y productos DEMO con existencias.
El esquema inicial es exclusivo para una base vacía: no lo ejecutes en una
instalación existente ni en la base del negocio.

## 2. Aplicar migraciones y crear el administrador

Desde la raíz del proyecto y en una terminal privada nueva, prepara un archivo
local independiente del `.env` habitual:

```powershell
Copy-Item deployment/vercel.env.example .env.vercel.local
```

Rellena `.env.vercel.local` con la conexión **de pruebas**, su configuración TLS
y una `SECRET_KEY` aleatoria estable de al menos 32 caracteres. Para estos comandos
locales, configura además `TRUSTED_HOSTS=localhost`: fuera de Vercel no se agregan
los hosts automáticos. El archivo está ignorado por Git y por Vercel.

Genera la clave en una terminal privada y conserva el mismo valor para el
despliegue. No copies los secretos al repositorio, a capturas ni a conversaciones.

Usa un usuario con permisos para modificar el esquema al aplicar las migraciones:

```powershell
.\venv\Scripts\python.exe -m flask --app app --env-file .env.vercel.local migrar-operacion
.\venv\Scripts\python.exe -m flask --app app --env-file .env.vercel.local crear-admin
```

`crear-admin` solicita nombre, correo y una contraseña de al menos 12 caracteres.
Si el administrador ya existe, no vuelvas a crearlo. El usuario de conexión de ese
comando necesita permisos para consultar roles e insertar usuarios.

`--env-file` se carga antes de importar la aplicación, por lo que sus valores
prevalecen frente al `.env` habitual que carga `app.py`. Las variables ya exportadas
en el proceso sí tienen prioridad: usa una terminal sin valores `DB_*`,
`SECRET_KEY` o `APP_ENV` de otra instalación. Comprueba el destino configurado
antes de cualquier operación de mantenimiento.

En Vercel utiliza un usuario de aplicación con los permisos de operación
necesarios. No necesita crear bases, roles o tablas. El propietario usado para
migrar y el usuario de aplicación pueden ser distintos; asigna los permisos de
tablas y secuencias según el proveedor. La guía de [despliegue con Docker](despliegue.md)
describe el esquema de permisos propio de esa modalidad.

## 3. Configurar el proyecto de Vercel

1. Sube el repositorio a tu proveedor Git y selecciona **Add New → Project** en
   Vercel para importarlo.
2. Selecciona la raíz del repositorio como **Root Directory** y el preset
   **Flask**. Conserva el comando de compilación de `vercel.json`; no indiques
   un directorio de salida de un sitio estático ni `gunicorn` como comando.
3. En **Settings → Environment Variables**, agrega los valores de
   `deployment/vercel.env.example` para **Preview** y, si usarás ese entorno para
   pruebas, **Production**. Introduce los secretos como valores privados.
4. Activa **Automatically expose System Environment Variables** para disponer
   de `VERCEL` y las URLs del despliegue. No inventes ni fijes manualmente esas
   variables del sistema.
5. `TRUSTED_HOSTS` puede quedar vacío si solo usarás las URLs que Vercel expone.
   Si agregas un dominio propio que no figure entre ellas, inclúyelo explícitamente.
6. Publica cuando la base esté preparada. Si cambias variables de entorno,
   genera un nuevo despliegue para que se apliquen.

La configuración Compose, Caddy y los volúmenes de PostgreSQL corresponden al
despliegue en servidor Linux. No se levantan como parte de este despliegue Flask.

## 4. Comprobar el despliegue

Abre las siguientes rutas bajo la URL HTTPS de Vercel:

| Ruta | Resultado esperado |
| --- | --- |
| `/health/live` | HTTP 200 con `ok: true`; la aplicación responde. |
| `/health/ready` | HTTP 200 con `ok: true`; la base y el esquema están disponibles. |
| `/static/style.css` | HTTP 200 con la hoja de estilos. |
| `/login` | Formulario de inicio de sesión con estilos. |

Después comprueba inicio de sesión, permisos, apertura de caja, una venta,
existencias, ticket, exportación Excel y cierre de caja. Usa únicamente la base de
pruebas y revisa los errores en **Runtime Logs** sin publicar datos privados.

Si `/health/live` funciona y `/health/ready` devuelve 503, revisa conexión de red,
credenciales, certificados, permisos y migraciones. Un error de host requiere
comprobar las variables de sistema y `TRUSTED_HOSTS`.

Los respaldos y su restauración deben configurarse con el proveedor PostgreSQL o
desde mantenimiento externo con `pg_dump` y `pg_restore`. La programación de
Windows y el cron del servidor no se trasladan automáticamente a Vercel. La
validación de una publicación real y de su conexión remota queda pendiente hasta
que se configuren la cuenta, la base y las variables.

Referencias oficiales: [Flask en Vercel](https://vercel.com/docs/frameworks/backend/flask),
[runtime Python](https://vercel.com/docs/functions/runtimes/python) y
[variables del sistema](https://vercel.com/docs/environment-variables/system-environment-variables).
