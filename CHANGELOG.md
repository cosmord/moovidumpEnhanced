# Changelog

Todas las modificaciones relevantes de MooviDump Enhanced se documentan en este archivo.

## [1.3.0] - 2026-09-09

### Añadido

- Interfaz web local para introducir credenciales, consultar cursos y lanzar descargas.
- Servidor TypeScript que comunica la interfaz con `main.py` y muestra el progreso en tiempo real.
- Selección de cursos por índice visible o por ID.
- Generación opcional de informes JSON de las descargas.
- Descargas paralelas con el número de workers configurable.
- Reanudación de descargas interrumpidas mediante archivos `.part`.
- Workflow de GitHub Actions para compilar `MooviDumpEnhanced.exe` en Windows y adjuntarlo a una release publicada.

### Mejorado

- Organización de archivos descargados por curso, sección y módulo.
- Interfaz gráfica Python con selección de cursos, persistencia opcional de credenciales y panel de logs.
- Experiencia visual y accesibilidad de la interfaz web.
- Mensajes de estado y seguimiento del progreso de las descargas.
- Reintentos, timeouts y gestión de errores de red.

### Seguridad

- `MOODLE_SITE` debe usar una URL HTTPS válida sin credenciales embebidas.
- Los valores guardados en `.env` se escapan para evitar que comillas o saltos de línea rompan el archivo.
- La interfaz web valida el origen de las peticiones POST y limita el tamaño de los cuerpos JSON.
- Los nombres de cursos se insertan de forma segura en el DOM, sin interpretar HTML recibido desde Moodle.

### Compatibilidad

- Python 3.10 o posterior.
- `uv` como flujo principal de dependencias.
- `requirements.txt` se mantiene para instalaciones basadas en `pip`.

### Verificación

- La suite contiene 6 pruebas automatizadas.
- El servidor web se valida mediante compilación TypeScript.
