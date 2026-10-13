import os
import re
import sys
import time
import json
import queue
import wave
import shutil
import hashlib
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# Intentar importar winsound para notificaciones en Windows
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# --- CARPETA DEL PROGRAMA (todo funciona "portable", sin instalar nada) ---
if getattr(sys, "frozen", False):          # si algún día se empaqueta como .exe
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def buscar_ejecutable(nombre):
    """Busca primero dentro de la carpeta del programa (ffmpeg.exe, piper.exe),
    y si no está, en el PATH del sistema."""
    exe = nombre + (".exe" if sys.platform == "win32" else "")
    for carpeta in (BASE_DIR, os.path.join(BASE_DIR, nombre), os.path.join(BASE_DIR, "bin")):
        ruta = os.path.join(carpeta, exe)
        if os.path.isfile(ruta):
            return ruta
    return shutil.which(nombre)


FFMPEG_EXE = buscar_ejecutable("ffmpeg")
PIPER_EXE = buscar_ejecutable("piper")

# --- CONFIGURACIÓN DE RUTAS ---
CARPETA_VOCES = "voces"
CARPETA_TEMP = "temp_gagodbolka"
CONFIG_FILE = "gagodbolka_config.json"

# --- OPCIONES DEL MENÚ ---
# Velocidad: número MÁS ALTO = voz MÁS RÁPIDA (intuitivo).
# Internamente Piper usa length_scale = 1 / velocidad.
VELOCIDADES = [
    ("Muy lenta", 0.75),
    ("Lenta / lectura clara", 0.85),
    ("Normal", 1.00),
    ("Rápida", 1.15),
    ("Muy rápida", 1.30),
]

# Perfiles de CPU: (nombre, % aproximado del procesador)
PERFILES_CPU = [
    ("Suave (el PC sigue fluido)", 25),
    ("Equilibrado", 50),
    ("Rápido", 75),
    ("Máximo (usa todo)", 100),
]

# Calidad de audio: (nombre, kbps del MP3; 0 = WAV sin comprimir)
CALIDADES = [
    ("MP3 liviano 64 kbps (ideal para voz)", 64),
    ("MP3 equilibrado 96 kbps", 96),
    ("MP3 alta calidad 128 kbps", 128),
    ("WAV sin comprimir (muy pesado)", 0),
]


# --- NÚCLEOS DISPONIBLES ---
def _detectar_nucleos():
    """Devuelve la lista de núcleos lógicos que este proceso puede usar."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return sorted(os.sched_getaffinity(0))
        except OSError:
            pass
    return list(range(os.cpu_count() or 4))


NUCLEOS_DISPONIBLES = _detectar_nucleos()
TOTAL_NUCLEOS = len(NUCLEOS_DISPONIBLES)


# --- CONFIGURACIÓN GUARDADA ---
def cargar_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def guardar_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# --- MENÚ GENÉRICO ---
def menu(titulo, opciones, defecto=1):
    print(f"\n{titulo}")
    for i, texto in enumerate(opciones, 1):
        marca = "  ← por defecto" if i == defecto else ""
        print(f"  [{i}] {texto}{marca}")
    while True:
        r = input(f"Elige (1-{len(opciones)}) [Enter = {defecto}]: ").strip()
        if not r:
            return defecto
        if r.isdigit() and 1 <= int(r) <= len(opciones):
            return int(r)
        print("⚠️ Opción no válida. Inténtalo de nuevo.")


# --- VOCES ---
def ruta_config_voz(ruta_onnx):
    """Piper necesita el .json de la voz (normalmente 'voz.onnx.json')."""
    for candidato in (ruta_onnx + ".json", os.path.splitext(ruta_onnx)[0] + ".json"):
        if os.path.exists(candidato):
            return candidato
    return None


def listar_voces_disponibles():
    """Lista las voces .onnx que tienen su archivo .json junto a ellas."""
    if not os.path.exists(CARPETA_VOCES):
        os.makedirs(CARPETA_VOCES, exist_ok=True)
        return []

    validas = []
    for f in sorted(os.listdir(CARPETA_VOCES)):
        if not f.endswith(".onnx"):
            continue
        ruta = os.path.join(CARPETA_VOCES, f)
        if ruta_config_voz(ruta):
            validas.append(f)
        else:
            print(f"⚠️ A '{f}' le falta su archivo .json ({f}.json). Se omite.")
    return validas


def numero_de_hablantes(ruta_config):
    try:
        with open(ruta_config, "r", encoding="utf-8") as f:
            return int(json.load(f).get("num_speakers", 1))
    except (OSError, ValueError, TypeError):
        return 1


def elegir_voz(defecto_nombre=None):
    voces = listar_voces_disponibles()
    if not voces:
        print(f"❌ No hay voces utilizables en la carpeta '{CARPETA_VOCES}'.")
        print("👉 Cada voz necesita su .onnx Y su .json (ej. voz.onnx y voz.onnx.json).")
        return None, None

    defecto = voces.index(defecto_nombre) + 1 if defecto_nombre in voces else 1
    opciones = [v.replace(".onnx", "") for v in voces]
    idx = menu("🎤 --- VOZ ---", opciones, defecto)
    voz = voces[idx - 1]
    ruta = os.path.join(CARPETA_VOCES, voz)

    speaker = None
    n = numero_de_hablantes(ruta_config_voz(ruta))
    if n > 1:
        print(f"\n🗣️ Esta voz tiene {n} hablantes distintos.")
        while True:
            r = input(f"Número de hablante (0-{n - 1}) [Enter = 0]: ").strip()
            if not r:
                speaker = 0
                break
            if r.isdigit() and 0 <= int(r) < n:
                speaker = int(r)
                break
            print("⚠️ Número no válido.")
    return voz, speaker


# --- VELOCIDAD ---
def elegir_velocidad():
    opciones = [f"{n} ({v:.2f}x)" for n, v in VELOCIDADES] + ["Personalizada"]
    idx = menu("🎚️ --- VELOCIDAD DE LECTURA (más alto = más rápido) ---", opciones, defecto=2)
    if idx <= len(VELOCIDADES):
        return VELOCIDADES[idx - 1][1]
    while True:
        r = input("Velocidad entre 0.50 y 2.00 (ej. 1.10): ").strip().replace(",", ".")
        try:
            v = float(r)
            if 0.5 <= v <= 2.0:
                return v
        except ValueError:
            pass
        print("⚠️ Valor no válido.")


def length_scale_desde_velocidad(velocidad):
    return round(1.0 / velocidad, 3)


# --- CPU ---
def nucleos_para_porcentaje(pct):
    return max(1, min(TOTAL_NUCLEOS, round(TOTAL_NUCLEOS * pct / 100)))


def elegir_cpu():
    print(f"\n💻 Tu equipo tiene {TOTAL_NUCLEOS} núcleos lógicos.")
    opciones = []
    for nombre, pct in PERFILES_CPU:
        n = nucleos_para_porcentaje(pct)
        opciones.append(f"{nombre}: ~{pct}% → {n} de {TOTAL_NUCLEOS} núcleos")
    opciones.append("Personalizado (tú eliges el porcentaje)")
    idx = menu("⚙️ --- USO DE PROCESADOR ---", opciones, defecto=1)
    if idx <= len(PERFILES_CPU):
        return PERFILES_CPU[idx - 1][1]
    minimo = round(100 / TOTAL_NUCLEOS)
    while True:
        r = input(f"Porcentaje de CPU ({minimo}-100): ").strip().replace("%", "")
        if r.isdigit() and 1 <= int(r) <= 100:
            return max(int(r), minimo)
        print("⚠️ Valor no válido.")


def elegir_calidad():
    hay_ffmpeg = bool(FFMPEG_EXE)
    if not hay_ffmpeg:
        print("\n⚠️ No se encontró ffmpeg: el resultado será WAV (pesado).")
        return 0
    idx = menu("📦 --- FORMATO DE AUDIO FINAL ---", [n for n, _ in CALIDADES], defecto=1)
    return CALIDADES[idx - 1][1]


def describir_ajustes(aj):
    n = nucleos_para_porcentaje(aj["cpu_pct"])
    fmt = f"MP3 {aj['kbps']} kbps" if aj["kbps"] else "WAV"
    voz = aj["voz"].replace(".onnx", "")
    if aj.get("speaker") is not None:
        voz += f" (hablante {aj['speaker']})"
    return (f"Voz: {voz} | Velocidad: {aj['velocidad']:.2f}x | "
            f"CPU: {n} de {TOTAL_NUCLEOS} núcleos (~{round(n / TOTAL_NUCLEOS * 100)}%) | "
            f"Audio: {fmt}")


def pedir_ajustes():
    """Pregunta ajustes, o reutiliza los de la vez anterior si el usuario quiere."""
    cfg = cargar_config()
    previo = cfg.get("ajustes")
    if previo and os.path.exists(os.path.join(CARPETA_VOCES, previo.get("voz", ""))):
        print("\n📌 Configuración anterior:")
        print("   " + describir_ajustes(previo))
        r = input("¿Usar la misma configuración? (S/n): ").strip().lower()
        if r != "n":
            return previo

    voz, speaker = elegir_voz(previo.get("voz") if previo else None)
    if not voz:
        return None
    ajustes = {
        "voz": voz,
        "speaker": speaker,
        "velocidad": elegir_velocidad(),
        "cpu_pct": elegir_cpu(),
        "kbps": elegir_calidad(),
    }
    cfg["ajustes"] = ajustes
    guardar_config(cfg)
    return ajustes


# --- LÍMITES DE CPU POR PROCESO ---
def aplicar_limites(pid, nucleos):
    """Fija el proceso a los núcleos indicados (el límite real de CPU)
    y le baja la prioridad. Devuelve True si se pudo fijar la afinidad."""
    ok = False
    try:
        if sys.platform == "win32":
            import ctypes
            if max(nucleos) < 64:
                k32 = ctypes.windll.kernel32
                k32.OpenProcess.restype = ctypes.c_void_p
                k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
                k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
                # PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION
                h = k32.OpenProcess(0x0200 | 0x0400, False, pid)
                if h:
                    mascara = sum(1 << c for c in nucleos)
                    ok = bool(k32.SetProcessAffinityMask(h, mascara))
                    k32.CloseHandle(h)
        else:
            if hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(pid, set(nucleos))
                ok = True
            if hasattr(os, "setpriority"):
                os.setpriority(os.PRIO_PROCESS, pid, 10)
    except Exception:
        pass
    return ok


def entorno_un_hilo():
    """Cada proceso de Piper trabaja con 1 hilo; el paralelismo viene de
    lanzar varios procesos (uno por núcleo permitido)."""
    env = os.environ.copy()
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[var] = "1"
    return env


# --- NOTIFICACIÓN AUDITIVA ---
def reproducir_sonido_exito():
    if HAS_WINSOUND and sys.platform == "win32":
        try:
            winsound.Beep(600, 150)
            winsound.Beep(800, 150)
            winsound.Beep(1000, 300)
        except Exception:
            pass


# --- DIVISIÓN DE TEXTO POR BLOQUES DE PÁRRAFOS ---
def dividir_texto_en_bloques(texto, max_palabras=1000):
    parrafos = [p.strip() for p in re.split(r'\n\s*\n', texto) if p.strip()]
    bloques = []
    bloque_actual = []
    conteo_actual = 0

    for parrafo in parrafos:
        palabras_parrafo = len(parrafo.split())
        if conteo_actual + palabras_parrafo > max_palabras and bloque_actual:
            bloques.append("\n\n".join(bloque_actual))
            bloque_actual = [parrafo]
            conteo_actual = palabras_parrafo
        else:
            bloque_actual.append(parrafo)
            conteo_actual += palabras_parrafo

    if bloque_actual:
        bloques.append("\n\n".join(bloque_actual))

    return bloques if bloques else [texto]


def formato_tiempo(segundos):
    segundos = int(max(0, segundos))
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# --- SÍNTESIS EN PARALELO ---
def sintetizar_pendientes(pendientes, ctx, total_bloques, ya_hechos):
    """Sintetiza los bloques pendientes. Un proceso de Piper por núcleo permitido.
    Devuelve 'ok', 'error' o 'interrumpido'."""
    nucleos = ctx["nucleos"]
    n_workers = min(len(nucleos), len(pendientes))
    cola_nucleos = queue.Queue()
    for c in nucleos[:n_workers]:
        cola_nucleos.put(c)

    stop = threading.Event()
    lock = threading.Lock()
    activos = set()
    env = entorno_un_hilo()
    flags = 0x00004000 if sys.platform == "win32" else 0  # BELOW_NORMAL_PRIORITY_CLASS

    def tarea(num, texto, ruta_final):
        if stop.is_set():
            return num, False, None
        nucleo = cola_nucleos.get()
        try:
            if stop.is_set():
                return num, False, None
            ruta_tmp = ruta_final[:-4] + ".part.wav"
            cmd = [PIPER_EXE, "--model", ctx["modelo"], "--config", ctx["config"],
                   "--output_file", ruta_tmp, "--length_scale", str(ctx["length_scale"])]
            if ctx["speaker"] is not None:
                cmd += ["--speaker", str(ctx["speaker"])]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", env=env, creationflags=flags)
            except OSError as e:
                return num, False, str(e)
            with lock:
                activos.add(proc)
            aplicar_limites(proc.pid, [nucleo])
            try:
                _, err = proc.communicate(input=texto)
            finally:
                with lock:
                    activos.discard(proc)

            if proc.returncode != 0 or not os.path.exists(ruta_tmp):
                if os.path.exists(ruta_tmp):
                    os.remove(ruta_tmp)
                if stop.is_set() and proc.returncode != 0:
                    return num, False, None  # cancelado a propósito
                return num, False, (err or "sin detalle").strip()[-500:]
            os.replace(ruta_tmp, ruta_final)  # solo cuenta como listo si terminó bien
            return num, True, None
        finally:
            cola_nucleos.put(nucleo)

    palabras = {num: len(texto.split()) for num, texto, _ in pendientes}
    palabras_pendientes = sum(palabras.values())
    palabras_hechas = 0
    hechos = 0
    fallo = None
    interrumpido = False
    t0 = time.time()

    ex = ThreadPoolExecutor(max_workers=n_workers)
    futuros = [ex.submit(tarea, *p) for p in pendientes]
    try:
        for fut in as_completed(futuros):
            num, ok, err = fut.result()
            if ok:
                hechos += 1
                palabras_hechas += palabras[num]
                pct = round((ya_hechos + hechos) / total_bloques * 100)
                transcurrido = time.time() - t0
                eta = ""
                if palabras_hechas and transcurrido > 0:
                    resto = (palabras_pendientes - palabras_hechas) / (palabras_hechas / transcurrido)
                    eta = f" | faltan ~{formato_tiempo(resto)}"
                print(f"⏳ [{pct}%] Bloque {num}/{total_bloques} listo{eta}")
            elif err:
                if fallo is None:
                    fallo = (num, err)
                stop.set()  # no lanzar bloques nuevos; los que ya corren terminan y se guardan
    except KeyboardInterrupt:
        interrumpido = True
        stop.set()
        with lock:
            for p in list(activos):
                try:
                    p.terminate()
                except Exception:
                    pass
        for f in futuros:
            f.cancel()
    finally:
        ex.shutdown(wait=True)

    if interrumpido:
        return "interrumpido"
    if fallo:
        print(f"\n❌ Error en el bloque {fallo[0]}: {fallo[1]}")
        return "error"
    return "ok"


# --- UNIÓN Y EXPORTACIÓN DE AUDIO ---
def concatenar_wavs(lista_wavs, wav_salida):
    """Une WAVs leyendo y escribiendo en trozos pequeños (RAM casi constante)."""
    ref = None
    with wave.open(wav_salida, "wb") as salida:
        for i, w in enumerate(lista_wavs):
            with wave.open(w, "rb") as entrada:
                formato = (entrada.getnchannels(), entrada.getsampwidth(), entrada.getframerate())
                if i == 0:
                    ref = formato
                    salida.setnchannels(formato[0])
                    salida.setsampwidth(formato[1])
                    salida.setframerate(formato[2])
                elif formato != ref:
                    raise ValueError(f"Formato de audio distinto en {w}")
                while True:
                    frames = entrada.readframes(65536)
                    if not frames:
                        break
                    salida.writeframes(frames)


def _escapar_ruta_concat(ruta):
    return os.path.abspath(ruta).replace("\\", "/").replace("'", "'\\''")


def exportar_audio(wavs, nombre_base, kbps, dir_temp):
    """MP3 directo con ffmpeg (sin WAV gigante ni RAM). Si no se puede, WAV."""
    if kbps and FFMPEG_EXE:
        mp3 = f"{nombre_base}.mp3"
        lista = os.path.join(dir_temp, "lista.txt")
        with open(lista, "w", encoding="utf-8") as f:
            for w in wavs:
                f.write(f"file '{_escapar_ruta_concat(w)}'\n")
        print(f"\n📦 Uniendo y comprimiendo a MP3 ({kbps} kbps)...")
        cmd = [FFMPEG_EXE, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", lista, "-vn", "-ac", "1", "-codec:a", "libmp3lame",
               "-b:a", f"{kbps}k", "-metadata", f"title={nombre_base}", mp3]
        flags = 0x00004000 if sys.platform == "win32" else 0
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
        if res.returncode == 0 and os.path.exists(mp3):
            return mp3
        print(f"⚠️ ffmpeg falló: {(res.stderr or '').strip()[-300:]}")
        print("   Se generará WAV en su lugar.")

    wav = f"{nombre_base}.wav"
    print("\n🧩 Uniendo bloques en un WAV...")
    concatenar_wavs(wavs, wav)
    return wav


# --- ENTRADA DE ARCHIVO ---
def solicitar_archivo_texto():
    while True:
        entrada = input("\n📄 Nombre o ruta del archivo .txt (o 'salir' para finalizar): ").strip().strip('"\'')
        if entrada.lower() in ["salir", "exit", "q"]:
            return None
        if not entrada.lower().endswith(".txt"):
            entrada += ".txt"
        if os.path.exists(entrada):
            return entrada
        print(f"❌ No se encontró el archivo '{entrada}'. Verifica la ruta.")


def leer_json(ruta):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --- PROCESO PRINCIPAL ---
def procesar_texto_masivo():
    print("==================================================")
    print("       🔊 GAGODBOLKA TTS v3.0 - ARCHIVOS GRANDES    ")
    print("==================================================")

    os.chdir(BASE_DIR)  # rutas relativas (voces, textos, resultados) = carpeta del programa

    if not PIPER_EXE:
        print("\n❌ No se encontró piper. Debe estar en la carpeta del programa (piper/piper.exe).")
        return

    while True:
        archivo_texto = solicitar_archivo_texto()
        if not archivo_texto:
            print("\n👋 ¡Hasta luego!")
            break

        ajustes = pedir_ajustes()
        if not ajustes:
            continue

        ruta_modelo = os.path.join(CARPETA_VOCES, ajustes["voz"])
        ls = length_scale_desde_velocidad(ajustes["velocidad"])
        n_nucleos = nucleos_para_porcentaje(ajustes["cpu_pct"])
        # Se usan los últimos núcleos: el 0 suele estar más ocupado por el sistema
        nucleos = NUCLEOS_DISPONIBLES[-n_nucleos:]

        print("\n" + describir_ajustes(ajustes))

        nombre_base = os.path.splitext(os.path.basename(archivo_texto))[0]
        dir_temp = os.path.join(CARPETA_TEMP, nombre_base)

        print(f"\n📖 Leyendo '{archivo_texto}'...")
        with open(archivo_texto, "r", encoding="utf-8-sig") as f:
            texto_completo = f.read()

        bloques = dividir_texto_en_bloques(texto_completo)
        total = len(bloques)
        print(f"📊 {len(texto_completo.split()):,} palabras en {total} bloque(s).")

        # --- CHECKPOINT: solo se reanuda si voz/velocidad/texto son los mismos ---
        firma = {
            "modelo": ajustes["voz"],
            "speaker": ajustes.get("speaker"),
            "length_scale": ls,
            "bloques": total,
            "hash": hashlib.md5(texto_completo.encode("utf-8")).hexdigest(),
        }
        meta = os.path.join(dir_temp, "meta.json")
        if os.path.isdir(dir_temp) and leer_json(meta) != firma:
            print("🧹 Había progreso previo con otra voz, velocidad o texto. Se empieza de cero.")
            shutil.rmtree(dir_temp, ignore_errors=True)
        os.makedirs(dir_temp, exist_ok=True)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump(firma, f)

        wavs = []
        pendientes = []
        for i, bloque in enumerate(bloques, 1):
            ruta = os.path.join(dir_temp, f"bloque_{i:03d}.wav")
            wavs.append(ruta)
            if not os.path.exists(ruta):
                pendientes.append((i, bloque, ruta))

        ya_hechos = total - len(pendientes)
        if 0 < ya_hechos and pendientes:
            print(f"🔄 Se encontró un trabajo previo ({ya_hechos}/{total} bloques listos).")
            if input("¿Reanudar desde donde quedó? (S/n): ").strip().lower() == "n":
                shutil.rmtree(dir_temp, ignore_errors=True)
                os.makedirs(dir_temp, exist_ok=True)
                with open(meta, "w", encoding="utf-8") as f:
                    json.dump(firma, f)
                pendientes = [(i, b, w) for i, (b, w) in enumerate(zip(bloques, wavs), 1)]
                ya_hechos = 0

        inicio = time.time()
        estado = "ok"
        if pendientes:
            ctx = {
                "modelo": ruta_modelo,
                "config": ruta_config_voz(ruta_modelo),
                "length_scale": ls,
                "speaker": ajustes.get("speaker"),
                "nucleos": nucleos,
            }
            print(f"🚀 Procesando con {min(len(nucleos), len(pendientes))} proceso(s) en paralelo. "
                  f"(Ctrl+C para pausar: el progreso se guarda)\n")
            estado = sintetizar_pendientes(pendientes, ctx, total, ya_hechos)
        else:
            print("✅ Todos los bloques ya estaban sintetizados; solo falta unirlos.")

        if estado == "ok":
            try:
                final = exportar_audio(wavs, nombre_base, ajustes["kbps"], dir_temp)
            except Exception as e:
                print(f"\n❌ No se pudo unir el audio: {e}")
                print(f"   Los bloques siguen guardados en '{dir_temp}'. Reintenta con el mismo archivo.")
                print("\n" + "=" * 50)
                continue

            shutil.rmtree(dir_temp, ignore_errors=True)
            try:
                os.rmdir(CARPETA_TEMP)  # solo se borra si quedó vacía
            except OSError:
                pass

            tamano = os.path.getsize(final) / (1024 * 1024)
            print(f"\n🎉 ¡Listo en {formato_tiempo(time.time() - inicio)}!")
            print(f"🔊 Archivo: {final} ({tamano:.1f} MB)")
            reproducir_sonido_exito()
        elif estado == "interrumpido":
            print(f"\n⏸️ Pausado. Progreso guardado en '{dir_temp}'.")
            print("Ingresa el mismo archivo y la misma configuración para continuar.")
        else:
            print(f"\n⚠️ El proceso no terminó. Progreso guardado en '{dir_temp}'.")
            print("Vuelve a ingresar el mismo archivo para continuar.")

        print("\n" + "=" * 50)


if __name__ == "__main__":
    try:
        procesar_texto_masivo()
    except KeyboardInterrupt:
        print("\n\n👋 Cerrado por el usuario.")