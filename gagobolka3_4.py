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
import tempfile
import random
import unicodedata
from itertools import groupby
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
EDGE_CONCURRENCIA = 6
EDGE_REINTENTOS = 3
EDGE_PALABRAS_POR_BLOQUE = 2000
EDGE_KBPS_NATIVOS = 48
KOKORO_TIMEOUT_INICIO = 180


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


def elegir_calidad(motor="piper"):
    if not FFMPEG_EXE:
        print("\n⚠️ No se encontró ffmpeg: el resultado será WAV (pesado).")
        return 0
    calidades = CALIDADES
    if motor == "edge":
        original = (f"MP3 original de Edge ({EDGE_KBPS_NATIVOS} kbps)", EDGE_KBPS_NATIVOS)
        calidades = [original, *CALIDADES]
    idx = menu("📦 --- FORMATO DE AUDIO FINAL ---", [n for n, _ in calidades], defecto=1)
    return calidades[idx - 1][1]


def describir_ajustes(aj):
    motor = aj.get("motor", "piper")
    kbps = aj["kbps"]
    fmt = f"MP3 {kbps} kbps" if kbps else "WAV"
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
        "kbps": elegir_calidad(motor),
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
_SIGNOS_TTS = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'", "—": " - ", "–": " - ", "…": "...",
})
_URL_TTS = re.compile(r"\b(?:https?://|ftp://|www\.)[^\s<>\"“”]+", re.IGNORECASE)
_DECORACION_TTS = re.compile(r"([*=~#_+\-|<>/\\^])\1{2,}")
_EMOJI_TTS = re.compile(
    r"[0-9#*]\ufe0f?\u20e3|[\U0001f000-\U0001faff\u2194-\u21ff\u2300-\u23ff"
    r"\u25aa-\u25ff\u2600-\u27bf\u2934\u2935\u2b00-\u2bff\u3030\u303d"
    r"\u3297\u3299\u00a9\u00ae\u203c\u2049\u2122\u2139]\ufe0f?"
)


def _quitar_url_tts(coincidencia):
    url = coincidencia.group()
    return " " + url[len(url.rstrip(".,!?;:)]}")):]


def sanitizar_texto_tts(texto, max_sin_pausa=275):
    """Limpia ruido visual y conserva los saltos de párrafo para la narración."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = _EMOJI_TTS.sub(" ", texto)
    texto = "".join(
        " " if c == "\t" else c
        for c in texto
        if c in "\n\t" or (unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
                           and c not in "\ufe0e\ufe0f")
    )
    texto = _URL_TTS.sub(_quitar_url_tts, texto)
    texto = _DECORACION_TTS.sub(" ", texto).translate(_SIGNOS_TTS)
    lineas = []
    for linea in texto.split("\n"):
        palabras = linea.split()
        salida = []
        longitud = 0
        for palabra in palabras:
            if longitud and longitud + 1 + len(palabra) > max_sin_pausa:
                salida[-1] += ","
                longitud = 0
            salida.append(palabra)
            if re.search(r"""[.!?,;:]+["')\]]*$""", palabra):
                longitud = 0
            else:
                longitud += len(palabra) + bool(longitud)
        lineas.append(re.sub(r" +(?=[,.;:!?])", "", " ".join(salida)))
    limpio = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    return limpio if any(c.isalnum() for c in limpio) else ""


def dividir_texto_en_bloques(texto, max_palabras=1000):
    """Agrupa párrafos y también limita los que exceden el tamaño del bloque."""
    parrafos = []
    for parrafo in re.split(r"\n\s*\n", texto):
        palabras = list(re.finditer(r"\S+", parrafo))
        for inicio in range(0, len(palabras), max_palabras):
            grupo = palabras[inicio:inicio + max_palabras]
            parrafos.append(parrafo[grupo[0].start():grupo[-1].end()])
    return _agrupar_parrafos(parrafos, max_palabras)


def _dividir_bloques_legacy(texto, max_palabras):
    parrafos = [p.strip() for p in re.split(r'\n\s*\n', texto) if p.strip()]
    return _agrupar_parrafos(parrafos, max_palabras) or [texto]


def _agrupar_parrafos(parrafos, max_palabras):
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

    return bloques


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


def _archivo_no_vacio(ruta):
    try:
        return os.path.isfile(ruta) and os.path.getsize(ruta) > 0
    except OSError:
        return False


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
    por_nombre = {f.lower(): f for f in modelos}
    modelo = next((por_nombre[n] for n in ("kokoro-v1.0.int8.onnx", "kokoro-v1.0.onnx")
                   if n in por_nombre), modelos[0])
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
    """Solo el hilo de E/S puede bloquearse leyendo el protocolo del worker."""
    for linea in flujo:
        try:
            obj = json.loads(linea)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class _SesionKokoro:
    """Adapta el feed al modelo cargado sin alterar kokoro-onnx globalmente."""

    def __init__(self, sesion, np):
        tipos = {"tensor(float)": np.float32, "tensor(float16)": np.float16,
                 "tensor(double)": np.float64, "tensor(int64)": np.int64,
                 "tensor(int32)": np.int32}
        self.sesion = sesion
        self.np = np
        self.tipos = {entrada.name: tipos[entrada.type] for entrada in sesion.get_inputs()}
        self.velocidad = 1.0

    def __getattr__(self, nombre):
        return getattr(self.sesion, nombre)

    def run(self, output_names, input_feed, run_options=None):
        feed = dict(input_feed)
        for nombre, dtype in self.tipos.items():
            if nombre not in feed:
                continue
            if nombre == "speed":
                # Algunas versiones ya truncaron el float al construir un int32.
                forma = self.np.shape(feed[nombre])
                valor = self.velocidad
                if self.np.issubdtype(dtype, self.np.integer):
                    valor = max(1, round(valor))
                feed[nombre] = self.np.full(forma, valor, dtype=dtype)
            else:
                feed[nombre] = self.np.asarray(feed[nombre], dtype=dtype)
        return self.sesion.run(output_names, feed, run_options)


def _fragmentos_kokoro(texto, max_caracteres):
    """Prioriza el final de una oración o cláusula y luego el límite de palabra."""
    cortes = re.compile(r"""(?:[.!?;,:]["')\]]*|\n)\s+""")
    resto = texto.strip()
    while len(resto) > max_caracteres:
        ventana = resto[:max_caracteres + 1]
        pausas = list(cortes.finditer(ventana))
        fin = pausas[-1].end() if pausas and pausas[-1].end() >= max_caracteres // 2 else 0
        if not fin:
            fin = max(ventana.rfind(" "), ventana.rfind("\n"))
        if fin <= 0:
            fin = max_caracteres
        fragmento, resto = resto[:fin].strip(), resto[fin:].lstrip()
        if any(c.isalnum() for c in fragmento):
            yield fragmento
    if any(c.isalnum() for c in resto):
        yield resto


def _worker_kokoro(modelo, voces):
    """Carga una sesión por proceso y escribe cada bloque incrementalmente."""
    canal = sys.stdout
    sys.stdout = sys.stderr

    def enviar(obj):
        canal.write(json.dumps(obj) + "\n")
        canal.flush()

    try:
        from kokoro_onnx import Kokoro
        import numpy as np
        import onnxruntime as ort
        import soundfile as sf

        opciones = ort.SessionOptions()
        opciones.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opciones.intra_op_num_threads = max(1, int(os.getenv("GAGOBOLKA_KOKORO_HILOS", "1")))
        opciones.inter_op_num_threads = 1
        opciones.add_session_config_entry("session.intra_op.allow_spinning", "0")
        sesion = ort.InferenceSession(modelo, sess_options=opciones, providers=["CPUExecutionProvider"])
        kokoro = Kokoro.from_session(sesion, voces)
        kokoro.sess = _SesionKokoro(sesion, np)
        estilos = {}
        # Kokoro v1.0 sintetiza a 24 kHz. Se reutiliza esta pausa entre fragmentos.
        silencio = np.zeros(3600, dtype=np.float32)
    except Exception as e:
        enviar({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return

    enviar({"ready": True})
    for linea in sys.stdin:
        try:
            job = json.loads(linea)
        except ValueError:
            continue
        tmp = job["salida"][:-4] + ".part.wav"
        try:
            fragmentos = _fragmentos_kokoro(job["texto"], job.get("max_caracteres", 275))
            primero = next(fragmentos, None)
            if primero is None:
                raise ValueError("El bloque no contiene texto para sintetizar.")
            velocidad = float(job["velocidad"])
            kokoro.sess.velocidad = velocidad
            voz = job["voz"]
            if voz not in estilos:
                estilos[voz] = kokoro.get_voice_style(voz)
            parametros = {"voice": estilos[voz], "speed": velocidad, "lang": job["lang"]}
            muestras, tasa = kokoro.create(primero, **parametros)
            if not len(muestras):
                raise ValueError("Kokoro devolvió un fragmento de audio vacío.")
            with sf.SoundFile(tmp, mode="w", samplerate=tasa, channels=1, subtype="PCM_16") as audio:
                audio.write(muestras)
                for fragmento in fragmentos:
                    muestras, tasa_fragmento = kokoro.create(fragmento, **parametros)
                    if tasa_fragmento != tasa:
                        raise ValueError("Kokoro cambió la frecuencia de muestreo dentro del bloque.")
                    if not len(muestras):
                        raise ValueError("Kokoro devolvió un fragmento de audio vacío.")
                    audio.write(silencio)
                    audio.write(muestras)
            os.replace(tmp, job["salida"])
        except Exception as e:
            _borrar(tmp)
            enviar({"ok": False, "num": job["num"], "error": f"{type(e).__name__}: {e}"})
        else:
            enviar({"ok": True, "num": job["num"]})


def _comando_worker_kokoro(modelo, voces):
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
    return base + ["--worker-kokoro", os.path.abspath(modelo), os.path.abspath(voces)]


def _timeout_bloque_kokoro(texto, velocidad, cpu_pct):
    factor_cpu = max(1.0, 50 / max(1, cpu_pct))
    carga = max(len(texto.split()), len(texto) / 6)
    return max(60, math.ceil((20 + 0.45 * carga / max(0.5, velocidad)) * factor_cpu))


class _CanalWorkerKokoro:
    """Separa las operaciones de pipe del hilo que aplica el timeout."""

    def __init__(self, proc):
        self.proc = proc
        self.pedidos = queue.Queue()
        self.respuestas = queue.Queue()
        self.hilo = threading.Thread(target=self._atender, name="kokoro-io", daemon=True)
        self.hilo.start()

    def _atender(self):
        try:
            listo = _leer_json_linea(self.proc.stdout)
            self.respuestas.put(listo)
            if not listo or not listo.get("ready"):
                return
            while (job := self.pedidos.get()) is not None:
                self.proc.stdin.write(json.dumps(job) + "\n")
                self.proc.stdin.flush()
                resp = _leer_json_linea(self.proc.stdout)
                self.respuestas.put(resp)
                if resp is None:
                    return
        except (OSError, ValueError):
            self.respuestas.put(None)

    def cerrar(self):
        _matar_worker_kokoro(self.proc)
        self.pedidos.put(None)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.hilo.join(timeout=2)
        if not self.hilo.is_alive():
            for flujo in (self.proc.stdin, self.proc.stdout):
                try:
                    flujo.close()
                except OSError:
                    pass


def _matar_worker_kokoro(proc):
    try:
        proc.kill()
    except OSError:
        pass


def sintetizar_pendientes_kokoro(pendientes, ctx, total_bloques, ya_hechos):
    if not pendientes:
        return "ok"
    archivos = buscar_modelo_kokoro()
    if not archivos:
        print(f"\n❌ Faltan los archivos de Kokoro en '{CARPETA_KOKORO}'.")
        return "error"
    modelo, voces = archivos
    n_nucleos = nucleos_para_porcentaje(ctx["cpu_pct"])
    n_workers = min(n_nucleos, MAX_WORKERS_KOKORO, len(pendientes))
    hilos = max(1, n_nucleos // n_workers)
    nucleos = NUCLEOS_DISPONIBLES[-n_nucleos:]
    trabajos = queue.Queue()
    for pendiente in pendientes:
        trabajos.put(pendiente)
    prog = Progreso(total_bloques, ya_hechos, pendientes)
    fallos = []
    activos = set()
    lock = threading.Lock()
    stop = threading.Event()
    flags = 0x00004000 if sys.platform == "win32" else 0
    limite = LimiteCPU(ctx["cpu_pct"])
    anunciar_limite(limite, ctx["cpu_pct"])
    env = entorno_un_hilo()
    env["GAGOBOLKA_KOKORO_HILOS"] = str(hilos)
    env["PYTHONIOENCODING"] = "utf-8"
    lang = lang_de_voz_kokoro(ctx["voz"])
    velocidad = float(ctx["velocidad"])
    print(f"🧠 Kokoro: {os.path.basename(modelo)}, {n_workers} proceso(s), {hilos} hilo(s) por proceso.")

    def arrancar(i, log):
        with lock:
            if stop.is_set():
                return None
            proc = subprocess.Popen(
                _comando_worker_kokoro(modelo, voces),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env,
                text=True, encoding="utf-8", errors="replace", creationflags=flags)
            activos.add(proc)
        if not limite.asignar(proc.pid) and ctx["cpu_pct"] < 100:
            inicio = i * hilos
            aplicar_limites(proc.pid, nucleos[inicio:inicio + hilos])
        return _CanalWorkerKokoro(proc)

    def cerrar(canal):
        try:
            canal.cerrar()
        finally:
            with lock:
                activos.discard(canal.proc)

    def worker(i):
        log_path = os.path.join(ctx["dir_temp"], f"kokoro_{i}.log")
        canal = None
        num = "-"
        tmp = None
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                while not stop.is_set():
                    try:
                        num, texto, final = trabajos.get_nowait()
                    except queue.Empty:
                        break
                    tmp = final[:-4] + ".part.wav"
                    plazo = _timeout_bloque_kokoro(texto, velocidad, ctx["cpu_pct"])
                    for intento, max_caracteres in enumerate((275, 140)):
                        if canal is None:
                            canal = arrancar(i, log)
                            if canal is None:
                                return
                            try:
                                listo = canal.respuestas.get(timeout=KOKORO_TIMEOUT_INICIO)
                            except queue.Empty as e:
                                raise RuntimeError("Kokoro excedió el tiempo de inicio.") from e
                            if not listo or not listo.get("ready"):
                                detalle = (listo or {}).get("error") or _cola_log(log_path)
                                raise RuntimeError(f"Kokoro no pudo iniciar: {detalle}")
                        job = {"num": num, "texto": texto, "voz": ctx["voz"],
                               "velocidad": velocidad, "lang": lang, "salida": final,
                               "max_caracteres": max_caracteres}
                        canal.pedidos.put(job)
                        try:
                            resp = canal.respuestas.get(timeout=plazo)
                        except queue.Empty:
                            cerrar(canal)
                            canal = None
                            _borrar(tmp)
                            if _archivo_no_vacio(final):
                                break
                            if stop.is_set():
                                return
                            if intento:
                                raise RuntimeError(f"Kokoro excedió {plazo}s por bloque en ambos intentos.")
                            print(f"⚠️ Bloque {num}: Kokoro excedió {plazo}s. "
                                  "Reiniciando el worker con fragmentos más cortos.")
                            continue
                        if resp is None:
                            raise RuntimeError(f"El worker se cerró: {_cola_log(log_path)}")
                        if resp.get("num") != num or not resp.get("ok") or not _archivo_no_vacio(final):
                            raise RuntimeError(resp.get("error", "El worker no entregó el bloque esperado."))
                        break
                    prog.listo(num, texto)
        except (OSError, RuntimeError, ValueError) as e:
            if not stop.is_set():
                fallos.append((num, str(e)))
                stop.set()
        finally:
            if canal is not None:
                cerrar(canal)
            if tmp is not None:
                _borrar(tmp)

    ex = ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="kokoro")
    interrumpido = False
    try:
        futuros = [ex.submit(worker, i) for i in range(n_workers)]
        for futuro in as_completed(futuros):
            futuro.result()
    except KeyboardInterrupt:
        interrumpido = True
        stop.set()
        with lock:
            for proc in activos:
                _matar_worker_kokoro(proc)
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
    rate = f"{pct_vel:+d}%"
    prog = Progreso(total_bloques, ya_hechos, pendientes)
    fallos = []
    trabajos = iter(pendientes)
    conexiones = min(EDGE_CONCURRENCIA, len(pendientes))
    print(f"🌐 Conectando con Edge ({conexiones} conexiones a la vez)...")

    async def worker():
        while not fallos:
            pendiente = next(trabajos, None)
            if pendiente is None:
                return
            num, texto, final = pendiente
            tmp_mp3 = final[:-4] + ".part.mp3"
            for intento in range(EDGE_REINTENTOS + 1):
                try:
                    await edge_tts.Communicate(texto, ctx["voz"], rate=rate).save(tmp_mp3)
                    if not _archivo_no_vacio(tmp_mp3):
                        raise ValueError("Edge devolvió un archivo MP3 vacío.")
                    os.replace(tmp_mp3, final)
                except Exception as e:
                    if intento == EDGE_REINTENTOS:
                        fallos.append((num, e))
                        return
                    espera = 2 ** (intento + 1) + random.uniform(0, 1)
                    print(f"⚠️ Bloque {num}: {type(e).__name__}. Reintento en {espera:.1f}s "
                          f"({intento + 1}/{EDGE_REINTENTOS})")
                else:
                    prog.listo(num, texto)
                    break
                finally:
                    _borrar(tmp_mp3)
                await asyncio.sleep(espera)
                if fallos:
                    return

    async def todo():
        await asyncio.gather(*(worker() for _ in range(conexiones)))

    try:
        asyncio.run(todo())
    except KeyboardInterrupt:
        return "interrumpido"
    if fallos:
        num, error = fallos[0]
        print(f"\n❌ Error en el bloque {num}: {type(error).__name__}: {error}")
        print("   Revisa tu conexión a internet. Si el error persiste, actualiza edge-tts.")
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
            return "Edge necesita ffmpeg.exe para unir los bloques y exportar el audio final."
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


def _exportar_concat_ffmpeg(bloques, destino, opciones, lista, cpu_pct):
    with open(lista, "w", encoding="utf-8") as f:
        for bloque in bloques:
            f.write(f"file '{_escapar_ruta_concat(bloque)}'\n")
    cmd = [FFMPEG_EXE, "-nostdin", "-y", "-loglevel", "error", "-xerror", "-f", "concat",
           "-safe", "0", "-i", lista, "-map", "0:a:0", *opciones, destino]
    flags = 0x00004000 if sys.platform == "win32" else 0
    limite = LimiteCPU(cpu_pct)
    try:
        with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True, errors="replace", creationflags=flags) as proc:
            limite.asignar(proc.pid)
            try:
                _, err = proc.communicate()
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
        if proc.returncode or not _archivo_no_vacio(destino):
            raise RuntimeError("ffmpeg: " + ((err or "").strip()[-300:] or "archivo de salida vacío"))
    finally:
        limite.cerrar()


def exportar_audio(bloques, nombre_base, kbps, dir_temp, carpeta_salida, cpu_pct=100):
    """Concatena directamente si los bloques usan el mismo formato."""
    formatos = {os.path.splitext(ruta)[1].lower() for ruta in bloques}
    if not formatos or not formatos <= {".wav", ".mp3"}:
        raise ValueError("Se necesitan bloques de audio WAV o MP3 para exportar.")
    if ".mp3" in formatos and not FFMPEG_EXE:
        raise RuntimeError("Se necesita ffmpeg para exportar bloques MP3.")
    lista = os.path.join(dir_temp, "lista.txt")

    with tempfile.TemporaryDirectory(prefix=".gagobolka-", dir=carpeta_salida) as trabajo:
        if len(formatos) > 1:
            # Solo al reanudar un checkpoint de Edge v3.3 puede haber ambos formatos.
            wavs = []
            for i, (formato, grupo) in enumerate(groupby(
                    bloques, key=lambda ruta: os.path.splitext(ruta)[1].lower())):
                if formato == ".wav":
                    wavs.extend(grupo)
                else:
                    wav = os.path.join(trabajo, f"edge_{i}.wav")
                    _exportar_concat_ffmpeg(grupo, wav, ["-ac", "1", "-ar", "24000",
                                           "-c:a", "pcm_s16le"], lista, cpu_pct)
                    wavs.append(wav)
            bloques, formatos = wavs, {".wav"}

        if kbps and FFMPEG_EXE:
            mp3 = os.path.join(trabajo, "salida.mp3")
            if formatos == {".mp3"} and kbps == EDGE_KBPS_NATIVOS:
                opciones = ["-c:a", "copy"]
                print("\n🎧 Uniendo el MP3 original de Edge...")
            else:
                opciones = ["-ac", "1", "-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
                print(f"\n🎧 Uniendo y comprimiendo a MP3 ({kbps} kbps)...")
            try:
                _exportar_concat_ffmpeg(bloques, mp3, [*opciones, "-metadata",
                                       f"title={nombre_base}"], lista, cpu_pct)
            except (OSError, RuntimeError) as e:
                print(f"⚠️ {e}. Se intentará generar WAV.")
            else:
                final = ruta_libre(os.path.join(carpeta_salida, f"{nombre_base}.mp3"))
                os.replace(mp3, final)
                return final

        print("\n🎧 Uniendo bloques en un WAV...")
        wav = os.path.join(trabajo, "salida.wav")
        if formatos == {".mp3"}:
            _exportar_concat_ffmpeg(bloques, wav, ["-c:a", "pcm_s16le"], lista, cpu_pct)
        else:
            concatenar_wavs(bloques, wav)
        final = ruta_libre(os.path.join(carpeta_salida, f"{nombre_base}.wav"))
        os.replace(wav, final)
        return final


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


def _preparar_checkpoint(texto, ajustes, dir_temp, reiniciar=False):
    """Conserva los índices de v3.3 al continuar un trabajo existente."""
    n_palabras = len(texto.split())
    tam_legacy = max(100, min(1000, n_palabras // 12))
    motor = ajustes["motor"]
    firma = {
        "motor": motor,
        "voz": ajustes["voz"],
        "speaker": ajustes.get("speaker"),
        "velocidad": ajustes["velocidad"],
        "hash": hashlib.md5(texto.encode("utf-8")).hexdigest(),
    }
    meta = os.path.join(dir_temp, "meta.json")
    anterior = leer_json(meta)
    legacy = False
    if not reiniciar and isinstance(anterior, dict) and "version_bloques" not in anterior:
        bloques_legacy = _dividir_bloques_legacy(texto, tam_legacy)
        legacy = anterior == {**firma, "bloques": len(bloques_legacy)}

    if legacy:
        bloques = [(i, sanitizar_texto_tts(b)) for i, b in enumerate(bloques_legacy, 1)]
        firma = anterior
    else:
        tam_bloque = EDGE_PALABRAS_POR_BLOQUE if motor == "edge" else tam_legacy
        bloques = list(enumerate((sanitizar_texto_tts(b)
                                  for b in dividir_texto_en_bloques(texto, tam_bloque)), 1))
        firma.update(bloques=len(bloques), version_bloques=1)

    bloques = [(i, b) for i, b in bloques if b]
    if not bloques:
        return [], []

    if os.path.isdir(dir_temp) and (reiniciar or anterior != firma):
        if not reiniciar:
            print("⚠️ El progreso previo corresponde a otro texto o ajustes. Se empieza de cero.")
        shutil.rmtree(dir_temp)
    os.makedirs(dir_temp, exist_ok=True)
    if reiniciar or anterior != firma:
        tmp = meta + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(firma, f)
        os.replace(tmp, meta)

    audios, pendientes = [], []
    for num, (indice, bloque) in enumerate(bloques, 1):
        base = os.path.join(dir_temp, f"bloque_{indice:03d}")
        ruta = base + (".mp3" if motor == "edge" else ".wav")
        if legacy and motor == "edge" and not _archivo_no_vacio(ruta):
            wav_legacy = base + ".wav"
            if _archivo_no_vacio(wav_legacy):
                ruta = wav_legacy
        audios.append(ruta)
        if not _archivo_no_vacio(ruta):
            pendientes.append((num, bloque, ruta))
    return audios, pendientes


# --- PROCESO PRINCIPAL ---
def procesar_texto_masivo():
    print("==================================================")
    print("       🔊 GAGOBOLKA TTS v3.4.1 - MULTI-MOTOR      ")
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

        try:
            audios, pendientes = _preparar_checkpoint(texto_completo, ajustes, dir_temp)
        except OSError as e:
            print(f"\n❌ No se pudo preparar el checkpoint: {e}")
            continue
        if not audios:
            print("ℹ️ El archivo no contiene texto para sintetizar después de limpiarlo.")
            continue

        total = len(audios)
        print(f"📝 {len(texto_completo.split()):,} palabras en el archivo, {total} bloque(s) para narrar.")
        ya_hechos = total - len(pendientes)
        if 0 < ya_hechos and pendientes:
            print(f"📌 Se encontró un trabajo previo ({ya_hechos}/{total} bloques listos).")
            if input("¿Reanudar desde donde quedó? (S/n): ").strip().lower() == "n":
                try:
                    audios, pendientes = _preparar_checkpoint(
                        texto_completo, ajustes, dir_temp, reiniciar=True)
                except OSError as e:
                    print(f"\n❌ No se pudo reiniciar el checkpoint: {e}")
                    continue
                total, ya_hechos = len(audios), 0

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
                final = exportar_audio(
                    audios, nombre_base, ajustes["kbps"], dir_temp, salida, ajustes["cpu_pct"])
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