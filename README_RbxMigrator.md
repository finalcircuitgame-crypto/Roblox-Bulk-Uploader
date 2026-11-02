# RbxMigrator v3.35

**RbxMigrator** es una herramienta para migrar recursos de Roblox. Esta versión utiliza primero las API disponibles y añade reintentos automáticos para los recursos que fallen durante una migración.

## Contenido del paquete

- `RbxMigrator.py`: panel local y servidor auxiliar.
- `rbxmigrator_core.py`: lógica de migración, descarga e integración con Roblox/RBXDL.
- `RbxMigrator_Studio.plugin.lua`: código fuente del complemento para Roblox Studio.
- `Launch_RbxMigrator.bat`: iniciador para Windows.
- `test_*.py`: pruebas de regresión sin conexión.
- `README_EN.md`: instrucciones en inglés.

## Instalación y ejecución en Windows

1. Extrae el archivo ZIP en una carpeta que puedas modificar.
2. Instala Python 3.10 o una versión posterior si aún no lo tienes.
3. Abre PowerShell o el Símbolo del sistema en esa carpeta e instala la dependencia básica:

   ```powershell
   py -m pip install requests
   ```

4. Haz doble clic en `Launch_RbxMigrator.bat` o ejecuta:

   ```powershell
   py RbxMigrator.py
   ```

5. Si el panel no se abre automáticamente, visita `http://127.0.0.1:8765` en tu navegador.

El servidor auxiliar solo escucha en `127.0.0.1`; de forma predeterminada, no está expuesto a otros dispositivos de tu red.

## Complemento para Roblox Studio

Abre `RbxMigrator_Studio.plugin.lua` en Studio como código fuente de un complemento e instálalo mediante tu procedimiento habitual para complementos locales. La barra de herramientas y la ventana se llaman **RbxMigrator**. Se conservan los identificadores internos anteriores para evitar botones o ventanas duplicados al actualizar una instalación existente.

## Reintentos automáticos de recursos fallidos

Si una migración termina con recursos fallidos, el complemento vuelve a intentar migrar únicamente los recursos que siguen sin resolverse, con un máximo de **2 reintentos adicionales**. Los recursos que ya se migraron correctamente se reutilizan desde la caché para evitar volver a subirlos innecesariamente.

Durante cada reintento, el complemento muestra el estado en su ventana y escribe un aviso en la ventana **Output** de Roblox Studio. Si algunos recursos siguen fallando después del último intento, sus identificadores y los errores disponibles aparecen en el informe de migración y en el estado final.

Los reintentos no pueden solucionar permisos insuficientes, recursos privados inaccesibles, límites persistentes de Roblox/RBXDL ni otros errores permanentes.

## Descarga mediante API

El descargador comprueba primero la caché local, después Roblox Asset Delivery cuando la cuenta actual tiene acceso al recurso y, por último, el sistema masivo del portal RBXDL Creator Portal cuando está configurado. Las descargas individuales mediante navegador son opcionales y están desactivadas de forma predeterminada. Para habilitarlas, instala Playwright y activa la opción **Allow browser fallback** en la interfaz del descargador:

```powershell
py -m pip install playwright
```

La herramienta respeta la autenticación, los permisos, los límites de solicitudes y los tokens de descarga de Roblox/RBXDL. No evita las restricciones de acceso a recursos privados. Una solicitud de API correcta tampoco garantiza que todos los recursos se puedan descargar.

## Configuración existente y compatibilidad

- La aplicación conserva el nombre de la carpeta privada de configuración `RobloxTowerAnimationMigrator` para que las instalaciones anteriores puedan mantener su configuración y sesión de RBXDL. Es una ruta interna, no el nombre público del producto.
- Se conservan los nombres de las variables de entorno `TM_*` por compatibilidad con versiones anteriores.
- El espacio de trabajo de migración y la carpeta `migrations` se guardan junto a los archivos de Python. Conserva esa carpeta al actualizar si necesitas los trabajos, las correspondencias, las descargas o la caché anteriores.
- Cada persona debe ejecutar la herramienta con su propia cuenta de Windows y autenticar su propia sesión de Roblox/RBXDL. **No compartas tu carpeta de configuración, perfil de sesión, cookies ni credenciales.**

## Pruebas sin conexión

Ejecuta estos comandos desde la carpeta extraída:

```powershell
py -m py_compile RbxMigrator.py rbxmigrator_core.py
py test_backend_first.py
py test_browser_fallback.py
py test_v3_32.py
py test_v3_33.py
py test_v3_34.py
py test_v3_35.py
```

Estas pruebas comprueban el comportamiento local y no garantizan que las descargas reales de Roblox o RBXDL funcionen. Antes de procesar un lote grande, prueba con unos pocos recursos que tengas autorización para migrar.
