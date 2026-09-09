# MooviDump Enhanced

Descarga y organiza los recursos de tus cursos de Moodle en la carpeta `dumps/`.
La salida sigue la estructura curso -> sección -> módulo -> archivo.

## Requisitos

- Python 3.10 o posterior.
- `uv` para instalar las dependencias de Python.
- Node.js y `pnpm` solo si vas a usar la interfaz web. Si `pnpm` no está instalado, `run.py` intenta instalarlo con `npm`.

El proyecto usa `pyproject.toml` y `uv.lock` como fuente principal de dependencias.
`requirements.txt` se conserva para flujos antiguos que todavía usan `pip`.

## Instalación rápida

1. Copia el archivo de ejemplo y completa tus credenciales si quieres guardarlas:

```powershell
copy example.env .env
```

Edita `.env` con estos valores:

```env
MOODLE_SITE="https://moovi.uvigo.gal"
MOODLE_USERNAME="tu_usuario"
MOODLE_PASSWORD="tu_contraseña"
```

2. Instala las dependencias:

```bash
uv sync
```

## Uso

### Lanzador interactivo

Ejecuta `run.py` para elegir entre terminal, interfaz gráfica o navegador local:

```powershell
uv run run.py
```

En el modo terminal puedes elegir si usar las credenciales de `.env`, introducirlas solo para esa ejecución o guardarlas.

También puedes seleccionar el modo directamente:

```powershell
uv run run.py --mode 1
uv run run.py --mode 2
uv run run.py --mode 3
```

### Línea de comandos

Ejecuta `main.py` si `MOODLE_SITE`, `MOODLE_USERNAME` y `MOODLE_PASSWORD` están definidos en el entorno o en `.env`:

```bash
uv run main.py [--force] [--verbose] [--jobs 4] [--report report.json]
```

Opciones disponibles:

- `--force`: fuerza la redescarga de archivos aunque ya existan.
- `--verbose`: activa el logging en nivel `DEBUG`.
- `--jobs`: establece el número máximo de descargas paralelas.
- `--report`: guarda un resumen JSON en la ruta indicada.
- `--all-courses`: descarga todos los cursos visibles sin preguntar.
- `--courses 1684,1685`: descarga los cursos indicados por índice visible o por ID.
- `--list-courses`: muestra los cursos visibles como JSON y termina.

Las descargas interrumpidas dejan un fichero `.part` y se reanudan en la siguiente ejecución si el servidor Moodle admite solicitudes parciales.

### Interfaz gráfica

La interfaz gráfica permite introducir las credenciales, guardar o no la contraseña en `.env`, elegir todos los cursos o una lista de IDs, forzar la redescarga y consultar el log:

```powershell
uv run run_gui.py
```

### Ejecutable de Windows

Para generar el ejecutable de la interfaz gráfica:

```powershell
.\build_exe.ps1
```

El resultado es `dist/MooviDumpEnhanced.exe`. El ejecutable incluye `main.py` como worker. La carpeta `dumps/` y el archivo `.env` se crean junto al ejecutable.

### Publicar el `.exe` en GitHub Releases

Cuando publicas una release en GitHub, Actions compila el ejecutable en Windows y adjunta `MooviDumpEnhanced.exe` a esa misma release. La release debe apuntar a una etiqueta, por ejemplo `v1.3.0`.

Para completar manualmente una release ya creada, como `v1.2.0`, ejecuta el workflow `Build and publish Windows executable` desde la pestaña **Actions** e introduce su etiqueta.

### Modo navegador local

El modo navegador busca una carpeta `web-server/`, `web/` o `frontend/`. Al seleccionarlo desde `run.py`, el lanzador comprueba `pnpm`, instala las dependencias si todavía no existe `node_modules/`, inicia el servidor y abre el navegador. También puedes hacerlo manualmente desde la carpeta del frontend:

```powershell
cd web-server
pnpm install
pnpm run dev
```

El servidor usa el puerto `3000` por defecto y busca el siguiente puerto libre si está ocupado. Puedes cambiar el puerto inicial con la variable de entorno `PORT`. Abre la URL que muestra el lanzador si el navegador no se abre automáticamente.

La interfaz web obtiene los cursos mediante `main.py --list-courses`, lanza las descargas como procesos hijos y muestra el progreso y el log.

## Estructura de salida

Por defecto, los archivos se guardan bajo `dumps/` con una estructura anidada por curso y sección:

```text
dumps/
└── FMI/
    ├── Tema 1/
    │   ├── Módulo A/
    │   │   └── recurso.pdf
    │   └── Módulo B/
    └── Tema 2/
        └── ...
```

Si `DUMP_ALL = True` en `main.py`, también se guardan snapshots JSON de cursos, secciones y módulos.

## Personalización

- `COURSE_ALIASES` en `main.py` permite asignar nombres de carpeta a IDs de curso.
- `DUMP_ALL` añade los snapshots JSON.
- `FULL_SANITIZER` reemplaza los espacios por guiones bajos en los nombres generados.

## Solución de problemas

- `login failed`: revisa el usuario, la contraseña y `MOODLE_SITE`.
- `Cannot connect`: comprueba la URL y la conectividad.
- `No courses found`: comprueba que tu usuario está matriculado en cursos visibles.
- Los archivos no se descargan: revisa los permisos del recurso en Moodle.

Usa `--verbose` para obtener más información en el log. No compartas credenciales al enviar un informe.

Gracias a Tyr7z por el código base.

Consulta el [CHANGELOG](CHANGELOG.md) para ver el historial de versiones.
