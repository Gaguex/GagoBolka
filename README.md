# GagoBolka

**GagoBolka** convierte archivos de texto en audio desde una interfaz de terminal. Incluye tres motores TTS: Piper y Kokoro ONNX para síntesis local, además de Microsoft Edge TTS, que requiere internet.

Procesa documentos extensos por bloques y conserva el progreso mediante checkpoints para poder reanudar una tarea interrumpida.

## Motores de voz

| Motor | Conexión | Descripción |
|---|---|---|
| Piper | Local | Síntesis con modelos de voz descargados. |
| Kokoro ONNX | Local | Voces en varios idiomas usando ONNX Runtime. Prefiere el modelo INT8 cuando está disponible. |
| Microsoft Edge TTS | Internet | Voces neuronales en varios idiomas, incluido inglés. |

La velocidad y el resultado dependen del motor, la voz, el texto, el equipo y, para Edge, la conexión.

## Funciones

- Convierte archivos `.txt` grandes en bloques.
- Permite seleccionar motor, voz, velocidad, límite de CPU, formato y carpeta de salida desde la terminal.
- Guarda checkpoints en `meta.json` y permite reanudar el trabajo.
- Limpia enlaces, emojis, caracteres invisibles y separadores decorativos antes de sintetizar.
- Exporta MP3 a 64, 96 o 128 kbps, además de WAV.
- Edge puede conservar su MP3 original a 48 kbps y concatenar los bloques sin recodificación.
- Reintenta solicitudes fallidas de Edge y aplica un timeout a los bloques de Kokoro.

## Requisitos

- Windows 10/11 de 64 bits.
- Python de 64 bits para ejecutar desde el código. Python 3.13 es una opción recomendada para Kokoro.
- FFmpeg para exportar MP3 y concatenar bloques.
- Piper y al menos un modelo de voz para usar Piper.
- El modelo ONNX y el archivo de voces para usar Kokoro.
- Conexión a internet para usar Edge TTS.

Los modelos de voz y algunas dependencias pueden tener licencias propias. Revisa sus términos antes de redistribuirlos.

## Instalación

Clona el repositorio:

```powershell
git clone https://github.com/Gaguex/GagoBolka.git
cd GagoBolka
```

Crea un entorno virtual e instala las dependencias opcionales:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install edge-tts kokoro-onnx soundfile
```

Coloca Piper en `piper/piper.exe` y sus modelos ONNX y archivos JSON en `voces/`.

Para Kokoro, coloca `kokoro-v1.0.onnx` y `voices-v1.0.bin` en `voces_kokoro/`. El modelo `kokoro-v1.0.int8.onnx` es opcional y se selecciona automáticamente si está presente.

Coloca FFmpeg en `ffmpeg/ffmpeg.exe` o asegúrate de que esté disponible en `PATH`.

## Ejecutar

Desde PowerShell, dentro de la carpeta del proyecto:

```powershell
.\.venv\Scripts\python.exe .\gagobolka3_4.py
```

Sigue las opciones de la terminal para seleccionar el archivo, motor, voz, velocidad, límite de CPU, formato y carpeta de salida.

Los checkpoints se guardan dentro de `temp_gagodbolka/`. Para reanudar, vuelve a seleccionar el mismo archivo y los mismos ajustes.

## Formatos de audio

- MP3 a 64, 96 o 128 kbps.
- WAV sin comprimir.
- MP3 original de Edge a 48 kbps, concatenado sin recodificar.

WAV ocupa bastante más espacio que MP3.

## Crear un ejecutable con PyInstaller

Instala PyInstaller en el entorno virtual:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
```

Compila en modo de un directorio y conserva la consola:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --onedir --console --name GagoBolka --collect-all kokoro_onnx --collect-all espeakng_loader --collect-all onnxruntime --collect-all phonemizer --collect-all _soundfile_data --copy-metadata kokoro-onnx --copy-metadata phonemizer-fork .\gagobolka3_4.py
```

Copia las carpetas `piper`, `ffmpeg`, `voces` y `voces_kokoro` a `dist\GagoBolka`. Distribuye esa carpeta completa, incluidos `GagoBolka.exe` y `_internal`.

## Solución de problemas

**Edge aparece como no disponible.** Instala `edge-tts` en el entorno que utilizas para ejecutar la aplicación:

```powershell
.\.venv\Scripts\python.exe -m pip install edge-tts
```

**Kokoro aparece como no disponible.** Instala sus dependencias en el mismo entorno:

```powershell
.\.venv\Scripts\python.exe -m pip install kokoro-onnx soundfile
```

Comprueba también que los archivos del modelo y las voces estén en `voces_kokoro/`.

**Piper o FFmpeg no se encuentran.** Comprueba las rutas `piper/piper.exe` y `ffmpeg/ffmpeg.exe`.

## Licencia

El código de este repositorio se distribuye bajo la licencia MIT, incluida en [`LICENSE`](LICENSE). Los modelos de voz, motores TTS y FFmpeg pueden tener condiciones de licencia independientes.
