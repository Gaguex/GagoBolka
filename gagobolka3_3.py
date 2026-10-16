import os
import re
import math
import asyncio
import importlib.util
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


# Nombres de idioma para agrupar las voces (el código sale del nombre del archivo: es_MX-... -> es)
NOMBRES_IDIOMA = {
    "en": "inglés", "es": "español", "fr": "francés", "de": "alemán",
    "pt": "portugués", "it": "italiano", "ca": "catalán", "ru": "ruso",
    "zh": "chino", "ja": "japonés", "nl": "neerlandés", "pl": "polaco",
    "otros": "otros idiomas",
}
IDIOMAS_PRIMERO = ["en", "es"]  # orden en que aparecen los grupos en el menú

# --- MOTORES ADICIONALES (se detectan sin importarlos: el arranque sigue siendo rápido) ---
def _disponible(modulo):
    try:
        return importlib.util.find_spec(modulo) is not None
    except (ImportError, ValueError):
        return False


HAS_EDGE = _disponible("edge_tts")
HAS_KOKORO = _disponible("kokoro_onnx") and _disponible("soundfile")

CARPETA_KOKORO = "voces_kokoro"   # kokoro-v1.0.onnx (o la versión cuantizada) + voices-v1.0.bin
MAX_WORKERS_KOKORO = 2            # cada proceso carga el modelo en RAM (~0.5-1 GB el normal)
EDGE_CONCURRENCIA = 3             # pocas conexiones: es un servicio no oficial
EDGE_REINTENTOS = 3

NOMBRES_MOTOR = {"piper": "Piper (sin internet)", "edge": "Microsoft Edge (en línea)",
                 "kokoro": "Kokoro (sin internet, alta calidad)"}

VOCES_EDGE_DESTACADAS = [
    ("en-US-BrianNeural", "Brian — masculina, madura (documental)"),
    ("en-US-AndrewNeural", "Andrew — masculina, cálida (narrativa)"),
    ("en-US-ChristopherNeural", "Christopher — masculina, autoritaria"),
    ("en-US-EricNeural", "Eric — masculina, joven"),
    ("en-US-AriaNeural", "Aria — femenina, clara"),
    ("es-CL-LorenzoNeural", "Chile · Lorenzo — masculina, neutra"),
    ("es-CL-CatalinaNeural", "Chile · Catalina — femenina"),
    ("es-MX-DaliaNeural", "México · Dalia — femenina, expresiva"),
    ("es-MX-JorgeNeural", "México · Jorge — masculina"),
    ("es-ES-AlvaroNeural", "España · Álvaro — masculina, profunda"),
    ("es-ES-ElviraNeural", "España · Elvira — femenina, narrativa"),
]

# Kokoro: la 1.ª letra de la voz indica el idioma -> (código de idioma para el motor, grupo, nombre)
# (japonés y chino se omiten: necesitan paquetes extra)
IDIOMAS_KOKORO = {
    "a": ("en-us", "en", "inglés"), "b": ("en-gb", "en", "inglés"),
    "e": ("es", "es", "español"), "f": ("fr-fr", "fr", "francés"),
    "h": ("hi", "hi", "hindi"), "i": ("it", "it", "italiano"),
    "p": ("pt-br", "pt", "portugués"),
}
VOCES_KOKORO_BASE = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam",
                     "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
                     "ef_dora", "em_alex", "em_santa"]


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


def elegir_hablante(ruta_voz):
    """Pregunta el hablante si la voz tiene varios. Devuelve None si tiene uno solo."""
    n = numero_de_hablantes(ruta_config_voz(ruta_voz))
    if n <= 1:
        return None
    print(f"\n🗣️ Esta voz tiene {n} hablantes distintos (varias voces dentro del mismo modelo).")
    print("   Si no sabes cuál elegir, solo presiona Enter para usar el predeterminado (0).")
    while True:
        r = input(f"Número de hablante (0-{n - 1}) [Enter = predeterminado]: ").strip()
        if not r:
            return 0
        if r.isdigit() and 0 <= int(r) < n:
            return int(r)
        print("⚠️ Número no válido.")


def idioma_de_voz(nombre_archivo, ruta_json):
    """Código de idioma de la voz: primero por el nombre (es_MX-...), si no por su .json."""
    m = re.match(r"^([a-z]{2,3})[_-]", nombre_archivo)
    if m:
        return m.group(1)
    datos = leer_json(ruta_json) if ruta_json else None
    try:
        return datos["language"]["family"]
    except (TypeError, KeyError):
        return "otros"


def elegir_voz(defecto_nombre=None):
    voces = listar_voces_disponibles()
    if not voces:
        print(f"❌ No hay voces utilizables en la carpeta '{CARPETA_VOCES}'.")
        print("👉 Cada voz necesita su .onnx Y su .json (ej. voz.onnx y voz.onnx.json).")
        return None, None

    # Agrupar por idioma
    grupos = {}
    for v in voces:
        cod = idioma_de_voz(v, ruta_config_voz(os.path.join(CARPETA_VOCES, v)))
        grupos.setdefault(cod, []).append(v)
    resto = sorted((c for c in grupos if c not in IDIOMAS_PRIMERO),
                   key=lambda c: NOMBRES_IDIOMA.get(c, c))
    orden = [c for c in IDIOMAS_PRIMERO if c in grupos] + resto

    # Mostrar con numeración continua a través de los grupos
    lista = []
    defecto = defecto_nombre if defecto_nombre in voces else None
    print("\n🎤 --- VOZ ---")
    for cod in orden:
        print(f"\n  -------- Voces en {NOMBRES_IDIOMA.get(cod, cod)} --------")
        for v in grupos[cod]:
            lista.append(v)
            marca = "  ← por defecto" if v == (defecto or lista[0]) else ""
            print(f"  [{len(lista)}] {v[:-5]}{marca}")

    por_defecto = lista.index(defecto) + 1 if defecto else 1
    while True:
        r = input(f"\nElige (1-{len(lista)}) [Enter = {por_defecto}]: ").strip()
        if not r:
            idx = por_defecto
            break
        if r.isdigit() and 1 <= int(r) <= len(lista):
            idx = int(r)
            break
        print("⚠️ Opción no válida. Inténtalo de nuevo.")

    voz = lista[idx - 1]
    ruta = os.path.join(CARPETA_VOCES, voz)

    speaker = None
    n = numero_de_hablantes(ruta_config_voz(ruta))
    if n > 1:
        print(f"\n🗣️ Esta voz tiene {n} hablantes distintos (varias voces dentro del mismo modelo).")
        print("   Si no sabes cuál elegir, solo presiona Enter para usar el predeterminado (0).")
        while True:
            r = input(f"Número de hablante (0-{n - 1}) [Enter = predeterminado]: ").strip()
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
    return max(1, min(TOTAL_NUCLEOS, math.ceil(TOTAL_NUCLEOS * pct / 100)))


def elegir_cpu():
    print(f"\n💻 Tu equipo tiene {TOTAL_NUCLEOS} núcleos lógicos.")
    opciones = []
    for nombre, pct in PERFILES_CPU:
        opciones.append(f"{nombre}: tope {pct}% del procesador")
    opciones.append("Personalizado (tú eliges el porcentaje)")
    idx = menu("⚙️ --- USO DE PROCESADOR ---", opciones, defecto=1)
    if idx <= len(PERFILES_CPU):
        return PERFILES_CPU[idx - 1][1]
    while True:
        r = input("Porcentaje máximo de CPU (10-100): ").strip().replace("%", "")
        if r.isdigit() and 10 <= int(r) <= 100:
            return int(r)
        print("⚠️ Valor no válido.")


# --- CARPETA DE SALIDA ---
def examinar_carpeta(inicial):
    """Abre el explorador de Windows para elegir carpeta (tkinter viene con Python/PyInstaller).
    Si no se puede abrir la ventana, pide la ruta escrita."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        raiz = tk.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        carpeta = filedialog.askdirectory(
            title="Elige la carpeta donde se guardarán los audios",
            initialdir=inicial if os.path.isdir(inicial) else BASE_DIR,
            parent=raiz)
        raiz.destroy()
        return os.path.normpath(carpeta) if carpeta else None
    except Exception:
        r = input("Escribe la ruta completa de la carpeta de salida: ").strip().strip('"\'')
        return os.path.normpath(r) if r else None


def carpeta_valida(ruta):
    """Crea la carpeta si no existe y comprueba que se pueda escribir en ella."""
    try:
        os.makedirs(ruta, exist_ok=True)
        return os.access(ruta, os.W_OK)
    except OSError:
        return False


def elegir_carpeta_salida(actual):
    idx = menu("📁 --- CARPETA DE SALIDA ---",
               [f"Usar: {actual}", "Examinar... (elegir otra carpeta)"], defecto=1)
    if idx == 1:
        return actual
    nueva = examinar_carpeta(actual)
    if not nueva:
        print("↩️ No se eligió ninguna carpeta; se mantiene la anterior.")
        return actual
    if not carpeta_valida(nueva):
        print("⚠️ No se puede escribir en esa carpeta; se mantiene la anterior.")
        return actual
    print(f"✅ Los audios se guardarán en: {nueva}")
    return nueva


def elegir_calidad():
    hay_ffmpeg = bool(FFMPEG_EXE)
    if not hay_ffmpeg:
        print("\n⚠️ No se encontró ffmpeg: el resultado será WAV (pesado).")
        return 0
    idx = menu("📦 --- FORMATO DE AUDIO FINAL ---", [n for n, _ in CALIDADES], defecto=1)
    return CALIDADES[idx - 1][1]


def describir_ajustes(aj):
    motor = aj.get("motor", "piper")
    fmt = f"MP3 {aj['kbps']} kbps" if aj["kbps"] else "WAV"
    voz = aj["voz"].replace(".onnx", "")
    if aj.get("speaker") is not None:
        voz += f" (hablante {aj['speaker']})"
    if motor == "edge":
        cpu = "no aplica (en línea)"
    else:
        cpu = f"tope {aj['cpu_pct']}% ({nucleos_para_porcentaje(aj['cpu_pct'])} de {TOTAL_NUCLEOS} núcleos)"
    return (f"Motor: {NOMBRES_MOTOR[motor]} | Voz: {voz} | Velocidad: {aj['velocidad']:.2f}x\n"
            f"   CPU: {cpu} | Audio: {fmt}\n   Guardar en: {aj.get('salida', BASE_DIR)}")


def pedir_ajustes():
    """Pregunta ajustes, o reutiliza los de la vez anterior si el usuario quiere."""
    cfg = cargar_config()
    previo = cfg.get("ajustes")
    if previo:
        previo.setdefault("motor", "piper")     # configuraciones de versiones anteriores
        previo.setdefault("salida", BASE_DIR)
        voz_ok = previo["motor"] != "piper" or os.path.exists(os.path.join(CARPETA_VOCES, previo.get("voz", "")))
        problema = motivo_no_disponible(previo["motor"])
        if voz_ok and not problema:
            print("\n📌 Configuración anterior:")
            print("   " + describir_ajustes(previo))
            op = menu("¿Qué quieres hacer?", [
                "Usar la misma configuración",
                "Cambiar motor, voz, velocidad, CPU y formato",
                "Cambiar solo la carpeta de salida",
            ], defecto=1)
            if op == 1:
                if previo["motor"] == "piper":
                    ruta = os.path.join(CARPETA_VOCES, previo["voz"])
                    if numero_de_hablantes(ruta_config_voz(ruta)) > 1:
                        previo["speaker"] = elegir_hablante(ruta)
                        cfg["ajustes"] = previo
                        guardar_config(cfg)
                return previo
            if op == 3:
                previo["salida"] = elegir_carpeta_salida(previo["salida"])
                cfg["ajustes"] = previo
                guardar_config(cfg)
                return previo
        elif problema:
            print(f"\nℹ️ La configuración anterior usaba {NOMBRES_MOTOR[previo['motor']]}, "
                  f"que ahora no está disponible.")

    motor = elegir_motor(previo["motor"] if previo else "piper")
    voz_previa = previo.get("voz") if previo and previo.get("motor") == motor else None
    if motor == "piper":
        voz, speaker = elegir_voz(voz_previa)
    elif motor == "edge":
        voz, speaker = elegir_voz_edge(voz_previa)
    else:
        voz, speaker = elegir_voz_kokoro(voz_previa)
    if not voz:
        return None
    ajustes = {
        "motor": motor,
        "voz": voz,
        "speaker": speaker,
        "velocidad": elegir_velocidad(),
        "cpu_pct": elegir_cpu() if motor != "edge" else 100,
        "kbps": elegir_calidad(),
        "salida": elegir_carpeta_salida(previo.get("salida", BASE_DIR) if previo else BASE_DIR),
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


class LimiteCPU:
    """Tope REAL de CPU en Windows: un Job Object con 'CPU hard cap'.
    El % es del procesador TOTAL y se reparte entre todos los procesos asignados
    (da igual cuántos hilos use cada uno). Requiere Windows 8 o superior."""

    def __init__(self, porcentaje):
        self.job = None
        self.activo = False
        self.motivo = ""
        if porcentaje >= 100:
            self.motivo = "sin tope (100%)"
            return
        if sys.platform != "win32":
            self.motivo = "el tope por porcentaje solo existe en Windows"
            return
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            self._k32 = k32
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_void_p, ctypes.c_uint32]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]

            class CPU_RATE_CONTROL(ctypes.Structure):
                _fields_ = [("ControlFlags", ctypes.c_uint32),
                            ("CpuRate", ctypes.c_uint32)]   # % x 100 (2500 = 25%)

            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError("no se pudo crear el Job Object")
            # ENABLE (0x1) | HARD_CAP (0x4); clase 15 = JobObjectCpuRateControlInformation
            info = CPU_RATE_CONTROL(0x1 | 0x4, int(porcentaje) * 100)
            if not k32.SetInformationJobObject(job, 15, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                raise OSError("este Windows no admite tope de CPU (¿anterior a Windows 8?)")
            self.job = job
            self.activo = True
        except Exception as e:
            self.motivo = str(e)

    def asignar(self, pid):
        """Mete un proceso dentro del límite. Devuelve True si quedó limitado."""
        if not self.activo:
            return False
        try:
            # PROCESS_SET_QUOTA (0x0100) | PROCESS_TERMINATE (0x0001)
            h = self._k32.OpenProcess(0x0100 | 0x0001, False, pid)
            if not h:
                return False
            ok = bool(self._k32.AssignProcessToJobObject(self.job, h))
            self._k32.CloseHandle(h)
            return ok
        except Exception:
            return False

    def cerrar(self):
        if self.job:
            try:
                self._k32.CloseHandle(self.job)
            except Exception:
                pass
            self.job = None


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
def sintetizar_pendientes_piper(pendientes, ctx, total_bloques, ya_hechos):
    """Piper: sintetiza los bloques pendientes. Un proceso por núcleo permitido.
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
    limite = LimiteCPU(ctx["cpu_pct"])
    sin_limite = []  # procesos que Windows no dejó limitar
    if ctx["cpu_pct"] < 100:
        if limite.activo:
            print(f"🔒 Tope de CPU activo: máximo {ctx['cpu_pct']}% del procesador.")
        else:
            print(f"⚠️ No se pudo aplicar el tope de CPU ({limite.motivo}).")
            print("   Solo se está limitando por núcleos, que es menos preciso.")
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
            if limite.activo:
                # Con el tope de CPU activo, Windows decide en qué núcleos correr
                # (así no se fuerza el uso de núcleos lentos en CPUs híbridas).
                if not limite.asignar(proc.pid):
                    sin_limite.append(proc.pid)
            else:
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
        limite.cerrar()

    if sin_limite:
        print(f"⚠️ El tope de CPU no se pudo aplicar a {len(sin_limite)} proceso(s).")

    if interrumpido:
        return "interrumpido"
    if fallo:
        print(f"\n❌ Error en el bloque {fallo[0]}: {fallo[1]}")
        return "error"
    return "ok"


# =====================================================================
#  UTILIDADES NUEVAS
# =====================================================================
def ruta_libre(ruta):
    """libro.mp3 -> libro (2).mp3 si ya existe (nunca sobrescribe sin avisar)."""
    if not os.path.exists(ruta):
        return ruta
    base, ext = os.path.splitext(ruta)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def leer_texto(ruta):
    """UTF-8 (con o sin BOM); si no es válido, cp1252 (el 'ANSI' del Bloc de notas)."""
    with open(ruta, "rb") as f:
        crudo = f.read()
    try:
        return crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return crudo.decode("cp1252", errors="replace")


def _borrar(*rutas):
    for r in rutas:
        try:
            if os.path.exists(r):
                os.remove(r)
        except OSError:
            pass


class Progreso:
    """Progreso con tiempo estimado, compartido por todos los motores."""

    def __init__(self, total, ya_hechos, pendientes):
        self.total, self.hechos = total, ya_hechos
        self.palabras_pend = sum(len(b.split()) for _, b, _ in pendientes)
        self.palabras = 0
        self.t0 = time.time()
        self._lock = threading.Lock()

    def listo(self, num, texto):
        with self._lock:
            self.hechos += 1
            self.palabras += len(texto.split())
            pct = round(self.hechos / self.total * 100)
            t = time.time() - self.t0
            eta = ""
            if self.palabras and t > 0:
                resto = (self.palabras_pend - self.palabras) / (self.palabras / t)
                eta = f" | faltan ~{formato_tiempo(resto)}"
            print(f"⏳ [{pct}%] Bloque {num}/{self.total} listo{eta}")


def anunciar_limite(limite, pct):
    if pct < 100:
        if limite.activo:
            print(f"🔒 Tope de CPU activo: máximo {pct}% del procesador.")
        else:
            print(f"⚠️ No se pudo aplicar el tope de CPU ({limite.motivo}).")


# =====================================================================
#  MENÚ AGRUPADO (voces separadas por idioma)
# =====================================================================
def menu_agrupado(titulo, grupos, defecto_id=None, extras=()):
    """grupos: [(nombre_del_grupo, [(id, etiqueta), ...]), ...]
    Devuelve ('voz', id) o ('extra', posición_en_extras)."""
    todos = [vid for _, items in grupos for vid, _ in items]
    defecto = todos.index(defecto_id) + 1 if defecto_id in todos else 1
    print(f"\n{titulo}")
    n = 0
    for nombre, items in grupos:
        print(f"\n  -------- {nombre} --------")
        for vid, etiqueta in items:
            n += 1
            print(f"  [{n}] {etiqueta}{'  ← por defecto' if n == defecto else ''}")
    if extras:
        print("\n  -------- Otras opciones --------")
        for texto in extras:
            n += 1
            print(f"  [{n}] {texto}")
    while True:
        r = input(f"\nElige (1-{n}) [Enter = {defecto}]: ").strip()
        if not r:
            r = str(defecto)
        if r.isdigit() and 1 <= int(r) <= n:
            k = int(r)
            return ("voz", todos[k - 1]) if k <= len(todos) else ("extra", k - len(todos) - 1)
        print("⚠️ Opción no válida. Inténtalo de nuevo.")


# =====================================================================
#  EDGE: voces
# =====================================================================
def elegir_voz_edge(defecto=None):
    en = [(c, d) for c, d in VOCES_EDGE_DESTACADAS if c.startswith("en-")]
    es = [(c, d) for c, d in VOCES_EDGE_DESTACADAS if c.startswith("es-")]
    grupos = [("Voces en inglés", en), ("Voces en español", es)]
    extras = ["Ver la lista completa de Edge (requiere internet)",
              "Escribir el código de una voz (ej. es-AR-TomasNeural)"]
    while True:
        tipo, valor = menu_agrupado("🌐 --- VOCES MICROSOFT EDGE (EN LÍNEA) ---", grupos, defecto, extras)
        if tipo == "voz":
            return valor, None
        if valor == 0:
            voz = _elegir_voz_edge_completa()
            if voz:
                return voz, None
        else:
            v = input("Código de la voz: ").strip()
            if re.match(r"^[a-z]{2,3}-[A-Za-z0-9]{2,8}-\w+$", v):
                return v, None
            print("⚠️ Ese código no parece válido (formato: idioma-PAÍS-NombreNeural).")


def _elegir_voz_edge_completa():
    import edge_tts
    print("\n🔎 Consultando la lista de voces de Edge...")
    try:
        voces = asyncio.run(edge_tts.list_voices())
    except Exception as e:
        print(f"❌ No se pudo obtener la lista ({type(e).__name__}). ¿Hay internet?")
        return None
    filtro = input("Idioma a buscar (ej. es, en, fr, pt) [Enter = es]: ").strip().lower() or "es"
    sel = sorted((v for v in voces if v["Locale"].lower().startswith(filtro)),
                 key=lambda v: (v["Locale"], v["ShortName"]))
    if not sel:
        print(f"⚠️ No hay voces para '{filtro}'.")
        return None
    opciones = [f"{v['ShortName']} ({'femenina' if v['Gender'] == 'Female' else 'masculina'}, {v['Locale']})"
                for v in sel]
    idx = menu(f"Voces de Edge para '{filtro}':", opciones, defecto=1)
    return sel[idx - 1]["ShortName"]


# =====================================================================
#  KOKORO: archivos, voces y síntesis
# =====================================================================
def buscar_modelo_kokoro():
    """Devuelve (modelo.onnx, voces.bin) de la versión 1.0, o None si faltan."""
    carpeta = os.path.join(BASE_DIR, CARPETA_KOKORO)
    if not os.path.isdir(carpeta):
        return None
    archivos = sorted(os.listdir(carpeta))
    modelos = [f for f in archivos if f.lower().startswith("kokoro") and f.lower().endswith(".onnx")]
    voces = [f for f in archivos if f.lower().startswith("voices") and f.lower().endswith(".bin")]
    if not modelos or not voces:
        return None
    modelo = "kokoro-v1.0.onnx" if "kokoro-v1.0.onnx" in modelos else modelos[0]
    return os.path.join(carpeta, modelo), os.path.join(carpeta, voces[0])


def listar_voces_kokoro(ruta_voces):
    try:
        import numpy as np
        nombres = sorted(np.load(ruta_voces, allow_pickle=False).files)
    except Exception:
        nombres = list(VOCES_KOKORO_BASE)
    return [n for n in nombres if re.match(r"^[a-z][fm]_\w+$", n) and n[0] in IDIOMAS_KOKORO]


def lang_de_voz_kokoro(voz):
    return IDIOMAS_KOKORO.get(voz[:1], IDIOMAS_KOKORO["a"])[0]


def elegir_voz_kokoro(defecto=None):
    modelo = buscar_modelo_kokoro()
    nombres = listar_voces_kokoro(modelo[1]) if modelo else list(VOCES_KOKORO_BASE)
    grupos = {}
    for n in nombres:
        _, corto, nombre_idioma = IDIOMAS_KOKORO[n[0]]
        genero = "femenina" if n[1] == "f" else "masculina"
        extra = ", británico" if n[0] == "b" else ""
        etiqueta = f"{n} ({n.split('_', 1)[1].capitalize()}, {genero}{extra})"
        grupos.setdefault((corto, nombre_idioma), []).append((n, etiqueta))
    orden = sorted(grupos, key=lambda k: (IDIOMAS_PRIMERO.index(k[0]) if k[0] in IDIOMAS_PRIMERO else 99, k[1]))
    lista = [(f"Voces en {k[1]}", grupos[k]) for k in orden]
    _, voz = menu_agrupado("🍓 --- VOCES KOKORO (SIN INTERNET) ---", lista, defecto)
    return voz, None


def _leer_json_linea(flujo):
    """Lee líneas hasta encontrar un objeto JSON; None si el proceso terminó."""
    while True:
        linea = flujo.readline()
        if not linea:
            return None
        try:
            obj = json.loads(linea)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj


def _worker_kokoro(modelo, voces):
    """Proceso hijo: carga el modelo UNA vez y sintetiza los bloques que le lleguen por stdin."""
    canal = sys.stdout
    sys.stdout = sys.stderr   # nada de las librerías debe ensuciar el canal de mensajes

    def enviar(obj):
        canal.write(json.dumps(obj) + "\n")
        canal.flush()

    try:
        from kokoro_onnx import Kokoro
        import soundfile as sf
        kokoro = Kokoro(modelo, voces)
    except Exception as e:
        enviar({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return
    enviar({"ready": True})
    for linea in sys.stdin:
        try:
            job = json.loads(linea)
        except ValueError:
            continue
        try:
            muestras, tasa = kokoro.create(job["texto"], voice=job["voz"],
                                           speed=job["velocidad"], lang=job["lang"])
            tmp = job["salida"][:-4] + ".part.wav"
            sf.write(tmp, muestras, tasa)
            os.replace(tmp, job["salida"])   # solo cuenta como listo si se escribió completo
            enviar({"ok": True, "num": job["num"]})
        except Exception as e:
            enviar({"ok": False, "num": job["num"], "error": f"{type(e).__name__}: {e}"})


def _comando_worker_kokoro(modelo, voces):
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
    return base + ["--worker-kokoro", os.path.abspath(modelo), os.path.abspath(voces)]


def sintetizar_pendientes_kokoro(pendientes, ctx, total_bloques, ya_hechos):
    archivos = buscar_modelo_kokoro()
    if not archivos:
        print(f"\n❌ Faltan los archivos de Kokoro en '{CARPETA_KOKORO}'.")
        return "error"
    modelo, voces = archivos
    n_workers = max(1, min(nucleos_para_porcentaje(ctx["cpu_pct"]), MAX_WORKERS_KOKORO, len(pendientes)))
    cola = queue.Queue()
    for p in pendientes:
        cola.put(p)
    prog = Progreso(total_bloques, ya_hechos, pendientes)
    stop = threading.Event()
    lock = threading.Lock()
    activos = set()
    fallos = []
    flags = 0x00004000 if sys.platform == "win32" else 0
    limite = LimiteCPU(ctx["cpu_pct"])
    anunciar_limite(limite, ctx["cpu_pct"])
    lang = lang_de_voz_kokoro(ctx["voz"])
    print(f"🧠 Cargando Kokoro en {n_workers} proceso(s)... (puede tardar unos segundos)")

    def hilo(i):
        log_path = os.path.join(ctx["dir_temp"], f"kokoro_{i}.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            try:
                proc = subprocess.Popen(_comando_worker_kokoro(modelo, voces), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=log, text=True,
                                        encoding="utf-8", creationflags=flags)
            except OSError as e:
                fallos.append(("-", str(e)))
                stop.set()
                return
            with lock:
                activos.add(proc)
            limite.asignar(proc.pid)
            try:
                listo = _leer_json_linea(proc.stdout)
                if not listo or not listo.get("ready"):
                    if not stop.is_set():
                        motivo = (listo or {}).get("error") or _cola_log(log_path)
                        fallos.append(("-", f"Kokoro no pudo iniciar: {motivo}"))
                        stop.set()
                    return
                while not stop.is_set():
                    try:
                        num, texto, final = cola.get_nowait()
                    except queue.Empty:
                        break
                    proc.stdin.write(json.dumps({"num": num, "texto": texto, "voz": ctx["voz"],
                                                 "velocidad": ctx["velocidad"], "lang": lang,
                                                 "salida": final}) + "\n")
                    proc.stdin.flush()
                    resp = _leer_json_linea(proc.stdout)
                    if resp is None:
                        if not stop.is_set():
                            fallos.append((num, "el proceso de Kokoro se cerró: " + _cola_log(log_path)))
                            stop.set()
                        break
                    if resp.get("ok"):
                        prog.listo(num, texto)
                    else:
                        fallos.append((num, resp.get("error", "error desconocido")))
                        stop.set()
                        break
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                with lock:
                    activos.discard(proc)

    ex = ThreadPoolExecutor(max_workers=n_workers)
    futuros = [ex.submit(hilo, i) for i in range(n_workers)]
    interrumpido = False
    try:
        for f in futuros:
            f.result()
    except KeyboardInterrupt:
        interrumpido = True
        stop.set()
        with lock:
            for p in list(activos):
                try:
                    p.terminate()
                except Exception:
                    pass
    finally:
        ex.shutdown(wait=True)
        limite.cerrar()

    if interrumpido:
        return "interrumpido"
    if fallos:
        print(f"\n❌ Error en Kokoro (bloque {fallos[0][0]}): {fallos[0][1]}")
        return "error"
    return "ok"


def _cola_log(ruta, n=300):
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()[-n:] or "sin detalle"
    except OSError:
        return "sin detalle"


# =====================================================================
#  EDGE: síntesis (en línea, varias conexiones pocas y con reintentos)
# =====================================================================
def sintetizar_pendientes_edge(pendientes, ctx, total_bloques, ya_hechos):
    import edge_tts
    pct_vel = round((ctx["velocidad"] - 1.0) * 100)
    rate = f"{'+' if pct_vel >= 0 else ''}{pct_vel}%"
    prog = Progreso(total_bloques, ya_hechos, pendientes)
    fallo = []
    flags = 0x00004000 if sys.platform == "win32" else 0
    print(f"🌐 Conectando con Edge ({min(EDGE_CONCURRENCIA, len(pendientes))} conexiones a la vez)...")

    async def a_wav(mp3, wav):
        def _correr():
            return subprocess.run([FFMPEG_EXE, "-y", "-loglevel", "error", "-i", mp3,
                                   "-ar", "24000", "-ac", "1", wav],
                                  capture_output=True, text=True, creationflags=flags)
        r = await asyncio.to_thread(_correr)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg: " + (r.stderr or "").strip()[-200:])

    async def bloque(num, texto, final, sem):
        async with sem:
            if fallo:
                return
            tmp_mp3, tmp_wav = final[:-4] + ".part.mp3", final[:-4] + ".part.wav"
            ultimo = None
            for intento in range(EDGE_REINTENTOS + 1):
                try:
                    await edge_tts.Communicate(texto, ctx["voz"], rate=rate).save(tmp_mp3)
                    await a_wav(tmp_mp3, tmp_wav)
                    os.replace(tmp_wav, final)
                    prog.listo(num, texto)
                    return
                except Exception as e:
                    ultimo = e
                    if intento < EDGE_REINTENTOS and not fallo:
                        espera = 2 * (intento + 1) ** 2   # 2, 8, 18 s
                        print(f"⚠️ Bloque {num}: {type(e).__name__}. Reintento en {espera}s "
                              f"({intento + 1}/{EDGE_REINTENTOS})")
                        await asyncio.sleep(espera)
                finally:
                    _borrar(tmp_mp3, tmp_wav)
            fallo.append((num, ultimo))

    async def todo():
        sem = asyncio.Semaphore(EDGE_CONCURRENCIA)
        await asyncio.gather(*(bloque(n, t, f, sem) for n, t, f in pendientes))

    try:
        asyncio.run(todo())
    except KeyboardInterrupt:
        return "interrumpido"
    if fallo:
        print(f"\n❌ Error en el bloque {fallo[0][0]}: {type(fallo[0][1]).__name__}: {fallo[0][1]}")
        print("   Revisa tu conexión a internet. Si el error persiste, actualiza la librería:")
        print("   pip install -U edge-tts   (Microsoft cambia este servicio de vez en cuando)")
        return "error"
    return "ok"


# =====================================================================
#  ELECCIÓN DE MOTOR
# =====================================================================
def motivo_no_disponible(motor):
    """None si el motor se puede usar; si no, explica por qué."""
    if motor == "piper":
        if not PIPER_EXE:
            return "No se encontró piper (debe estar en la carpeta del programa: piper/piper.exe)."
    elif motor == "edge":
        if not HAS_EDGE:
            return "Falta la librería de Edge. Instálala con:  pip install edge-tts"
        if not FFMPEG_EXE:
            return "Edge necesita ffmpeg.exe en la carpeta del programa (convierte su audio)."
    elif motor == "kokoro":
        if not HAS_KOKORO:
            return "Faltan librerías. Instálalas con:  pip install kokoro-onnx soundfile"
        if not buscar_modelo_kokoro():
            return (f"Faltan 'kokoro-v1.0.onnx' y 'voices-v1.0.bin' en la carpeta '{CARPETA_KOKORO}'. "
                    "Deben ser de la versión 1.0 (los archivos 0.19 no sirven).")
    return None


def elegir_motor(defecto="piper"):
    claves = ["piper", "edge", "kokoro"]
    while True:
        opciones = []
        for c in claves:
            m = motivo_no_disponible(c)
            opciones.append(f"{NOMBRES_MOTOR[c]}  [{'DISPONIBLE' if not m else 'NO DISPONIBLE'}]")
        idx = menu("⚡ --- MOTOR DE VOZ ---", opciones, defecto=claves.index(defecto) + 1)
        motivo = motivo_no_disponible(claves[idx - 1])
        if not motivo:
            return claves[idx - 1]
        print(f"\n❌ {motivo}")


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


def exportar_audio(wavs, nombre_base, kbps, dir_temp, carpeta_salida, cpu_pct=100):
    """MP3 directo con ffmpeg (sin WAV gigante ni RAM). Si no se puede, WAV."""
    if kbps and FFMPEG_EXE:
        mp3 = ruta_libre(os.path.join(carpeta_salida, f"{nombre_base}.mp3"))
        lista = os.path.join(dir_temp, "lista.txt")
        with open(lista, "w", encoding="utf-8") as f:
            for w in wavs:
                f.write(f"file '{_escapar_ruta_concat(w)}'\n")
        print(f"\n📦 Uniendo y comprimiendo a MP3 ({kbps} kbps)...")
        cmd = [FFMPEG_EXE, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", lista, "-vn", "-ac", "1", "-codec:a", "libmp3lame",
               "-b:a", f"{kbps}k", "-metadata", f"title={nombre_base}", mp3]
        flags = 0x00004000 if sys.platform == "win32" else 0
        limite = LimiteCPU(cpu_pct)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace", creationflags=flags)
            limite.asignar(proc.pid)
            _, err = proc.communicate()
        finally:
            limite.cerrar()
        if proc.returncode == 0 and os.path.exists(mp3):
            return mp3
        print(f"⚠️ ffmpeg falló: {(err or '').strip()[-300:]}")
        print("   Se generará WAV en su lugar.")

    wav = ruta_libre(os.path.join(carpeta_salida, f"{nombre_base}.wav"))
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
    print("       🔊 GAGOBOLKA TTS v3.3 - MULTI-MOTOR          ")
    print("==================================================")

    os.chdir(BASE_DIR)  # rutas relativas (voces, textos, resultados) = carpeta del programa

    while True:
        archivo_texto = solicitar_archivo_texto()
        if not archivo_texto:
            print("\n👋 ¡Hasta luego!")
            break

        ajustes = pedir_ajustes()
        if not ajustes:
            continue
        motor = ajustes["motor"]

        print("\n" + describir_ajustes(ajustes))

        nombre_base = os.path.splitext(os.path.basename(archivo_texto))[0]
        dir_temp = os.path.join(CARPETA_TEMP, nombre_base)

        print(f"\n📖 Leyendo '{archivo_texto}'...")
        texto_completo = leer_texto(archivo_texto)

        # Bloques más chicos en textos cortos para poder repartirlos entre varios procesos.
        # Depende solo del texto (no de la CPU), así el progreso guardado sigue siendo válido.
        n_palabras = len(texto_completo.split())
        tam_bloque = max(100, min(1000, n_palabras // 12))
        bloques = dividir_texto_en_bloques(texto_completo, tam_bloque)
        total = len(bloques)
        print(f"📊 {n_palabras:,} palabras en {total} bloque(s).")

        # --- CHECKPOINT: solo se reanuda si motor/voz/velocidad/texto son los mismos ---
        firma = {
            "motor": motor,
            "voz": ajustes["voz"],
            "speaker": ajustes.get("speaker"),
            "velocidad": ajustes["velocidad"],
            "bloques": total,
            "hash": hashlib.md5(texto_completo.encode("utf-8")).hexdigest(),
        }
        meta = os.path.join(dir_temp, "meta.json")
        if os.path.isdir(dir_temp) and leer_json(meta) != firma:
            print("🧹 Había progreso previo con otro motor, voz, velocidad o texto. Se empieza de cero.")
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
            print("(Ctrl+C para pausar: el progreso se guarda)\n")
            ctx = {"voz": ajustes["voz"], "velocidad": ajustes["velocidad"],
                   "cpu_pct": ajustes["cpu_pct"], "dir_temp": dir_temp}
            if motor == "piper":
                ruta_modelo = os.path.join(CARPETA_VOCES, ajustes["voz"])
                n_nucleos = nucleos_para_porcentaje(ajustes["cpu_pct"])
                ctx.update({
                    "modelo": ruta_modelo,
                    "config": ruta_config_voz(ruta_modelo),
                    "length_scale": length_scale_desde_velocidad(ajustes["velocidad"]),
                    "speaker": ajustes.get("speaker"),
                    # Se usan los últimos núcleos: el 0 suele estar más ocupado por el sistema
                    "nucleos": NUCLEOS_DISPONIBLES[-n_nucleos:],
                })
                print(f"🚀 Piper: {min(n_nucleos, len(pendientes))} proceso(s) en paralelo.")
                estado = sintetizar_pendientes_piper(pendientes, ctx, total, ya_hechos)
            elif motor == "edge":
                estado = sintetizar_pendientes_edge(pendientes, ctx, total, ya_hechos)
            else:
                estado = sintetizar_pendientes_kokoro(pendientes, ctx, total, ya_hechos)
        else:
            print("✅ Todos los bloques ya estaban sintetizados; solo falta unirlos.")

        if estado == "ok":
            try:
                salida = ajustes.get("salida", BASE_DIR)
                if not carpeta_valida(salida):
                    print(f"\n⚠️ No se puede escribir en '{salida}'. Se usará la carpeta del programa.")
                    salida = BASE_DIR
                final = exportar_audio(wavs, nombre_base, ajustes["kbps"], dir_temp, salida, ajustes["cpu_pct"])
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
    # Modo interno: proceso hijo de Kokoro (lo lanza el propio programa; el usuario no lo usa)
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker-kokoro":
        _worker_kokoro(sys.argv[2], sys.argv[3])
        sys.exit(0)
    try:
        procesar_texto_masivo()
    except KeyboardInterrupt:
        print("\n\n👋 Cerrado por el usuario.")