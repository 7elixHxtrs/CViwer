"""CViwer - visor de capturadora todo en uno: video directo + audio + ajustes de color + grabación."""
import json, os, queue, re, shutil, subprocess, sys, threading, time, wave
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import cv2
import numpy as np
from PIL import Image, ImageTk

try:
    import sounddevice as sd
except Exception:
    sd = None
try:
    from pygrabber.dshow_graph import FilterGraph
except Exception:
    FilterGraph = None
try:  # ffmpeg incluido (para grabar en H.264 con audio)
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg")
try:  # timers de 1 ms en Windows (video mas fluido)
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")


def recurso(nombre):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre)

BG, PANEL, FG, MUTED, ACC, ACC2 = "#141414", "#1f1f1f", "#eeeeee", "#9a9a9a", "#107c10", "#16a116"

RESOLUCIONES = [("720p  60 fps", 1280, 720, 60), ("1080p  30 fps", 1920, 1080, 30),
                ("1080p  60 fps", 1920, 1080, 60), ("480p  30 fps", 640, 480, 30)]

SLIDERS = [  # clave, etiqueta, minimo, maximo, neutro
    ("brillo", "Brillo", -100, 100, 0),
    ("contraste", "Contraste", 0, 200, 100),
    ("gamma", "Gamma", 50, 250, 100),
    ("saturacion", "Saturación", 0, 200, 100),
    ("tono", "Tono", -90, 90, 0),
    ("temperatura", "Temperatura", -100, 100, 0),
    ("volumen", "Volumen", 0, 100, 100),
]
NEUTRO = {k: d for k, _, _, _, d in SLIDERS}


# ---------------------------------------------------------------- utilidades
def cargar_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_cfg(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def listar_camaras():
    if FilterGraph is not None:
        try:
            return list(FilterGraph().get_input_devices())
        except Exception:
            pass
    nombres = []
    for i in range(6):
        c = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if c.isOpened():
            nombres.append(f"Cámara {i}")
        c.release()
    return nombres


def listar_audio(tipo, todos=False):
    """[(indice, nombre)]. Por defecto solo WASAPI (nombres completos); con todos=True, todas las APIs."""
    if sd is None:
        return []
    try:
        apis, devs = sd.query_hostapis(), sd.query_devices()
    except Exception:
        return []
    ch = f"max_{tipo}_channels"
    if todos:
        return [(i, f"{d['name']}  [{apis[d['hostapi']]['name'].replace('Windows ', '')}]")
                for i, d in enumerate(devs) if d[ch] > 0]
    for pref in ("WASAPI", "MME"):
        res = [(i, d["name"]) for i, d in enumerate(devs) if d[ch] > 0 and pref in apis[d["hostapi"]]["name"]]
        if res:
            return res
    return []


def salida_por_defecto():
    try:
        for a in sd.query_hostapis():
            if "WASAPI" in a["name"]:
                return a["default_output_device"]
    except Exception:
        pass
    return None


CLAVES_CAPTURA = ("ezcap", "digital audio", "usb audio", "usb digital", "hdmi", "capture", "captura",
                  "cam link", "elgato", "avermedia", "game", "usb3", "usb 3")
MALAS = ("microphone", "micrófono", "microfono", "auricular", "headset", "hands-free", "pixel", "parsec",
         "steam", "obs", "virtual", "mezcla", "stereo mix", "realtek", "sound mapper", "controlador")


def adivinar_audio(video, entradas):
    """Busca la entrada de audio que probablemente pertenece a la capturadora de video elegida."""
    toks = [t for t in re.split(r"[^a-z0-9]+", video.lower())
            if len(t) >= 4 and t not in ("capture", "video", "camera", "device", "usb3")]
    mejor, mejor_pts = None, 0
    for pos, (dev, nombre) in enumerate(entradas):
        if dev is None:
            continue
        n = nombre.lower()
        pts = sum(10 for t in toks if t in n) + sum(3 for c in CLAVES_CAPTURA if c in n)
        pts -= sum(5 for m in MALAS if m in n)
        if pts > mejor_pts:
            mejor, mejor_pts = pos, pts
    return mejor


class Camara:
    """Lee en un hilo aparte y conserva solo el ultimo cuadro (menos latencia)."""
    def __init__(self, idx, w, h, fps):
        self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.frame, self.n, self.run = None, 0, True
        self.ok = self.cap.isOpened()
        if self.ok:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.run:
            ok, f = self.cap.read()
            if ok:
                self.frame = f
                self.n += 1
            else:
                time.sleep(0.005)

    def cerrar(self):
        self.run = False
        time.sleep(0.05)
        self.cap.release()


def procesar(f, c, cache):
    b, ct, g, sat, tono, temp = (c["brillo"], c["contraste"], c["gamma"],
                                 c["saturacion"], c["tono"], c["temperatura"])
    if (b, ct, g, temp) != (0, 100, 100, 0):
        key = (b, ct, g, temp)
        if cache.get("k") != key:
            x = np.arange(256, dtype=np.float32) / 255.0
            x = np.power(x, 100.0 / max(g, 10))
            x = (x - 0.5) * (ct / 100.0) + 0.5 + b / 200.0
            lut = np.stack([x * (1 - temp / 300.0), x, x * (1 + temp / 300.0)], axis=1)  # B, G, R
            cache["lut"] = np.clip(lut * 255, 0, 255).astype(np.uint8).reshape(256, 1, 3)
            cache["k"] = key
        f = cv2.LUT(f, cache["lut"])
    if sat != 100 or tono != 0:
        h, s, v = cv2.split(cv2.cvtColor(f, cv2.COLOR_BGR2HSV))
        if sat != 100:
            s = cv2.convertScaleAbs(s, alpha=sat / 100.0)
        if tono != 0:
            h = ((h.astype(np.int16) + tono) % 180).astype(np.uint8)
        f = cv2.cvtColor(cv2.merge((h, s, v)), cv2.COLOR_HSV2BGR)
    return f



# ---------------------------------------------------------------- grabacion
SIN_VENTANA = 0x08000000 if os.name == "nt" else 0
_ENC, _ENC_LOCK = None, threading.Lock()


def mejor_encoder():
    """Prueba los codificadores por hardware (NVIDIA, Intel, AMD); si ninguno sirve usa x264 (CPU)."""
    global _ENC
    with _ENC_LOCK:
        if _ENC or not FFMPEG:
            return _ENC
        candidatos = [
            ("h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]),
            ("h264_nvenc", ["-preset", "fast", "-rc", "vbr", "-cq", "23", "-b:v", "0"]),
            ("h264_qsv", ["-global_quality", "23"]),
            ("h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]),
        ]
        for enc, extra in candidatos:
            try:
                r = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
                                    "-frames:v", "8", "-pix_fmt", "yuv420p", "-c:v", enc, *extra, "-f", "null", "-"],
                                   capture_output=True, timeout=12, creationflags=SIN_VENTANA)
                if r.returncode == 0:
                    _ENC = (enc, extra)
                    return _ENC
            except Exception:
                pass
        _ENC = ("libx264", ["-preset", "veryfast", "-crf", "21"])
        return _ENC


class Grabador:
    """Graba el video (ya con los ajustes de color) a ritmo constante y, si hay, el audio de la capturadora."""
    def __init__(self, ruta, w, h, fps, get_frame, audio):
        self.w, self.h = w - w % 2, h - h % 2
        self.fps, self.get_frame, self.ruta = fps, get_frame, ruta
        base = os.path.splitext(ruta)[0]
        self.tmp_video, self.tmp_wav = base + "_video.mp4", base + "_audio.wav"
        self.activo = True
        self.proc = self.vw = self.wav = self.audio_q = self.audio_hilo = None
        self.con_audio = False
        self.codec = "mp4v"
        if FFMPEG:
            enc, extra = mejor_encoder()
            self.codec = enc
            self.con_audio = audio is not None
            destino = self.tmp_video if self.con_audio else ruta
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{self.w}x{self.h}", "-framerate", str(fps), "-i", "-",
                   "-c:v", enc, *extra, "-pix_fmt", "yuv420p", destino]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=SIN_VENTANA)
            if self.con_audio:
                sr, ch = audio
                self.audio_q = queue.Queue(maxsize=4000)
                self.wav = wave.open(self.tmp_wav, "wb")
                self.wav.setnchannels(ch)
                self.wav.setsampwidth(2)
                self.wav.setframerate(sr)
                self.audio_hilo = threading.Thread(target=self._audio_loop, daemon=True)
                self.audio_hilo.start()
        else:  # sin ffmpeg: video simple sin audio
            self.vw = cv2.VideoWriter(ruta, cv2.VideoWriter_fourcc(*"mp4v"), fps, (self.w, self.h))
            if not self.vw.isOpened():
                raise RuntimeError("No se pudo crear el archivo de video.")
        self.hilo = threading.Thread(target=self._loop, daemon=True)
        self.hilo.start()

    def _audio_loop(self):
        while True:
            d = self.audio_q.get()
            if d is None:
                break
            self.wav.writeframes((np.clip(d, -1, 1) * 32767).astype("<i2").tobytes())

    def _loop(self):
        dt = 1.0 / self.fps
        t = time.perf_counter()
        while self.activo:
            f = self.get_frame()
            if f is not None:
                f = f[:self.h, :self.w]
                if f.shape[0] != self.h or f.shape[1] != self.w:
                    f = cv2.resize(f, (self.w, self.h))
                try:
                    if self.proc:
                        self.proc.stdin.write(f.tobytes())
                    else:
                        self.vw.write(f)
                except Exception:
                    break
            t += dt
            espera = t - time.perf_counter()
            if espera > 0:
                time.sleep(espera)
            elif espera < -0.5:
                t = time.perf_counter()

    def detener(self):
        """Cierra los archivos (y une audio + video). Devuelve la ruta del resultado."""
        self.activo = False
        self.hilo.join(timeout=5)
        if self.audio_q is not None:
            self.audio_q.put(None)
            self.audio_hilo.join(timeout=10)
            self.wav.close()
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=120)
            except Exception:
                self.proc.kill()
        if self.vw:
            self.vw.release()
        resultado = self.ruta
        if self.con_audio and os.path.exists(self.tmp_video):
            try:
                r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", self.tmp_video, "-i", self.tmp_wav,
                                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                                    "-movflags", "+faststart", self.ruta],
                                   timeout=600, creationflags=SIN_VENTANA)
                ok = r.returncode == 0
            except Exception:
                ok = False
            if ok:
                for p in (self.tmp_video, self.tmp_wav):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            else:
                resultado = self.tmp_video  # se conservan video y audio por separado
        return resultado


# ---------------------------------------------------------------- aplicacion
class App:
    def __init__(self):
        self.cfg = cargar_cfg()
        self.color = {**NEUTRO, **self.cfg.get("color", {})}
        self.vol = [self.color["volumen"] / 100.0]
        self.nivel = [0.0]
        self.running = False
        self.thread = None
        self.audio = None
        self.msg = ""
        self.status_txt = ""
        self.fps = 0.0
        self.contador = 0
        self.t_fps = time.time()
        self.latest = self.pintado = self.ultimo = None
        self.photo = None
        self.canvas_size = (960, 540)
        self.fs = False
        self.flag_reset = False
        self.panel_visible = True
        self.rec_q = [None]
        self.audio_info = None
        self.grabador = None
        self.finalizando = False
        self.fin_hilo = None
        self.t_rec = 0.0
        self.secciones = {}
        self.dirs = {"grab": self.cfg.get("dir_grab") or os.path.join(BASE, "Grabaciones"),
                     "cap": self.cfg.get("dir_cap") or os.path.join(BASE, "Capturas")}
        self.lbl_dir = {}
        self._panel_puesto = True
        self._aviso_job = None

        self.root = tk.Tk()
        self.root.title("CViwer")
        try:
            self._icono = tk.PhotoImage(file=recurso("icono.png"))
            self.root.iconphoto(True, self._icono)
        except Exception:
            pass
        self.root.configure(bg=BG)
        self.root.geometry("1380x900")
        self.root.minsize(1000, 760)
        self.estilo()
        self.construir()
        self.refrescar_dispositivos(reinit=False)
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)
        for t in ("f", "F"):
            self.root.bind(f"<{t}>", lambda e: self.pantalla_completa())
        for t in ("c", "C"):
            self.root.bind(f"<{t}>", lambda e: self.alternar_panel())
        for t in ("r", "R"):
            self.root.bind(f"<{t}>", lambda e: self.alternar_grabacion())
        for t in ("p", "P"):
            self.root.bind(f"<{t}>", lambda e: self.captura())
        self.root.bind("<F11>", lambda e: self.pantalla_completa())
        self.root.bind("<Escape>", lambda e: self.pantalla_completa(False) if self.fs else None)
        self.root.after(250, self.poll)
        self.root.after(4, self.tick)
        threading.Thread(target=mejor_encoder, daemon=True).start()

    # ---- estilo
    def estilo(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Card.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 17, "bold"))
        s.configure("Sub.TLabel", background=BG, foreground=MUTED)
        s.configure("Sec.TLabel", background=PANEL, foreground=ACC2, font=("Segoe UI", 10, "bold"))
        s.configure("TCombobox", fieldbackground="#2b2b2b", background="#2b2b2b", foreground=FG,
                    arrowcolor=FG, bordercolor="#3a3a3a", lightcolor="#2b2b2b", darkcolor="#2b2b2b")
        s.map("TCombobox", fieldbackground=[("readonly", "#2b2b2b")], foreground=[("readonly", FG)],
              selectbackground=[("readonly", "#2b2b2b")], selectforeground=[("readonly", FG)])
        s.configure("Horizontal.TScale", background=PANEL, troughcolor="#333333", bordercolor=PANEL,
                    lightcolor=ACC2, darkcolor=ACC2)
        s.configure("TButton", background="#2f2f2f", foreground=FG, bordercolor="#3a3a3a",
                    padding=(8, 5), focuscolor="#2f2f2f")
        s.map("TButton", background=[("active", "#3c3c3c")])
        s.configure("Small.TButton", padding=(8, 3), font=("Segoe UI", 9))
        s.configure("Go.TButton", background=ACC, foreground="white", font=("Segoe UI", 12, "bold"),
                    padding=(10, 9), bordercolor=ACC)
        s.map("Go.TButton", background=[("active", ACC2)])
        s.configure("Stop.TButton", background="#b3261e", foreground="white", font=("Segoe UI", 12, "bold"),
                    padding=(10, 9), bordercolor="#b3261e")
        s.map("Stop.TButton", background=[("active", "#d03a31")])
        s.configure("TCheckbutton", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("Level.Horizontal.TProgressbar", background=ACC2, troughcolor="#333333",
                    bordercolor=PANEL, lightcolor=ACC2, darkcolor=ACC2, thickness=6)
        self.root.option_add("*TCombobox*Listbox.background", "#2b2b2b")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACC)
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

    # ---- interfaz
    def tarjeta(self, parent, titulo):
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(0, 8))
        ttk.Label(card, text=titulo.upper(), style="Sec.TLabel").grid(row=0, column=0, columnspan=3,
                                                                       sticky="w", pady=(0, 6))
        card.columnconfigure(1, weight=1)
        return card

    def seccion(self, parent, titulo, clave):
        """Tarjeta desplegable (acordeón: al abrir una se cierra la otra). Devuelve el contenedor interior."""
        card = ttk.Frame(parent, style="Card.TFrame", padding=(12, 9))
        card.pack(fill="x", pady=(0, 8))
        cab = ttk.Label(card, text=f"▸  {titulo.upper()}", style="Sec.TLabel", cursor="hand2")
        cab.pack(fill="x")
        cuerpo = ttk.Frame(card, style="Card.TFrame")
        cuerpo.columnconfigure(1, weight=1)
        self.secciones[clave] = {"cab": cab, "cuerpo": cuerpo, "titulo": titulo.upper(), "abierta": False}
        cab.bind("<Button-1>", lambda e: self.abrir_seccion(clave))
        return cuerpo

    def abrir_seccion(self, clave):
        for k, sec in self.secciones.items():
            abrir = (k == clave) and not sec["abierta"]
            if abrir and not sec["abierta"]:
                sec["cuerpo"].pack(fill="x", pady=(8, 0))
            elif not abrir and sec["abierta"]:
                sec["cuerpo"].pack_forget()
            sec["abierta"] = abrir
            sec["cab"].configure(text=("▾  " if abrir else "▸  ") + sec["titulo"])

    def construir(self):
        self.panel = ttk.Frame(self.root, padding=(14, 12))
        self.panel.pack(side="left", fill="y")
        self.canvas = tk.Canvas(self.root, bg="#000000", highlightthickness=0, cursor="arrow")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.img_id = self.canvas.create_image(0, 0, anchor="center")
        self.txt_id = self.canvas.create_text(0, 0, fill="#555555", font=("Segoe UI", 16),
                                              text="Elige tu capturadora y pulsa  ▶ Iniciar")
        self.aviso_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="#111111", outline="", state="hidden")
        self.aviso_id = self.canvas.create_text(0, 0, fill="#ffffff", font=("Segoe UI", 12), text="")
        self.rec_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="#111111", outline="", state="hidden")
        self.rec_id = self.canvas.create_text(0, 0, anchor="ne", fill="#ff3b30", text="",
                                              font=("Segoe UI", 13, "bold"))
        self.canvas.bind("<Configure>", self.al_redimensionar)
        self.canvas.bind("<Double-Button-1>", lambda e: self.pantalla_completa())

        main = self.panel
        ttk.Label(main, text="🎮  CViwer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="Video directo, sin OBS y con poco consumo", style="Sub.TLabel").pack(
            anchor="w", pady=(0, 10))

        # Fuentes
        c = self.tarjeta(main, "Fuentes")
        self.cb = {}
        for i, (k, txt) in enumerate([("video", "Video"), ("ain", "Audio entrada"),
                                      ("aout", "Audio salida"), ("res", "Calidad")], start=1):
            ttk.Label(c, text=txt, style="Card.TLabel", width=12).grid(row=i, column=0, sticky="w", pady=3)
            cb = ttk.Combobox(c, state="readonly", width=34)
            cb.grid(row=i, column=1, columnspan=2, sticky="ew", pady=3)
            self.cb[k] = cb
        self.cb["res"]["values"] = [r[0] for r in RESOLUCIONES]
        self.cb["video"].bind("<<ComboboxSelected>>", lambda e: self.auto_audio(True))
        ttk.Label(c, text="Nivel", style="Card.TLabel", width=12).grid(row=5, column=0, sticky="w", pady=3)
        self.barra = ttk.Progressbar(c, maximum=100, style="Level.Horizontal.TProgressbar")
        self.barra.grid(row=5, column=1, columnspan=2, sticky="ew", pady=3)
        self.todos = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Mostrar todos los dispositivos de audio", variable=self.todos,
                        command=lambda: self.refrescar_dispositivos(reinit=False)).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(c, text="⟳  Actualizar dispositivos",
                   command=lambda: self.refrescar_dispositivos(reinit=True)).grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        # Color
        c = self.seccion(main, "Imagen y sonido", "color")
        self.vars, self.vlabels = {}, {}
        for i, (k, txt, lo, hi, ne) in enumerate(SLIDERS):
            ttk.Label(c, text=txt, style="Card.TLabel", width=12).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.IntVar(value=int(self.color[k]))
            self.vars[k] = v
            sc = ttk.Scale(c, from_=lo, to=hi, variable=v, orient="horizontal",
                           command=lambda val, key=k: self.cambio(key, val))
            sc.grid(row=i, column=1, sticky="ew", padx=(0, 8), pady=2)
            lb = ttk.Label(c, text=self.fmt(k, v.get()), style="Muted.TLabel", width=5, anchor="e")
            lb.grid(row=i, column=2, sticky="e")
            self.vlabels[k] = lb
            sc.bind("<Double-Button-1>", lambda e, key=k, n=ne: self.poner(key, n))
        ttk.Button(c, text="↺  Restablecer color", command=self.restablecer).grid(
            row=len(SLIDERS), column=0, columnspan=3, sticky="ew", pady=(8, 0))

        # Carpetas de guardado
        c = self.seccion(main, "Carpetas de guardado", "carpetas")
        for i, (k, nombre) in enumerate([("grab", "Grabaciones"), ("cap", "Capturas")]):
            fila = i * 2
            ttk.Label(c, text=nombre, style="Card.TLabel").grid(row=fila, column=0, sticky="w")
            ttk.Button(c, text="Cambiar…", style="Small.TButton",
                       command=lambda k=k: self.elegir_carpeta(k)).grid(row=fila, column=1, sticky="e", padx=(0, 4))
            ttk.Button(c, text="Abrir", style="Small.TButton",
                       command=lambda k=k: self.abrir_carpeta(k)).grid(row=fila, column=2, sticky="e")
            lb = ttk.Label(c, text=self.corto(self.dirs[k]), style="Muted.TLabel")
            lb.grid(row=fila + 1, column=0, columnspan=3, sticky="w", pady=(0, 8 if i == 0 else 0))
            self.lbl_dir[k] = lb
        if self.cfg.get("seccion") in self.secciones:
            self.abrir_seccion(self.cfg["seccion"])

        # Botones
        self.btn_go = ttk.Button(main, text="▶  Iniciar", style="Go.TButton", command=self.alternar)
        self.btn_go.pack(fill="x")
        fr2 = ttk.Frame(main)
        fr2.pack(fill="x", pady=(6, 0))
        fr2.columnconfigure((0, 1), weight=1)
        ttk.Button(fr2, text="⛶  Pantalla completa (F)", command=self.pantalla_completa).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(fr2, text="📷  Captura (P)", command=self.captura).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.btn_rec = ttk.Button(fr2, text="⏺  Grabar  (R)", command=self.alternar_grabacion)
        self.btn_rec.grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=(6, 0))
        ttk.Button(fr2, text="📁  Carpeta", command=self.abrir_carpeta).grid(
            row=1, column=1, sticky="ew", padx=(3, 0), pady=(6, 0))
        ttk.Button(fr2, text="☰  Ocultar menú  (C)", command=self.alternar_panel).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self.status = ttk.Label(main, text="Listo.", style="Sub.TLabel", wraplength=360, justify="left")
        self.status.pack(anchor="w", pady=(10, 0))

    def al_redimensionar(self, e):
        self.canvas_size = (e.width, e.height)
        self.canvas.coords(self.img_id, e.width / 2, e.height / 2)
        self.canvas.coords(self.txt_id, e.width / 2, e.height / 2)
        self.canvas.coords(self.aviso_id, e.width / 2, e.height - 40)
        self.canvas.coords(self.rec_id, e.width - 26, 22)

    def fmt(self, k, v):
        if k == "gamma":
            return f"{v / 100:.2f}"
        if k in ("contraste", "saturacion", "volumen"):
            return f"{v}%"
        return f"{v:+d}" if v else "0"

    def cambio(self, k, val):
        v = int(round(float(val)))
        self.color[k] = v
        self.vlabels[k].configure(text=self.fmt(k, v))
        if k == "volumen":
            self.vol[0] = v / 100.0

    def poner(self, k, v):
        self.vars[k].set(v)
        self.cambio(k, v)

    def restablecer(self):
        for k, d in NEUTRO.items():
            if k != "volumen":
                self.poner(k, d)

    def pantalla_completa(self, valor=None):
        self.fs = (not self.fs) if valor is None else valor
        self.root.attributes("-fullscreen", self.fs)
        self.canvas.config(cursor="none" if self.fs else "arrow")
        self.aplicar_panel()
        if self.fs:
            self.aviso("Esc para salir de pantalla completa")
        self.root.focus_set()

    def alternar_panel(self):
        self.panel_visible = not self.panel_visible
        self.aplicar_panel()
        if not self.panel_visible:
            self.aviso("Pulsa C para mostrar el menú")
        self.root.focus_set()

    def aplicar_panel(self):
        mostrar = self.panel_visible and not self.fs
        if mostrar and not self._panel_puesto:
            self.panel.pack(side="left", fill="y", before=self.canvas)
        elif not mostrar and self._panel_puesto:
            self.panel.pack_forget()
        self._panel_puesto = mostrar

    def aviso(self, texto, ms=2500):
        self.canvas.itemconfig(self.aviso_id, text=texto)
        x1, y1, x2, y2 = self.canvas.bbox(self.aviso_id)
        self.canvas.coords(self.aviso_bg, x1 - 12, y1 - 6, x2 + 12, y2 + 6)
        self.canvas.itemconfig(self.aviso_bg, state="normal")
        self.canvas.tag_raise(self.aviso_bg)
        self.canvas.tag_raise(self.aviso_id)
        if self._aviso_job:
            self.root.after_cancel(self._aviso_job)
        self._aviso_job = self.root.after(ms, self.quitar_aviso)

    def quitar_aviso(self):
        self.canvas.itemconfig(self.aviso_id, text="")
        self.canvas.itemconfig(self.aviso_bg, state="hidden")

    def captura(self):
        if self.ultimo is None:
            return
        try:
            ruta = os.path.join(self.asegurar_dir("cap"), time.strftime("captura_%Y%m%d_%H%M%S.png"))
            ok, buf = cv2.imencode(".png", self.ultimo)  # (imwrite falla con acentos en la ruta)
            if not ok:
                raise RuntimeError("No se pudo codificar la imagen.")
            buf.tofile(ruta)
            self.status_txt = f"Captura guardada en:\n{ruta}"
        except Exception as e:
            self.status_txt = f"No se pudo guardar la captura: {e}"

    # ---- dispositivos
    def refrescar_dispositivos(self, reinit=True):
        if reinit and sd is not None and not self.audio:
            try:  # PortAudio guarda la lista al abrir; la reiniciamos para ver dispositivos nuevos
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        todos = self.todos.get()
        self.cams = listar_camaras()
        self.ains = [(None, "Sin audio")] + listar_audio("input", todos)
        self.aouts = listar_audio("output", todos)
        nin, nout = [n for _, n in self.ains], [n for _, n in self.aouts]
        self.cb["video"]["values"] = self.cams or ["(no se encontró ninguna)"]
        self.cb["ain"]["values"] = nin
        self.cb["aout"]["values"] = nout or ["(predeterminada)"]

        # video: lo guardado, o la primera que no sea una camara virtual / del equipo
        virtuales = ("obs", "ivcam", "integrated", "virtual", "droidcam", "webcam")
        pref = [i for i, n in enumerate(self.cams) if not any(v in n.lower() for v in virtuales)]
        guardado = self.cfg.get("cam")
        self.cb["video"].current(self.cams.index(guardado) if guardado in self.cams
                                 else (pref[0] if pref else 0))
        # audio entrada: lo guardado (si no era "Sin audio") o autodeteccion
        g_in = self.cfg.get("ain")
        if g_in in nin and g_in != "Sin audio":
            self.cb["ain"].current(nin.index(g_in))
        else:
            self.cb["ain"].current(0)
            self.auto_audio(False)
        pred = salida_por_defecto()
        idx_pred = next((i for i, (d, _) in enumerate(self.aouts) if d == pred), 0)
        g_out = self.cfg.get("aout")
        self.cb["aout"].current(nout.index(g_out) if g_out in nout else idx_pred)
        self.cb["res"].current(self.cfg.get("res", 0))
        if not self.cams:
            self.status.configure(text="No se detectó ninguna capturadora. Conéctala y pulsa «Actualizar».")

    def auto_audio(self, avisar):
        video = self.cb["video"].get()
        pos = adivinar_audio(video, self.ains)
        if pos is not None:
            self.cb["ain"].current(pos)
            self.status.configure(text=f"Audio de la capturadora detectado:\n{self.ains[pos][1]}")
        elif avisar:
            self.status.configure(text="No encontré el audio de esta capturadora; elígelo manualmente "
                                       "o marca «Mostrar todos los dispositivos».")

    # ---- audio
    def iniciar_audio(self):
        i = self.cb["ain"].current()
        din = self.ains[i][0] if i >= 0 else None
        if sd is None or din is None:
            return
        j = self.cb["aout"].current()
        dout = self.aouts[j][0] if self.aouts and j >= 0 else None
        try:
            ii = sd.query_devices(din)
            oo = sd.query_devices(dout if dout is not None else sd.default.device[1])
            ch = max(1, min(2, ii["max_input_channels"], oo["max_output_channels"]))
            extra = None
            if "WASAPI" in sd.query_hostapis()[ii["hostapi"]]["name"]:
                es = sd.WasapiSettings(auto_convert=True)
                extra = (es, es)
            vol, nivel, rec = self.vol, self.nivel, self.rec_q

            def cb(indata, outdata, frames, t, status):
                outdata[:] = indata[:, :ch] * vol[0]
                nivel[0] = float(np.abs(indata).max())
                q = rec[0]
                if q is not None:
                    try:
                        q.put_nowait(indata[:, :ch].copy())
                    except Exception:
                        pass

            sr = int(ii["default_samplerate"])
            self.audio = sd.Stream(device=(din, dout), samplerate=sr, channels=ch,
                                   dtype="float32", latency="low", callback=cb, extra_settings=extra)
            self.audio.start()
            self.audio_info = (sr, ch)
        except Exception as e:
            self.audio = None
            self.msg = f"El video funciona, pero no se pudo abrir el audio:\n{e}"

    def parar_audio(self):
        if self.audio:
            try:
                self.audio.stop()
                self.audio.close()
            except Exception:
                pass
            self.audio = None
        self.nivel[0] = 0.0

    # ---- grabacion
    def corto(self, ruta, n=46):
        return ruta if len(ruta) <= n else "…" + ruta[-(n - 1):]

    def asegurar_dir(self, k):
        d = self.dirs[k]
        os.makedirs(d, exist_ok=True)
        return d

    def abrir_carpeta(self, k="grab"):
        try:
            os.startfile(self.asegurar_dir(k))
        except Exception as e:
            messagebox.showwarning("Carpeta", f"No se pudo abrir la carpeta:\n{e}")

    def elegir_carpeta(self, k):
        titulo = "Elige dónde guardar las grabaciones" if k == "grab" else "Elige dónde guardar las capturas"
        actual = self.dirs[k]
        inicio = actual if os.path.isdir(actual) else os.path.dirname(actual) if os.path.isdir(
            os.path.dirname(actual)) else BASE
        d = filedialog.askdirectory(parent=self.root, title=titulo, initialdir=inicio)
        if d:
            d = os.path.normpath(d)
            self.dirs[k] = d
            self.lbl_dir[k].configure(text=self.corto(d))
            self.guardar()
            self.status.configure(text=f"{'Grabaciones' if k == 'grab' else 'Capturas'} se guardarán en:\n{d}")

    def alternar_grabacion(self):
        self.parar_grabacion() if self.grabador else self.iniciar_grabacion()
        self.root.focus_set()

    def iniciar_grabacion(self):
        if self.finalizando:
            self.aviso("Espera, se está guardando la grabación anterior…")
            return
        if not self.running or self.ultimo is None:
            self.aviso("Primero pulsa Iniciar para ver el video")
            return
        h, w = self.ultimo.shape[:2]
        fps = RESOLUCIONES[max(0, self.cb["res"].current())][3]
        audio = self.audio_info if self.audio else None
        try:
            ruta = os.path.join(self.asegurar_dir("grab"), time.strftime("grabacion_%Y%m%d_%H%M%S.mp4"))
            self.grabador = Grabador(ruta, w, h, fps, lambda: self.ultimo, audio)
        except Exception as e:
            self.grabador = None
            messagebox.showerror("No se pudo grabar", str(e))
            return
        self.rec_q[0] = self.grabador.audio_q
        self.t_rec = time.time()
        self.btn_rec.configure(text="⏹  Detener grabación", style="Stop.TButton")
        self.aviso("Grabando…" if audio and FFMPEG else "Grabando… (sin audio)")

    def parar_grabacion(self):
        g, self.grabador = self.grabador, None
        self.rec_q[0] = None
        self.btn_rec.configure(text="⏺  Grabar  (R)", style="TButton")
        if g is None:
            return
        self.finalizando = True
        self.status.configure(text="Guardando grabación…")

        def fin():
            try:
                ruta = g.detener()
                self.status_txt = f"Grabación guardada en:\n{ruta}"
            except Exception as e:
                self.status_txt = f"Error al guardar la grabación: {e}"
            self.finalizando = False

        self.fin_hilo = threading.Thread(target=fin, daemon=True)
        self.fin_hilo.start()

    def actualizar_rec(self):
        if self.grabador:
            t = int(time.time() - self.t_rec)
            txt = f"●  REC  {t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else \
                  f"●  REC  {t // 60:02d}:{t % 60:02d}"
            self.canvas.itemconfig(self.rec_id, text=txt)
            x1, y1, x2, y2 = self.canvas.bbox(self.rec_id)
            self.canvas.coords(self.rec_bg, x1 - 10, y1 - 5, x2 + 10, y2 + 5)
            self.canvas.itemconfig(self.rec_bg, state="normal")
            self.canvas.tag_raise(self.rec_bg)
            self.canvas.tag_raise(self.rec_id)
        else:
            self.canvas.itemconfig(self.rec_id, text="")
            self.canvas.itemconfig(self.rec_bg, state="hidden")

    # ---- inicio / parada
    def alternar(self):
        self.detener() if self.running else self.iniciar()

    def iniciar(self):
        if not self.cams:
            messagebox.showwarning("Sin capturadora", "No se encontró ninguna capturadora de video.")
            return
        ci = self.cb["video"].current()
        _, w, h, fps = RESOLUCIONES[max(0, self.cb["res"].current())]
        self.msg = ""
        self.running = True
        self.thread = threading.Thread(target=self.bucle_video, args=(max(ci, 0), w, h, fps), daemon=True)
        self.thread.start()
        self.iniciar_audio()
        self.btn_go.configure(text="■  Detener", style="Stop.TButton")
        self.canvas.itemconfig(self.txt_id, text="Abriendo capturadora…")
        self.guardar()

    def detener(self):
        if self.grabador:
            self.parar_grabacion()
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.parar_audio()
        self.latest = self.pintado = self.photo = None
        self.canvas.itemconfig(self.img_id, image="")
        self.canvas.itemconfig(self.txt_id, text="Elige tu capturadora y pulsa  ▶ Iniciar")
        self.btn_go.configure(text="▶  Iniciar", style="Go.TButton")
        self.status.configure(text="Detenido.")
        if self.fs:
            self.pantalla_completa(False)
        if self.msg:
            messagebox.showwarning("Aviso", self.msg)
            self.msg = ""

    def bucle_video(self, idx, w, h, fps):
        """Hilo: toma el cuadro, aplica el color y lo escala al tamano del visor."""
        cam = Camara(idx, w, h, fps)
        if not cam.ok:
            self.msg = "No se pudo abrir la capturadora.\n¿Está en uso por OBS u otro programa?"
            self.running = False
            return
        mostrado, cache = -1, {}
        while self.running:
            if cam.frame is not None and cam.n != mostrado:
                mostrado = cam.n
                f = procesar(cam.frame, dict(self.color), cache)
                self.ultimo = f
                cw, ch = self.canvas_size
                if cw > 10 and ch > 10:
                    fh, fw = f.shape[:2]
                    esc = min(cw / fw, ch / fh)
                    nw, nh = max(2, int(fw * esc)), max(2, int(fh * esc))
                    if (nw, nh) != (fw, fh):
                        f = cv2.resize(f, (nw, nh), interpolation=cv2.INTER_LINEAR)
                    self.latest = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
            else:
                time.sleep(0.002)
        self.running = False
        cam.cerrar()

    # ---- bucles de la interfaz
    def tick(self):
        lat = self.latest
        if self.running and lat is not None and lat is not self.pintado:
            self.pintado = lat
            h, w = lat.shape[:2]
            img = Image.fromarray(lat)
            if self.photo is None or self.photo.width() != w or self.photo.height() != h:
                self.photo = ImageTk.PhotoImage(img)
                self.canvas.itemconfig(self.img_id, image=self.photo)
                self.canvas.itemconfig(self.txt_id, text="")
            else:
                self.photo.paste(img)
            self.contador += 1
        self.root.after(4, self.tick)

    def poll(self):
        if self.flag_reset:
            self.flag_reset = False
            self.restablecer()
        ahora = time.time()
        self.fps = self.contador / max(ahora - self.t_fps, 0.001)
        self.contador, self.t_fps = 0, ahora
        self.barra["value"] = min(100, self.nivel[0] * 140)
        if self.thread is not None and not self.thread.is_alive() and self.btn_go.cget("text").startswith("■"):
            self.detener()
        elif self.status_txt:
            self.status.configure(text=self.status_txt)
            self.status_txt = ""
        elif self.running and self.fps > 1 and not self.finalizando:
            self.status.configure(text=f"▶ Reproduciendo · {self.fps:.0f} fps")
        self.actualizar_rec()
        self.root.after(250, self.poll)

    def guardar(self):
        i, j = self.cb["ain"].current(), self.cb["aout"].current()
        self.cfg.update(
            cam=self.cb["video"].get(), ain=self.ains[i][1] if i >= 0 else "Sin audio",
            aout=self.aouts[j][1] if self.aouts and j >= 0 else "", res=max(0, self.cb["res"].current()),
            color=self.color, dir_grab=self.dirs["grab"], dir_cap=self.dirs["cap"],
            seccion=next((k for k, v in self.secciones.items() if v["abierta"]), None))
        guardar_cfg(self.cfg)

    def cerrar(self):
        self.guardar()
        if self.grabador:
            g, self.grabador = self.grabador, None
            self.rec_q[0] = None
            try:
                g.detener()
            except Exception:
                pass
        if self.fin_hilo and self.fin_hilo.is_alive():
            self.fin_hilo.join(timeout=120)
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.parar_audio()
        self.root.destroy()


if __name__ == "__main__":
    App().root.mainloop()
