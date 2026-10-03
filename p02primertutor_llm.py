"""Tutor de Unity con Ollama, historial local y persistencia opcional en MongoDB."""

import importlib
import json
import os
import queue
import threading
import tkinter as tk
import uuid
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog
from typing import Any, Dict, List, Optional


MODELO_INICIAL = os.environ.get("UNITY_TUTOR_MODEL", "llama3.2")
BASE_URL_MONGO = os.environ.get("UNITY_TUTOR_MONGODB_URI", "")
BASE_DATOS_MONGO = os.environ.get("UNITY_TUTOR_MONGODB_DATABASE", "tutor_unity")
COLORES = {
    "fondo": "#0B1120",
    "panel": "#101827",
    "superficie": "#151F32",
    "burbuja": "#1B2940",
    "acento": "#8B78FF",
    "acento_hover": "#A293FF",
    "texto": "#F3F5FC",
    "texto_suave": "#9AA8BE",
    "verde": "#62D6B0",
    "rojo": "#FF8B9A",
    "amarillo": "#F7C96B",
}

MENSAJE_SISTEMA = """
Eres Unity Mentor, un tutor paciente y experto en desarrollo de videojuegos con
Unity y C#. Enseñas en español a estudiantes principiantes e intermedios.
Explica paso a paso, usa ejemplos pequeños de C# y aclara dónde se coloca cada
script en Unity. Ayuda con GameObjects, componentes, escenas, prefabs, física,
UI, animaciones, input, cámaras y errores comunes. No inventes nombres de APIs:
si depende de la versión de Unity, pregunta la versión o señala esa dependencia.
No entregues solo código: explica cómo probarlo en el editor y qué resultado
debería observarse. Mantén un tono amable, práctico y motivador.
"""

try:
    ollama = importlib.import_module("ollama")
except ModuleNotFoundError as error:
    if error.name != "ollama":
        raise
    ollama = None

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None

    class PyMongoError(Exception):
        pass


class MongoHistory:
    """Acceso acotado a documentos de conversación, no almacena credenciales."""

    def __init__(self, uri: str, database: str):
        if MongoClient is None:
            raise RuntimeError("Falta pymongo. Instala dependencias con: py -m pip install -r requirements.txt")
        if not uri.strip():
            raise ValueError("Captura la URI de MongoDB.")
        if not database.strip():
            raise ValueError("Escribe el nombre de la base de datos.")
        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=3500)
            self.client.admin.command("ping")
            self.collection = self.client[database.strip()]["conversaciones"]
            self.collection.create_index("updated_at")
        except PyMongoError as error:
            if hasattr(self, "client"):
                self.client.close()
            raise RuntimeError(f"No se pudo conectar con MongoDB: {error}") from error

    def create(self, model: str) -> Dict[str, Any]:
        now = datetime.now()
        document = {
            "title": "Nueva conversación",
            "preview": "",
            "model": model,
            "messages": [],
            "summary": "",
            "created_at": now,
            "updated_at": now,
        }
        result = self.collection.insert_one(document)
        document["_id"] = result.inserted_id
        return document

    def list(self) -> List[Dict[str, Any]]:
        return list(self.collection.find({}, {"messages": 0}).sort("updated_at", -1).limit(300))

    def get(self, conversation_id: str) -> Dict[str, Any]:
        from bson import ObjectId

        document = self.collection.find_one({"_id": ObjectId(conversation_id)})
        if document is None:
            raise LookupError("No se encontró esa conversación en MongoDB.")
        return document

    def append_message(self, conversation_id: str, message: Dict[str, Any]) -> None:
        from bson import ObjectId

        result = self.collection.update_one(
            {"_id": ObjectId(conversation_id)},
            {"$push": {"messages": message}, "$set": {"updated_at": datetime.now()}},
        )
        if result.matched_count == 0:
            raise LookupError("La conversación ya no existe en MongoDB.")

    def update(self, conversation_id: str, changes: Dict[str, Any]) -> None:
        from bson import ObjectId

        changes = dict(changes)
        changes["updated_at"] = datetime.now()
        result = self.collection.update_one(
            {"_id": ObjectId(conversation_id)}, {"$set": changes}
        )
        if result.matched_count == 0:
            raise LookupError("La conversación ya no existe en MongoDB.")

    def delete(self, conversation_id: str) -> bool:
        from bson import ObjectId

        result = self.collection.delete_one({"_id": ObjectId(conversation_id)})
        return result.deleted_count == 1

    def close(self) -> None:
        self.client.close()


class TutorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Unity Mentor · Tutor inteligente")
        self.root.geometry("1180x790")
        self.root.minsize(900, 650)
        self.root.configure(bg=COLORES["fondo"])

        self.model = MODELO_INICIAL
        self.history: Optional[MongoHistory] = None
        self.connection_mode = "Solo esta sesión"
        self.sessions: List[Dict[str, Any]] = []
        self.pending_sessions: Dict[str, Dict[str, Any]] = {}
        self.active_id: Optional[str] = None
        self.messages: List[Dict[str, str]] = []
        self.busy = False
        self.busy_operation: Optional[str] = None
        self.pending_summary = False
        self.session_ids: List[str] = []
        self.ui_events = queue.Queue()

        self._build_ui()
        self.root.after(80, self._process_ui_events)
        if BASE_URL_MONGO:
            try:
                self._connect_mongo(BASE_URL_MONGO, BASE_DATOS_MONGO)
            except (RuntimeError, ValueError) as error:
                self._set_status(f"MongoDB desconectado · {error}", "amarillo")
        self._new_conversation(show_welcome=True)
        self._refresh_sessions()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=COLORES["fondo"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        self.sidebar = tk.Frame(outer, bg=COLORES["panel"], width=275, padx=16, pady=17)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=COLORES["panel"])
        brand.pack(fill="x")
        tk.Label(brand, text="✦", font=("Segoe UI", 24, "bold"),
                 fg=COLORES["acento"], bg=COLORES["panel"]).pack(side="left", padx=(0, 9))
        tk.Label(brand, text="UNITY MENTOR", font=("Segoe UI", 13, "bold"),
                 fg=COLORES["texto"], bg=COLORES["panel"]).pack(side="left")
        tk.Label(
            self.sidebar,
            text="TU COMPAÑERO DE DESARROLLO",
            font=("Segoe UI", 8, "bold"),
            fg=COLORES["texto_suave"],
            bg=COLORES["panel"],
        ).pack(anchor="w", pady=(23, 9))

        self._sidebar_button("＋   Nueva conversación", self._new_conversation)
        self._sidebar_button("✧   Resumir conversación", self._summarize)
        self._sidebar_button("✎   Renombrar conversación", self._rename_conversation)
        self._sidebar_button("⇩   Exportar conversación", self._export_conversation)
        self._sidebar_button("⌫   Eliminar conversación", self._delete_conversation)
        self._sidebar_button("⚙   Conexiones y modelo", self._open_settings)

        search_row = tk.Frame(self.sidebar, bg=COLORES["panel"])
        search_row.pack(fill="x", pady=(20, 5))
        tk.Label(search_row, text="HISTORIAL", font=("Segoe UI", 8, "bold"),
                 fg=COLORES["texto_suave"], bg=COLORES["panel"]).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._render_sessions())
        tk.Entry(
            self.sidebar, textvariable=self.search_var, font=("Segoe UI", 9),
            bg=COLORES["superficie"], fg=COLORES["texto"], insertbackground=COLORES["texto"],
            relief="flat", highlightthickness=1, highlightbackground=COLORES["superficie"],
        ).pack(fill="x", ipady=7, pady=(2, 6))
        list_frame = tk.Frame(self.sidebar, bg=COLORES["panel"])
        list_frame.pack(fill="both", expand=True)
        self.session_list = tk.Listbox(
            list_frame, bg=COLORES["panel"], fg=COLORES["texto"],
            selectbackground=COLORES["acento"], selectforeground=COLORES["texto"],
            font=("Segoe UI", 9), relief="flat", borderwidth=0,
            activestyle="none", exportselection=False,
        )
        self.session_list.pack(side="left", fill="both", expand=True)
        list_scroll = tk.Scrollbar(list_frame, command=self.session_list.yview)
        list_scroll.pack(side="right", fill="y")
        self.session_list.config(yscrollcommand=list_scroll.set)
        self.session_list.bind("<<ListboxSelect>>", self._select_session)

        self.mongo_badge = tk.Label(
            self.sidebar,
            text="●  SOLO ESTA SESIÓN",
            font=("Segoe UI", 8, "bold"),
            fg=COLORES["amarillo"],
            bg=COLORES["superficie"],
            padx=10,
            pady=9,
            anchor="w",
        )
        self.mongo_badge.pack(fill="x", pady=(12, 0))

        main = tk.Frame(outer, bg=COLORES["fondo"])
        main.pack(side="left", fill="both", expand=True, padx=(16, 0))
        header = tk.Frame(main, bg=COLORES["fondo"])
        header.pack(fill="x", pady=(0, 14))
        title_block = tk.Frame(header, bg=COLORES["fondo"])
        title_block.pack(side="left")
        tk.Label(title_block, text="Buena noche.", font=("Segoe UI", 21, "bold"),
                 fg=COLORES["texto"], bg=COLORES["fondo"]).pack(anchor="w")
        tk.Label(title_block, text="Aprende Unity creando, probando y entendiendo.",
                 font=("Segoe UI", 10), fg=COLORES["texto_suave"],
                 bg=COLORES["fondo"]).pack(anchor="w", pady=(3, 0))
        self.status = tk.Label(
            header, text="●  LISTO", font=("Segoe UI", 8, "bold"),
            fg=COLORES["verde"], bg=COLORES["superficie"], padx=12, pady=8,
        )
        self.status.pack(side="right", anchor="center")

        self.active_title_label = tk.Label(
            main, text="Nueva conversación", font=("Segoe UI", 11, "bold"),
            fg=COLORES["texto"], bg=COLORES["fondo"], anchor="w",
        )
        self.active_title_label.pack(fill="x", pady=(0, 8))
        chat_card = tk.Frame(main, bg=COLORES["superficie"], padx=13, pady=13)
        chat_card.pack(fill="both", expand=True)
        self.chat_canvas = tk.Canvas(chat_card, bg=COLORES["superficie"],
                                     highlightthickness=0, borderwidth=0)
        chat_scroll = tk.Scrollbar(chat_card, orient="vertical", command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=chat_scroll.set)
        chat_scroll.pack(side="right", fill="y")
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_frame = tk.Frame(self.chat_canvas, bg=COLORES["superficie"])
        self.chat_window = self.chat_canvas.create_window((0, 0), window=self.chat_frame, anchor="nw")
        self.chat_frame.bind(
            "<Configure>",
            lambda _event: self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all")),
        )
        self.chat_canvas.bind(
            "<Configure>",
            lambda event: self.chat_canvas.itemconfigure(self.chat_window, width=event.width),
        )
        self.chat_canvas.bind_all(
            "<MouseWheel>",
            lambda event: self.chat_canvas.yview_scroll(int(-event.delta / 120), "units"),
        )

        composer = tk.Frame(main, bg=COLORES["panel"], padx=11, pady=10)
        composer.pack(fill="x", pady=(13, 0))
        self.input = tk.Text(
            composer, height=3, wrap="word", font=("Segoe UI", 10),
            bg=COLORES["panel"], fg=COLORES["texto"],
            insertbackground=COLORES["texto"], selectbackground=COLORES["acento"],
            relief="flat", borderwidth=0, padx=8, pady=6,
        )
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", self._send_shortcut)
        self.send_button = tk.Button(
            composer, text="Preguntar  ➤", command=self._send,
            font=("Segoe UI", 10, "bold"), fg=COLORES["texto"],
            bg=COLORES["acento"], activebackground=COLORES["acento_hover"],
            activeforeground=COLORES["texto"], relief="flat", borderwidth=0,
            padx=16, pady=12, cursor="hand2",
        )
        self.send_button.pack(side="right", padx=(9, 0), anchor="center")
        tk.Label(
            main, text="Ctrl + Enter para enviar  ·  El tutor responde sobre Unity y C#",
            font=("Segoe UI", 8), fg=COLORES["texto_suave"], bg=COLORES["fondo"],
        ).pack(anchor="e", pady=(6, 0))

    def _sidebar_button(self, text: str, command) -> None:
        tk.Button(
            self.sidebar, text=text, command=command, anchor="w",
            font=("Segoe UI", 9), fg=COLORES["texto"], bg=COLORES["panel"],
            activebackground=COLORES["superficie"], activeforeground=COLORES["texto"],
            relief="flat", borderwidth=0, padx=10, pady=9, cursor="hand2",
        ).pack(fill="x", pady=2)

    def _set_status(self, text: str, color: str = "verde") -> None:
        self.status.config(text=text, fg=COLORES[color])

    def _connect_mongo(self, uri: str, database: str) -> None:
        connection = MongoHistory(uri, database)
        old = self.history
        self.history = connection
        try:
            self._sync_pending_sessions()
            documents = connection.list()
        except (PyMongoError, RuntimeError, ValueError) as error:
            connection.close()
            self.history = old
            raise RuntimeError(f"No se pudo sincronizar el historial con MongoDB: {error}") from error
        self.sessions = documents
        self.connection_mode = f"MongoDB · {database}"
        self.mongo_badge.config(text=f"●  MONGODB CONECTADO · {database}",
                                fg=COLORES["verde"])
        if old is not None:
            old.close()
        self._render_sessions(select_active=True)
        self._set_status("●  MONGODB CONECTADO")

    def _sync_pending_sessions(self) -> None:
        if self.history is None:
            return
        for old_id, document in list(self.pending_sessions.items()):
            saved = {
                "title": document["title"],
                "preview": document.get("preview", ""),
                "model": document.get("model", self.model),
                "messages": document.get("messages", []),
                "summary": document.get("summary", ""),
                "created_at": document.get("created_at", datetime.now()),
                "updated_at": document.get("updated_at", datetime.now()),
            }
            if old_id.startswith("local-"):
                result = self.history.collection.insert_one(saved)
                new_id = str(result.inserted_id)
            else:
                from bson import ObjectId

                result = self.history.collection.update_one(
                    {"_id": ObjectId(old_id)}, {"$set": saved}
                )
                if result.matched_count:
                    new_id = old_id
                else:
                    result = self.history.collection.insert_one(saved)
                    new_id = str(result.inserted_id)
            if self.active_id == old_id:
                self.active_id = new_id
            del self.pending_sessions[old_id]

    def _new_conversation(self, show_welcome: bool = False) -> None:
        if self.busy:
            return
        self.messages = []
        self.summary = ""
        self.preview = ""
        try:
            if self.history is not None:
                document = self.history.create(self.model)
                self.active_id = str(document["_id"])
                self.active_title = document["title"]
                self.created_at = document["created_at"]
            else:
                self.active_id = f"local-{uuid.uuid4().hex}"
                self.active_title = "Nueva conversación"
                self.created_at = datetime.now()
                self.pending_sessions[self.active_id] = self._local_document()
        except (RuntimeError, ValueError, PyMongoError) as error:
            if self.history is not None:
                self.history.close()
            self.history = None
            self.active_id = f"local-{uuid.uuid4().hex}"
            self.active_title = "Nueva conversación"
            self.created_at = datetime.now()
            self.pending_sessions[self.active_id] = self._local_document()
            self.mongo_badge.config(text="●  MODO SOLO SESIÓN", fg=COLORES["amarillo"])
            self._set_status(f"MongoDB no disponible · {error}", "amarillo")
        self.active_title_label.config(text=self.active_title)
        self._clear_chat()
        if show_welcome or not self.messages:
            self._add_message(
                "assistant",
                "¡Qué gusto! Soy Unity Mentor. Puedo ayudarte con scripts de C#, "
                "GameObjects, escenas, físicas, UI, animación y errores. "
                "¿Qué estás creando? ",
            )
        self._refresh_sessions(select_active=True)
        self.input.focus_set()

    def _local_document(self) -> Dict[str, Any]:
        return {
            "_id": self.active_id,
            "title": getattr(self, "active_title", "Nueva conversación"),
            "preview": getattr(self, "preview", ""),
            "model": self.model,
            "messages": list(getattr(self, "messages", [])),
            "summary": getattr(self, "summary", ""),
            "created_at": self.created_at,
            "updated_at": datetime.now(),
        }

    def _current_document(self) -> Dict[str, Any]:
        return {
            "_id": self.active_id,
            "title": self.active_title,
            "preview": self.preview,
            "model": self.model,
            "messages": list(self.messages),
            "summary": self.summary,
            "created_at": getattr(self, "created_at", datetime.now()),
            "updated_at": datetime.now(),
        }

    def _persist_message(self, message: Dict[str, str]) -> None:
        message_with_time = dict(message)
        message_with_time["created_at"] = datetime.now()
        if self.history is not None and self.active_id and not self.active_id.startswith("local-"):
            self.history.append_message(self.active_id, message_with_time)
            return
        if self.active_id:
            document = self.pending_sessions.setdefault(self.active_id, self._current_document())
            document["messages"] = [
                {**item, "created_at": item.get("created_at", datetime.now())}
                for item in self.messages
            ]
            document["updated_at"] = datetime.now()

    def _send_shortcut(self, _event):
        self._send()
        return "break"

    def _send(self) -> None:
        if self.busy:
            return
        question = self.input.get("1.0", "end").strip()
        if not question:
            self._set_status("Escribe tu pregunta", "amarillo")
            return
        if ollama is None:
            self._set_status("Falta Ollama · instala requirements.txt", "rojo")
            messagebox.showerror(
                "Ollama no disponible",
                "No está instalado el paquete ollama. Ejecuta:\npy -m pip install -r requirements.txt",
            )
            return

        self.input.delete("1.0", "end")
        user_message = {"role": "user", "content": question}
        self.messages.append(user_message)
        self._add_message("user", question)
        self._set_busy(True, "Unity Mentor está pensando…", "chat")
        request_messages = [{"role": "system", "content": MENSAJE_SISTEMA}] + [
            {"role": message["role"], "content": message["content"]}
            for message in self.messages
        ]
        active_id = self.active_id
        threading.Thread(
            target=self._chat_worker,
            args=(active_id, user_message, request_messages, self.model),
            daemon=True,
        ).start()

    def _chat_worker(self, active_id, user_message, request_messages, model):
        database_error = None
        if self.history is not None and active_id and not active_id.startswith("local-"):
            try:
                self.history.append_message(
                    active_id, {**user_message, "created_at": datetime.now()}
                )
            except (PyMongoError, RuntimeError, ValueError, LookupError) as error:
                database_error = str(error)
        try:
            response = ollama.chat(model=model, messages=request_messages)
            content = response["message"]["content"]
            self.ui_events.put(
                (self._chat_complete, (active_id, user_message, content, None, database_error))
            )
        except Exception as error:
            self.ui_events.put(
                (self._chat_complete, (active_id, user_message, None, str(error), database_error))
            )

    def _process_ui_events(self) -> None:
        while True:
            try:
                callback, arguments = self.ui_events.get_nowait()
            except queue.Empty:
                break
            callback(*arguments)
        if self.root.winfo_exists():
            self.root.after(80, self._process_ui_events)

    def _chat_complete(self, active_id, user_message, content, error, database_error) -> None:
        if active_id != self.active_id:
            return
        if error:
            try:
                if database_error:
                    self._persist_message(user_message)
                self._maybe_title_from_first_question()
                self._save_current_metadata()
            except (RuntimeError, ValueError, PyMongoError, LookupError) as save_error:
                self._keep_pending_after_error(save_error)
            self._add_message(
                "assistant",
                "No pude obtener respuesta de Ollama. Revisa que Ollama esté activo "
                f"y que exista el modelo «{self.model}».\n\nDetalle: {error}",
            )
            if self.history is None and self.active_id in self.pending_sessions:
                self._set_status("Ollama sin respuesta · pregunta conservada en esta sesión", "rojo")
            else:
                self._set_status("No se pudo consultar Ollama", "rojo")
        else:
            assistant_message = {"role": "assistant", "content": content}
            self.messages.append(assistant_message)
            self._add_message("assistant", content)
            try:
                if database_error:
                    self._persist_message(user_message)
                self._persist_message(assistant_message)
                self._maybe_title_from_first_question()
                self._save_current_metadata()
                self._set_status("●  CONVERSACIÓN GUARDADA" if self.history else "●  SOLO ESTA SESIÓN",
                                 "verde" if self.history else "amarillo")
            except (RuntimeError, ValueError, PyMongoError, LookupError) as save_error:
                self._keep_pending_after_error(save_error)
        self._set_busy(False)
        self._refresh_sessions(select_active=True)
        if self.pending_summary:
            self.pending_summary = False
            self.root.after(0, self._summarize)

    def _maybe_title_from_first_question(self) -> None:
        user_messages = [message for message in self.messages if message["role"] == "user"]
        if len(user_messages) != 1:
            return
        title = " ".join(user_messages[0]["content"].split())[:48]
        self.preview = " ".join(user_messages[0]["content"].split())[:180]
        if self.active_title != "Nueva conversación":
            return
        if len(user_messages[0]["content"]) > 48:
            title += "…"
        self.active_title = title or "Conversación Unity"
        self.active_title_label.config(text=self.active_title)

    def _save_current_metadata(self) -> None:
        if not self.active_id:
            return
        if self.history is not None and not self.active_id.startswith("local-"):
            self.history.update(
                self.active_id,
                {"title": self.active_title, "preview": self.preview,
                 "model": self.model, "summary": self.summary},
            )
        else:
            document = self.pending_sessions.setdefault(self.active_id, self._current_document())
            document.update({
                "title": self.active_title, "model": self.model,
                "summary": self.summary, "preview": self.preview,
                "messages": list(self.messages),
                "updated_at": datetime.now(),
            })

    def _keep_pending_after_error(self, error) -> None:
        if self.active_id:
            self.pending_sessions[self.active_id] = self._current_document()
        if self.history is not None:
            self.history.close()
            self.history = None
        self._set_status(f"Historial aún no guardado en MongoDB · {error}", "rojo")
        self.mongo_badge.config(text="●  REVISAR CONEXIÓN · HISTORIAL EN SESIÓN",
                                fg=COLORES["rojo"])

    def _set_busy(
        self, busy: bool, status: str = "", operation: Optional[str] = None
    ) -> None:
        self.busy = busy
        self.busy_operation = operation if busy else None
        self.send_button.config(state="disabled" if busy else "normal")
        self.input.config(state="disabled" if busy else "normal")
        if status:
            self._set_status(status, "acento" if busy else "verde")
        self.root.update_idletasks()

    def _add_message(self, role: str, content: str) -> None:
        user = role == "user"
        row = tk.Frame(self.chat_frame, bg=COLORES["superficie"])
        row.pack(fill="x", pady=6)
        bubble_color = COLORES["acento"] if user else COLORES["burbuja"]
        bubble = tk.Frame(row, bg=bubble_color, padx=13, pady=10)
        bubble.pack(side="right" if user else "left", anchor="e" if user else "w",
                    padx=(65, 0) if user else (0, 65))
        tk.Label(
            bubble, text="TÚ" if user else "UNITY MENTOR",
            font=("Segoe UI", 8, "bold"),
            fg=COLORES["texto"] if user else COLORES["verde"], bg=bubble_color,
        ).pack(anchor="w", pady=(0, 4))
        tk.Label(
            bubble, text=content, font=("Segoe UI", 10), fg=COLORES["texto"],
            bg=bubble_color, justify="left", anchor="w", wraplength=660,
        ).pack(anchor="w")
        self.root.after(40, lambda: self.chat_canvas.yview_moveto(1.0))

    def _clear_chat(self) -> None:
        for child in self.chat_frame.winfo_children():
            child.destroy()

    def _refresh_sessions(self, select_active: bool = False) -> None:
        try:
            mongo_sessions = self.history.list() if self.history is not None else []
        except PyMongoError as error:
            self._keep_pending_after_error(error)
            mongo_sessions = []
        unique = {str(doc["_id"]): doc for doc in mongo_sessions}
        unique.update(self.pending_sessions)
        self.sessions = sorted(
            unique.values(), key=lambda doc: doc.get("updated_at", datetime.min), reverse=True
        )
        self._render_sessions(select_active=select_active)

    def _render_sessions(self, select_active: bool = False) -> None:
        query = self.search_var.get().strip().lower()
        self.session_list.delete(0, "end")
        self.session_ids = []
        active_index = None
        for document in self.sessions:
            title = str(document.get("title") or "Conversación Unity")
            messages = document.get("messages", [])
            preview = str(document.get("preview") or next(
                (m["content"] for m in messages if m.get("role") == "user"), ""
            ))
            if query and query not in title.lower() and query not in preview.lower():
                continue
            session_id = str(document["_id"])
            marker = "◉ " if session_id == self.active_id else "  "
            self.session_list.insert("end", marker + title[:31])
            self.session_ids.append(session_id)
            if session_id == self.active_id:
                active_index = len(self.session_ids) - 1
        if select_active and active_index is not None:
            self.session_list.selection_clear(0, "end")
            self.session_list.selection_set(active_index)

    def _select_session(self, _event) -> None:
        if self.busy:
            return
        selection = self.session_list.curselection()
        if not selection:
            return
        conversation_id = self.session_ids[selection[0]]
        if conversation_id == self.active_id:
            return
        try:
            if conversation_id.startswith("local-"):
                document = self.pending_sessions[conversation_id]
            elif self.history is not None:
                document = self.history.get(conversation_id)
            else:
                return
            self._load_conversation(document)
        except (LookupError, RuntimeError, ValueError, PyMongoError) as error:
            self._set_status(f"No se pudo cargar la conversación · {error}", "rojo")
            messagebox.showerror("Historial", str(error))

    def _load_conversation(self, document: Dict[str, Any]) -> None:
        self.active_id = str(document["_id"])
        self.active_title = document.get("title") or "Conversación Unity"
        self.preview = document.get("preview", "")
        self.created_at = document.get("created_at", datetime.now())
        self.summary = document.get("summary", "")
        self.messages = [
            {"role": item["role"], "content": item["content"]}
            for item in document.get("messages", [])
            if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)
        ]
        self.active_title_label.config(text=self.active_title)
        self._clear_chat()
        if not self.messages:
            self._add_message("assistant", "Esta conversación todavía no tiene mensajes.")
        else:
            for message in self.messages:
                self._add_message(message["role"], message["content"])
        if self.summary:
            self._add_message("assistant", f"RESUMEN ANTERIOR\n\n{self.summary}")
        self._set_status("●  HISTORIAL CARGADO", "verde")

    def _summarize(self) -> None:
        if self.busy:
            if self.busy_operation == "chat":
                self.pending_summary = True
                self._set_status(
                    "Resumen solicitado · se generará al terminar la respuesta",
                    "amarillo",
                )
            else:
                self._set_status("El resumen ya se está generando…", "amarillo")
            return
        if not [item for item in self.messages if item["role"] in ("user", "assistant")]:
            self._set_status("Todavía no hay historial para resumir", "amarillo")
            self._add_message(
                "assistant",
                "Aún no hay preguntas y respuestas para resumir. Envía una pregunta "
                "y vuelve a solicitar el resumen.",
            )
            return
        if ollama is None:
            error = "No está instalado el paquete Ollama. Ejecuta: py -m pip install -r requirements.txt"
            self._set_status(error, "rojo")
            messagebox.showerror("No se puede generar el resumen", error, parent=self.root)
            return
        transcript = list(self.messages)
        self._set_busy(True, "Preparando resumen…", "summary")
        threading.Thread(
            target=self._summary_worker, args=(self.active_id, transcript, self.model), daemon=True
        ).start()

    def _summary_worker(self, active_id, transcript, model):
        try:
            response = ollama.chat(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Escribe un resumen breve en español de la conversación sobre "
                            "Unity. Indica los temas consultados, lo explicado y los próximos "
                            "pasos útiles. Usa solo lo que aparece en el historial. No hagas "
                            "preguntas al usuario, no inicies una conversación nueva y no "
                            "inventes temas que no se trataron. Si el historial solo contiene "
                            "un saludo, dilo y recomienda qué pregunta de Unity podría hacer."
                        ),
                    },
                    *transcript,
                ],
            )
            self.ui_events.put(
                (self._summary_complete, (active_id, response["message"]["content"], None))
            )
        except Exception as error:
            self.ui_events.put(
                (self._summary_complete, (active_id, None, str(error)))
            )

    def _summary_complete(self, active_id, summary, error) -> None:
        if active_id != self.active_id:
            self._set_busy(False)
            return
        if error:
            self._set_status(f"No se pudo resumir · {error}", "rojo")
            self._add_message(
                "assistant",
                "No pude generar el resumen. Verifica que Ollama siga activo y que el "
                f"modelo «{self.model}» esté disponible.\n\nDetalle: {error}",
            )
            self._set_busy(False)
            return
        self.summary = summary
        try:
            self._save_current_metadata()
            self._add_message("assistant", f"RESUMEN DE TU HISTORIAL\n\n{summary}")
            self._set_status("Resumen guardado en la conversación", "verde")
        except (RuntimeError, ValueError, PyMongoError, LookupError) as save_error:
            self._keep_pending_after_error(save_error)
            self._add_message("assistant", f"RESUMEN DE TU HISTORIAL\n\n{summary}")
        self._set_busy(False)
        self._refresh_sessions(select_active=True)

    def _rename_conversation(self) -> None:
        if not self.active_id or self.busy:
            return
        title = simpledialog.askstring(
            "Renombrar conversación", "Nombre para esta conversación:",
            initialvalue=self.active_title, parent=self.root,
        )
        if title is None:
            return
        title = " ".join(title.split())
        if not title:
            self._set_status("El nombre no puede quedar vacío", "amarillo")
            return
        self.active_title = title[:80]
        self.active_title_label.config(text=self.active_title)
        try:
            self._save_current_metadata()
            self._refresh_sessions(select_active=True)
            self._set_status("Conversación renombrada", "verde")
        except (RuntimeError, ValueError, PyMongoError, LookupError) as error:
            self._keep_pending_after_error(error)

    def _export_conversation(self) -> None:
        if not self.active_id:
            self._set_status("No hay conversación seleccionada", "amarillo")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Exportar conversación",
            initialfile=f"{self.active_title[:40]}.json",
            defaultextension=".json",
            filetypes=[("Archivo JSON", "*.json"), ("Texto", "*.txt")],
        )
        if not path:
            return
        try:
            document = self._current_document()
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8") as output:
                    output.write(f'{document["title"]}\nTutor Unity · {document["model"]}\n\n')
                    for item in self.messages:
                        speaker = "Estudiante" if item["role"] == "user" else "Unity Mentor"
                        output.write(f"{speaker}:\n{item['content']}\n\n")
                    if self.summary:
                        output.write(f"Resumen:\n{self.summary}\n")
            else:
                with open(path, "w", encoding="utf-8") as output:
                    json.dump(document, output, ensure_ascii=False, indent=2, default=str)
            messagebox.showinfo("Exportación lista", f"Conversación exportada a:\n{path}")
        except OSError as error:
            messagebox.showerror("No se pudo exportar", str(error))

    def _delete_conversation(self) -> None:
        if not self.active_id or self.busy:
            return
        if not messagebox.askyesno(
            "Eliminar conversación",
            "¿Eliminar esta conversación y su historial guardado?",
            parent=self.root,
        ):
            return
        conversation_id = self.active_id
        try:
            if conversation_id.startswith("local-"):
                self.pending_sessions.pop(conversation_id, None)
            elif self.history is not None:
                if not self.history.delete(conversation_id):
                    raise LookupError("La conversación ya no existe en MongoDB.")
            self.active_id = None
            self._new_conversation()
            self._refresh_sessions()
        except (RuntimeError, ValueError, PyMongoError, LookupError) as error:
            messagebox.showerror("No se pudo eliminar", str(error))

    def _open_settings(self) -> None:
        if self.busy:
            self._set_status("Espera a que termine la respuesta antes de cambiar la conexión.", "amarillo")
            return
        window = tk.Toplevel(self.root)
        window.title("Conexiones y modelo")
        window.geometry("560x390")
        window.resizable(False, False)
        window.configure(bg=COLORES["fondo"])
        window.transient(self.root)
        window.grab_set()
        panel = tk.Frame(window, bg=COLORES["fondo"], padx=22, pady=18)
        panel.pack(fill="both", expand=True)
        tk.Label(panel, text="Configuración del tutor", font=("Segoe UI", 17, "bold"),
                 fg=COLORES["texto"], bg=COLORES["fondo"]).pack(anchor="w")
        tk.Label(panel, text="Conecta MongoDB para guardar sesiones e historial.",
                 font=("Segoe UI", 9), fg=COLORES["texto_suave"],
                 bg=COLORES["fondo"]).pack(anchor="w", pady=(3, 15))
        tk.Label(panel, text="URI de MongoDB (local o Atlas)", fg=COLORES["texto"],
                 bg=COLORES["fondo"]).pack(anchor="w")
        uri_var = tk.StringVar(value=BASE_URL_MONGO)
        uri_entry = tk.Entry(panel, textvariable=uri_var, show="•", width=68,
                             bg=COLORES["superficie"], fg=COLORES["texto"],
                             insertbackground=COLORES["texto"], relief="flat")
        uri_entry.pack(fill="x", ipady=8, pady=(4, 10))
        tk.Label(panel, text="Base de datos", fg=COLORES["texto"],
                 bg=COLORES["fondo"]).pack(anchor="w")
        database_var = tk.StringVar(value=BASE_DATOS_MONGO)
        tk.Entry(panel, textvariable=database_var, width=32, bg=COLORES["superficie"],
                 fg=COLORES["texto"], insertbackground=COLORES["texto"],
                 relief="flat").pack(anchor="w", ipady=7, pady=(4, 10))
        tk.Label(panel, text="Modelo Ollama (debe estar instalado, por ejemplo llama3.2)",
                 fg=COLORES["texto"], bg=COLORES["fondo"]).pack(anchor="w")
        model_var = tk.StringVar(value=self.model)
        tk.Entry(panel, textvariable=model_var, width=32, bg=COLORES["superficie"],
                 fg=COLORES["texto"], insertbackground=COLORES["texto"],
                 relief="flat").pack(anchor="w", ipady=7, pady=(4, 10))
        tk.Label(
            panel, text="La URI no se guarda en archivos. Para reconectar automáticamente, "
            "usa UNITY_TUTOR_MONGODB_URI como variable de entorno.",
            wraplength=510, justify="left", fg=COLORES["texto_suave"],
            bg=COLORES["fondo"],
        ).pack(anchor="w", pady=(2, 12))
        buttons = tk.Frame(panel, bg=COLORES["fondo"])
        buttons.pack(fill="x")

        def save_settings():
            global BASE_URL_MONGO, BASE_DATOS_MONGO
            new_model = model_var.get().strip()
            if not new_model:
                messagebox.showerror("Configuración", "Escribe el nombre de un modelo Ollama.")
                return
            self.model = new_model
            BASE_URL_MONGO = uri_var.get().strip()
            BASE_DATOS_MONGO = database_var.get().strip()
            os.environ["UNITY_TUTOR_MODEL"] = self.model
            if not BASE_URL_MONGO:
                self._set_status("MongoDB no configurado · historial temporal", "amarillo")
                window.destroy()
                return
            try:
                self._connect_mongo(BASE_URL_MONGO, BASE_DATOS_MONGO)
                window.destroy()
            except (RuntimeError, ValueError) as error:
                self._set_status(f"MongoDB: {error}", "rojo")
                messagebox.showerror("Conexión MongoDB", str(error), parent=window)

        tk.Button(
            buttons, text="Probar y guardar conexión", command=save_settings,
            bg=COLORES["acento"], fg=COLORES["texto"], relief="flat",
            padx=13, pady=9, cursor="hand2",
        ).pack(side="left")
        tk.Button(
            buttons, text="Cerrar", command=window.destroy,
            bg=COLORES["superficie"], fg=COLORES["texto"], relief="flat",
            padx=13, pady=9, cursor="hand2",
        ).pack(side="right")

    def _close(self) -> None:
        if self.history is not None:
            self.history.close()
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    TutorApp(window)
    window.mainloop()
