# Luraph Fusion

Desvirtualizador de Lua/Luau para Luraph v14.7, v14.8, v14.9 y v15.
Combina un motor MIT de recuperación estructural con una integración opcional
para los motores de KryptIT. [Origen y licencias](THIRD_PARTY.md).

## Instalación

Necesitas Python 3.10+, Node.js 18+, Git, CMake y un compilador C++17.
No requiere paquetes de Python ni npm. Ejecuta desde la raíz del repositorio:

```bash
python scripts/build_runtime.py --jobs 2
python -m luraph protegido.lua -o recuperado.luau
```

La construcción fija Luau 0.739 y prepara `luau` y `luau-ast` para tu sistema.
En Windows usa una consola con las herramientas de compilación de Visual Studio.
El CLI se ejecuta desde el checkout; instalar solo el paquete Python no incluye
los motores ni las herramientas nativas.

## Uso

```bash
# Detectar sin ejecutar el archivo ni necesitar Luau
python -m luraph protegido.lua --detect

# Cabecera eliminada o versión ambigua
python -m luraph protegido.lua --engine 14.8 -o recuperado.luau

# Conservar una recuperación parcial, identificada en el informe
python -m luraph protegido.lua --allow-partial

# Aceptar únicamente el comportamiento observado, sin levantar la VM
python -m luraph protegido.lua --trace-only

# Procesar una carpeta; -o designa una carpeta cuando hay varios archivos
python -m luraph ./muestras -o ./resultados

# Comparar motores con una copia local de KryptIT
python -m luraph protegido.lua --backend all --kryptit-root ../kryptit --allow-partial
```

La copia de KryptIT debe conservar su estructura original y tener sus propias
herramientas Luau preparadas. Sus resultados se marcan `unverified` porque su
CLI externo no proporciona evidencia uniforme del tipo de recuperación.
`--allow-partial` permite seleccionarlos. No se descargan ni ejecutan automáticamente.

El motor base ofrece recuperación de constantes bajo demanda, levantamiento
simbólico, reconstrucción de flujo y variables, closures y emulación de Roblox.
Cada archivo se procesa en un proceso separado; los límites `--timeout` (por
pasada), `--budget` (emulación) y `--wall-timeout` (por motor) acotan el trabajo.
En POSIX, agotar el límite elimina todo el grupo de procesos. En Windows se usa
`taskkill /T`. No hay ejecución concurrente que comparta el estado de los motores.

## Resultados verificables

Cada intento terminado genera `<salida>.json`: versión detectada, motor usado,
SHA256 del archivo original y del resultado, sintaxis, marcadores pendientes,
diagnósticos y razones de rechazo. La selección prioriza recuperación estructural
y menos marcadores pendientes, sin usar el tamaño del código como medida de calidad.

| Estado | Significado | Aceptación |
| --- | --- | --- |
| `lifted` | El motor devolvió su resultado de lifting, pasó el parser y no contiene marcadores conocidos pendientes | Por defecto |
| `partial` | Lifting con bloques o closures sin resolver | `--allow-partial` |
| `unverified` | Resultado del motor externo sin evidencia uniforme del modo usado | `--allow-partial` |
| `trace` | Comportamiento observado durante la ejecución | `--trace-only` o `--allow-trace` |
| `invalid` / `failed` | Sintaxis inválida, resultado vacío, fallo o timeout | Nunca |

Código de salida: 0 si todos los archivos producen un resultado aceptable,
1 si alguno falla, 2 para argumentos inválidos y 130 al interrumpir.
Los rechazos conservan cualquier salida anterior y escriben un informe nuevo;
consulta `selected` y los hashes para distinguirlo de un resultado vigente.

## Límites y pruebas

`lifted` no garantiza equivalencia semántica ni recuperación completa. La sintaxis
y los marcadores conocidos no detectan todos los errores. Los nombres y comentarios
originales no se pueden recuperar. Las variantes nuevas y el código que depende
del estado real de Roblox pueden quedar incompletos. v14.8/v14.9 tienen limitaciones
del motor base para objetos VM construidos dinámicamente; KryptIT es una alternativa
externa. Sin un banner no se adivina la versión menor de v14.
Las listas variádicas mutables usadas como frame en el prólogo se rechazan como
recuperación parcial; el motor todavía no conserva ese estado entre instrucciones.

```bash
python -m unittest discover -s tests -v
```

Las pruebas cubren detección, selección, protección de archivos y límites de
procesos. Con Luau construido también comprueban sintaxis, tracing y lifting
sobre una VM sintética cuyo comportamiento se compara con el resultado.
La integración de KryptIT se comprueba mediante un doble de su CLI, sin afirmar
cobertura real de todas sus versiones. Los archivos de entrada se ejecutan en
un entorno Luau emulado; la emulación no es una barrera de aislamiento del sistema.


## Bot de Discord: `.luraph`

`bot.py` incorpora el comando `.luraph` del bot adjunto. Solo el usuario de
`DISCORD_OWNER_ID` puede ejecutarlo. Activa **Message Content Intent** en el
Developer Portal de Discord y permite al bot enviar mensajes y adjuntar archivos.

```bash
python prepare_engine.py --jobs 2
export DISCORD_TOKEN="tu_token"
export DISCORD_OWNER_ID="tu_id_de_usuario"
python bot.py
```

La preparación instala `requirements-bot.txt` y construye Luau. Necesita las
herramientas de instalación indicadas arriba. Si guardas `bot.py` y
`prepare_engine.py` fuera del checkout, descarga una revisión fija del motor en
`luraph-fusion/`. `LURAPH_RECOVER_DIR` permite usar otro checkout instalado.
El token se lee del entorno y no se entrega a los procesos de recuperación.
Si el motor falta al arrancar, el bot puede conectarse y el primer `.luraph`
ejecuta automáticamente `prepare_engine.py --skip-prometheus`. Guarda ambos
archivos juntos; la primera compilación puede tardar varios minutos y necesita
Git, CMake y un compilador C++17. Se vuelve a comprobar la instalación en cada
comando, así que prepararlo después del arranque no deja un error permanente.
El bot y la preparación buscan también un checkout en el directorio de trabajo.
Una ruta relativa de `LURAPH_RECOVER_DIR` se interpreta desde la carpeta del script.

En Discord, envía `.luraph` con un adjunto `.lua`, `.luau` o `.txt`, o utiliza
`.luraph https://ejemplo.com/archivo.lua` con una URL directa. Recibirás
`resultado.lua`. Si el archivo no tiene cabecera, añade la versión:
`.luraph 14.8` con el adjunto, o `.luraph https://ejemplo.com/archivo.lua 14.8`.
Se aceptan recuperaciones parciales con un aviso en el mensaje y en el archivo;
las trazas de comportamiento no se devuelven como desvirtualización.

El bot limita la entrada a 2 MiB, comprueba el límite de subida de Discord y
procesa un archivo a la vez. El motor dispone de 240 segundos por ejecución y
el bot cancela el trabajo a los 300 segundos. Cada trabajo usa un directorio
temporal y la caché de `.luraph` está separada de `.deobf`.

`.deobf` y `.get` conservan su uso anterior. `.deobf` requiere una instalación
independiente de Prometheus, Node 22+ y Lua 5.1, configurable con
`DEOBF_RECOVER_DIR`. Su ausencia no impide usar `.luraph`. La preparación
conserva los parches del script adjunto cuando encuentra Prometheus; usa
`--skip-prometheus` para preparar únicamente Luraph.

### Rendimiento

El motor inicia la sesión para decodificar constantes solo cuando encuentra una constante pendiente; los archivos que ya contienen todo lo necesario evitan ejecutar otra vez su bootstrap. Los resultados del bot en caché se entregan sin preparar de nuevo el motor. En tres mediciones alternadas con el ejemplo MIT `5ae248d6527b5c01.lua`, la mediana del motor bajó de 3,177 s a 1,971 s (38 %), con el mismo resultado Lua. El tiempo depende del archivo y del servidor; los límites de recuperación y las comprobaciones del resultado se conservan.

#### Caché de disco y respuestas durante otra recuperación

Configura `LURAPH_CACHE_DIR` para guardar hasta 128 MiB de resultados verificados (máximo 256 entradas) en SQLite. La caché no guarda los archivos de entrada. Verifica tamaño y SHA256 al leer y separa los resultados cuando cambia el código del bot, el motor o sus binarios. Los errores de disco permiten continuar con una recuperación normal. La lectura/escritura de disco ocurre fuera del bucle de Discord. Los resultados en caché pueden entregarse mientras otro archivo se desvirtualiza.

Para conservar la caché entre reconstrucciones del contenedor, monta un volumen del hosting en `/data` y establece `LURAPH_CACHE_DIR=/data/luraph-cache`. Sin volumen, la caché depende del disco efímero del contenedor. En una prueba local con recuperación real y la red Discord simulada, el primer archivo tardó 2,08 s y cinco entregas desde disco con instancias nuevas del bot tuvieron mediana 1,86 ms. Esto acelera archivos repetidos; no convierte archivos nuevos en recuperaciones instantáneas. Las rondas de runtime también usan notificaciones de salida/fin en lugar de esperas fijas de 100 ms, manteniendo los límites de silencio y tiempo total.

### Closures de Lua preservadas

Las funciones que Luraph conserva como código Lua se reconstruyen con sus
variables capturadas, incluidas las cajas que comparten lecturas y escrituras
con el padre. Se admiten fuentes de varias líneas y cuerpos largos. Una
closure que no puede reconstruirse emite un marcador `devirt:` y se clasifica
como parcial; no se sustituye silenciosamente por una función vacía.

La regresión `tests/fixtures/vm-plain-closure.lua` comprueba el pipeline v15
y compara el comportamiento original y recuperado (23, 42, 42). Las pruebas
de closures verifican también callbacks anidados, cajas independientes,
upvalues, bytes de cadenas y cuerpos de más de 2000 bytes.
