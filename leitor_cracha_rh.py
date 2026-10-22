"""
Leitor de Crachá - RH
Programa para Windows que encontra o leitor ESP32 + PN532 no USB sozinho,
abre a serial já configurada (115200) e mostra o UID lido e os logs.

Ninguém precisa escolher porta, placa ou velocidade.
Funciona com os firmwares que imprimem "UID: XXXXXXXX" ou "UID=XXXXXXXX".
"""

import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
from serial.tools import list_ports

try:
    import winsound
except ImportError:  # fora do Windows
    winsound = None

# ===================== CONFIGURAÇÃO ===================== #
BAUD = 115200
APP_TITULO = "Leitor de Crachá"

# Chips USB-serial usados nas placas ESP32 (VID, PID)
CHIPS_USB = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x1A86, 0x55D3): "CH343",
    (0x0403, 0x6001): "FTDI",
    (0x0403, 0x6015): "FTDI",
}
VID_ESPRESSIF = 0x303A  # ESP32-S2/S3/C3 com USB nativo

RE_UID = re.compile(r"UID\s*[:=]\s*([0-9A-Fa-f]{8,14})")
RE_SENSOR_OK = re.compile(r"PN532 (OK|detectado)|Sensor NFC voltou", re.IGNORECASE)
RE_SENSOR_ERRO = re.compile(r"PN532.*(n[ãa]o encontrado|parou de responder)", re.IGNORECASE)

# Cores
FUNDO = "#f2f2f5"
TOPO = "#2b2b3a"
ROXO = "#6a3fd1"
VERDE = "#14a06f"
AMARELO = "#f0a500"
VERMELHO = "#d64545"
TEXTO = "#222222"
CINZA = "#777777"


# ===================== SERIAL ===================== #
def encontrar_porta():
    """Retorna (porta, chip) do primeiro ESP32 encontrado, ou None."""
    for p in list_ports.comports():
        if p.vid is None:
            continue
        chip = CHIPS_USB.get((p.vid, p.pid))
        if chip:
            return p.device, chip
        if p.vid == VID_ESPRESSIF:
            return p.device, "USB nativo ESP32"
    return None


class LeitorSerial(threading.Thread):
    """Procura o leitor, conecta, lê linhas e reconecta sozinho se o cabo sair."""

    def __init__(self, fila):
        super().__init__(daemon=True)
        self.fila = fila
        self.parar = threading.Event()
        self.pedir_reset = threading.Event()

    def enviar(self, *msg):
        self.fila.put(msg)

    @staticmethod
    def resetar_esp(ser):
        # Pulso no EN pelo RTS (mesmo que o Arduino IDE faz), sem entrar em modo de gravação
        try:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.15)
            ser.rts = False
        except Exception:
            pass

    def run(self):
        while not self.parar.is_set():
            achado = encontrar_porta()
            if not achado:
                self.enviar("status", "procurando", None)
                self.parar.wait(1.5)
                continue

            porta, chip = achado
            ser = serial.Serial()
            ser.port = porta
            ser.baudrate = BAUD
            ser.timeout = 0.3
            ser.dtr = False
            ser.rts = False
            try:
                ser.open()
            except (serial.SerialException, OSError) as e:
                txt = str(e)
                if any(k in txt for k in ("denied", "PermissionError", "negado", "busy", "31")):
                    self.enviar("status", "ocupada", porta)
                else:
                    self.enviar("status", "erro", f"{porta}: {txt}")
                self.parar.wait(3)
                continue

            self.enviar("status", "conectado", (porta, chip))
            self.resetar_esp(ser)  # reinicia para receber o status do sensor desde o boot

            buf = b""
            try:
                while not self.parar.is_set():
                    if self.pedir_reset.is_set():
                        self.pedir_reset.clear()
                        self.enviar("evento", "Reiniciando o leitor...")
                        self.resetar_esp(ser)

                    dados = ser.read(ser.in_waiting or 1)
                    if dados:
                        buf += dados
                        while b"\n" in buf:
                            linha, buf = buf.split(b"\n", 1)
                            texto = linha.decode("utf-8", errors="replace").strip()
                            if texto:
                                self.enviar("linha", texto)
                        if len(buf) > 4096:
                            buf = b""
            except (serial.SerialException, OSError):
                pass
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

            if not self.parar.is_set():
                self.enviar("status", "desconectado", porta)
                self.parar.wait(1)


# ===================== INTERFACE ===================== #
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITULO)
        root.geometry("1000x680")
        root.minsize(840, 580)
        root.configure(bg=FUNDO)

        self.fila = queue.Queue()
        self.registro = []      # (hora, tag, texto) - todos os logs
        self.historico = []     # (datetime, uid)
        self.ultimo_status = None
        self.sensor_estado = None
        self.ultimo_uid = ""
        self._after_flash = None

        self.var_auto = tk.BooleanVar(value=True)
        self.var_som = tk.BooleanVar(value=True)
        self.var_tecnico = tk.BooleanVar(value=False)

        self._montar()

        self.leitor = LeitorSerial(self.fila)
        self.leitor.start()

        root.protocol("WM_DELETE_WINDOW", self.fechar)
        root.after(100, self.processar_fila)
        self.log("Programa iniciado. Procurando o leitor...", "evento")

    # ---------- montagem da tela ---------- #
    def _montar(self):
        fonte = ("Segoe UI", 10)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # Barra de status
        topo = tk.Frame(self.root, bg=TOPO, padx=16, pady=10)
        topo.pack(fill="x")
        self.dot = tk.Canvas(topo, width=16, height=16, bg=TOPO, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 14, 14, fill=AMARELO, outline="")
        self.dot.pack(side="left")
        self.lbl_status = tk.Label(topo, text="Iniciando...", bg=TOPO, fg="white",
                                   font=("Segoe UI", 11, "bold"))
        self.lbl_status.pack(side="left", padx=8)
        self.btn_reset = ttk.Button(topo, text="Reiniciar leitor", command=self.reiniciar_leitor,
                                    state="disabled")
        self.btn_reset.pack(side="right")
        self.lbl_sensor = tk.Label(topo, text="Sensor: —", bg=TOPO, fg="#cccccc", font=fonte)
        self.lbl_sensor.pack(side="right", padx=14)

        # Cartão do UID
        card = tk.Frame(self.root, bg="white", padx=20, pady=18,
                        highlightbackground="#dddddd", highlightthickness=1)
        card.pack(fill="x", padx=16, pady=(16, 8))
        tk.Label(card, text="ÚLTIMO CRACHÁ LIDO", bg="white", fg=CINZA,
                 font=("Segoe UI", 10, "bold")).pack()
        self.lbl_uid = tk.Label(card, text="— — — —", bg="white", fg=TEXTO,
                                font=("Consolas", 54, "bold"))
        self.lbl_uid.pack(pady=4)
        self.lbl_hora = tk.Label(card, text="Aproxime um crachá do leitor", bg="white",
                                 fg=CINZA, font=fonte)
        self.lbl_hora.pack()

        linha = tk.Frame(card, bg="white")
        linha.pack(pady=(12, 0))
        self.btn_copiar = tk.Button(linha, text="Copiar código", font=("Segoe UI", 11, "bold"),
                                    bg=ROXO, fg="white", activebackground="#5530b0",
                                    activeforeground="white", relief="flat", padx=20, pady=6,
                                    cursor="hand2", state="disabled", command=self.copiar_ultimo)
        self.btn_copiar.pack(side="left", padx=(0, 16))
        tk.Checkbutton(linha, text="Copiar automaticamente ao ler", variable=self.var_auto,
                       bg="white", activebackground="white", font=fonte).pack(side="left")
        tk.Checkbutton(linha, text="Bip ao ler", variable=self.var_som,
                       bg="white", activebackground="white", font=fonte).pack(side="left", padx=10)

        # Parte de baixo: histórico + registro
        baixo = tk.Frame(self.root, bg=FUNDO)
        baixo.pack(fill="both", expand=True, padx=16, pady=(8, 16))
        baixo.columnconfigure(0, weight=2)
        baixo.columnconfigure(1, weight=3)
        baixo.rowconfigure(0, weight=1)

        hist = ttk.LabelFrame(baixo, text=" Últimas leituras ", padding=8)
        hist.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.lst = tk.Listbox(hist, font=("Consolas", 12), activestyle="none", borderwidth=0,
                              highlightthickness=0, selectbackground=ROXO)
        self.lst.pack(fill="both", expand=True)
        self.lst.bind("<Double-Button-1>", lambda e: self.copiar_selecionado())
        bh = tk.Frame(hist)
        bh.pack(fill="x", pady=(8, 0))
        ttk.Button(bh, text="Copiar selecionado", command=self.copiar_selecionado).pack(side="left")
        ttk.Button(bh, text="Salvar lista", command=self.salvar_lista).pack(side="left", padx=6)
        ttk.Button(bh, text="Limpar", command=self.limpar_lista).pack(side="right")

        logs = ttk.LabelFrame(baixo, text=" Registro ", padding=8)
        logs.grid(row=0, column=1, sticky="nsew")
        caixa = tk.Frame(logs)
        caixa.pack(fill="both", expand=True)
        self.txt = tk.Text(caixa, font=("Consolas", 10), wrap="word", state="disabled",
                           bg="#fafafa", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(caixa, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("hora", foreground="#999999")
        self.txt.tag_configure("evento", foreground=TEXTO)
        self.txt.tag_configure("ok", foreground=VERDE, font=("Consolas", 10, "bold"))
        self.txt.tag_configure("erro", foreground=VERMELHO, font=("Consolas", 10, "bold"))
        self.txt.tag_configure("uid", foreground=ROXO, font=("Consolas", 10, "bold"))
        self.txt.tag_configure("esp", foreground="#8a8a8a")

        bl = tk.Frame(logs)
        bl.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(bl, text="Mostrar mensagens técnicas do leitor", variable=self.var_tecnico,
                        command=self.redesenhar_logs).pack(side="left")
        ttk.Button(bl, text="Salvar logs", command=self.salvar_logs).pack(side="right")
        ttk.Button(bl, text="Limpar", command=self.limpar_logs).pack(side="right", padx=6)

    # ---------- fila vinda da thread serial ---------- #
    def processar_fila(self):
        try:
            while True:
                msg = self.fila.get_nowait()
                tipo = msg[0]
                if tipo == "status":
                    self.tratar_status(msg[1], msg[2])
                elif tipo == "linha":
                    self.tratar_linha(msg[1])
                elif tipo == "evento":
                    self.log(msg[1], "evento")
        except queue.Empty:
            pass
        self.root.after(100, self.processar_fila)

    def tratar_status(self, estado, dados):
        chave = (estado, str(dados))
        if chave == self.ultimo_status:
            return
        self.ultimo_status = chave

        if estado == "conectado":
            porta, chip = dados
            cor, texto = VERDE, f"Leitor conectado ({porta})"
            self.log(f"Leitor conectado na porta {porta} ({chip}).", "ok")
            self.btn_reset.config(state="normal")
            self.set_sensor(None, "verificando...")
        else:
            self.btn_reset.config(state="disabled")
            self.set_sensor(None, "—")
            if estado == "procurando":
                cor, texto = AMARELO, "Procurando o leitor... conecte o cabo USB"
                self.log("Leitor não encontrado. Conecte o cabo USB do leitor no computador.", "evento")
            elif estado == "ocupada":
                cor, texto = VERMELHO, f"Porta {dados} em uso por outro programa"
                self.log(f"A porta {dados} está sendo usada por outro programa. "
                         "Feche o Arduino IDE, monitor serial ou outra janela deste programa.", "erro")
            elif estado == "desconectado":
                cor, texto = VERMELHO, "Leitor desconectado"
                self.log("Leitor desconectado. Verifique o cabo USB.", "erro")
            else:
                cor, texto = VERMELHO, "Erro ao abrir a porta"
                self.log(f"Erro ao abrir a porta: {dados}", "erro")

        self.dot.itemconfig(self.dot_id, fill=cor)
        self.lbl_status.config(text=texto)

    def tratar_linha(self, texto):
        self.log(texto, "esp")
        m = RE_UID.search(texto)
        if m:
            self.nova_leitura(m.group(1).upper())
        elif RE_SENSOR_ERRO.search(texto):
            self.set_sensor(False)
        elif RE_SENSOR_OK.search(texto):
            self.set_sensor(True)

    def set_sensor(self, ok, texto=None):
        if ok is None:
            self.sensor_estado = None
            self.lbl_sensor.config(text=f"Sensor: {texto}", fg="#cccccc")
            return
        if ok == self.sensor_estado:
            return
        self.sensor_estado = ok
        if ok:
            self.lbl_sensor.config(text="Sensor: funcionando", fg="#5be0a8")
            self.log("Sensor NFC funcionando.", "ok")
        else:
            self.lbl_sensor.config(text="Sensor: NÃO ENCONTRADO", fg="#ff8a8a")
            self.log("Sensor NFC não encontrado. Verifique os fios do leitor.", "erro")

    # ---------- leitura de crachá ---------- #
    def nova_leitura(self, uid):
        agora = datetime.now()
        self.ultimo_uid = uid
        self.historico.insert(0, (agora, uid))
        self.lst.insert(0, f"{agora:%H:%M:%S}   {uid}")
        if len(self.historico) > 200:
            self.historico.pop()
            self.lst.delete("end")

        self.lbl_uid.config(text=uid, fg=VERDE)
        if self._after_flash:
            self.root.after_cancel(self._after_flash)
        self._after_flash = self.root.after(1200, lambda: self.lbl_uid.config(fg=TEXTO))
        self.lbl_hora.config(text=f"Lido às {agora:%H:%M:%S} de {agora:%d/%m/%Y}")
        self.btn_copiar.config(state="normal")

        msg = f"Crachá lido: {uid}"
        if self.var_auto.get():
            self.copiar(uid)
            msg += " (copiado)"
        self.log(msg, "uid")
        self.set_sensor(True)

        if self.var_som.get() and winsound:
            winsound.MessageBeep(winsound.MB_OK)

    # ---------- área de transferência ---------- #
    def copiar(self, texto):
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)
        self.root.update()

    def copiar_ultimo(self):
        if not self.ultimo_uid:
            return
        self.copiar(self.ultimo_uid)
        self.btn_copiar.config(text="Copiado ✓", bg=VERDE)
        self.root.after(1200, lambda: self.btn_copiar.config(text="Copiar código", bg=ROXO))

    def copiar_selecionado(self):
        sel = self.lst.curselection()
        if not sel:
            messagebox.showinfo(APP_TITULO, "Selecione um crachá na lista.")
            return
        uid = self.historico[sel[0]][1]
        self.copiar(uid)
        self.log(f"Copiado: {uid}", "evento")

    # ---------- histórico ---------- #
    def salvar_lista(self):
        if not self.historico:
            messagebox.showinfo(APP_TITULO, "Nenhuma leitura para salvar.")
            return
        caminho = filedialog.asksaveasfilename(
            title="Salvar lista de crachás", defaultextension=".csv",
            initialdir=pasta_padrao(),
            initialfile=f"crachas_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("Planilha CSV", "*.csv")])
        if not caminho:
            return
        with open(caminho, "w", encoding="utf-8-sig") as f:
            f.write("data;hora;uid\n")
            for dt, uid in reversed(self.historico):
                f.write(f"{dt:%d/%m/%Y};{dt:%H:%M:%S};{uid}\n")
        self.log(f"Lista salva em {caminho}", "ok")

    def limpar_lista(self):
        self.historico.clear()
        self.lst.delete(0, "end")

    # ---------- logs ---------- #
    def log(self, texto, tag="evento"):
        hora = datetime.now().strftime("%H:%M:%S")
        self.registro.append((hora, tag, texto))
        if len(self.registro) > 5000:
            del self.registro[:1000]
        if tag == "esp" and not self.var_tecnico.get():
            return
        self._inserir(hora, tag, texto)

    def _inserir(self, hora, tag, texto):
        no_fim = self.txt.yview()[1] >= 0.99
        self.txt.config(state="normal")
        self.txt.insert("end", f"[{hora}] ", "hora")
        self.txt.insert("end", texto + "\n", tag)
        linhas = int(self.txt.index("end-1c").split(".")[0])
        if linhas > 1500:
            self.txt.delete("1.0", "301.0")
        self.txt.config(state="disabled")
        if no_fim:
            self.txt.see("end")

    def redesenhar_logs(self):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")
        mostrar = [r for r in self.registro if r[1] != "esp" or self.var_tecnico.get()]
        for hora, tag, texto in mostrar[-1500:]:
            self._inserir(hora, tag, texto)
        self.txt.see("end")

    def limpar_logs(self):
        self.registro.clear()
        self.redesenhar_logs()

    def salvar_logs(self):
        caminho = filedialog.asksaveasfilename(
            title="Salvar logs", defaultextension=".txt", initialdir=pasta_padrao(),
            initialfile=f"logs_leitor_{datetime.now():%Y%m%d_%H%M%S}.txt",
            filetypes=[("Texto", "*.txt")])
        if not caminho:
            return
        with open(caminho, "w", encoding="utf-8") as f:
            for hora, tag, texto in self.registro:
                origem = "ESP" if tag == "esp" else "APP"
                f.write(f"[{hora}] [{origem}] {texto}\n")
        self.log(f"Logs salvos em {caminho}", "ok")

    # ---------- outros ---------- #
    def reiniciar_leitor(self):
        self.set_sensor(None, "verificando...")
        self.leitor.pedir_reset.set()

    def fechar(self):
        self.leitor.parar.set()
        self.root.destroy()


def pasta_padrao():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    return desktop if os.path.isdir(desktop) else os.path.expanduser("~")


def main():
    if sys.platform == "win32":
        try:  # texto nítido em telas com escala (125%, 150%...)
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
