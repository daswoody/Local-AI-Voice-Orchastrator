"""Hintergrund-Agenten (v1.29).

Ein Agenten-Aufruf (4.16) blockiert den Turn nicht mehr: Die Session
startet den Lauf neben dem Gespraech, das Haupt-LLM bekommt sofort eine
Bestaetigung ("laeuft im Hintergrund") und redet weiter. Ist der Agent
fertig, landet sein Ergebnis im Eingang der Session und fliesst in die
naechste Antwort ein - oder, wenn der Nutzer eine Weile nichts sagt, meldet
sich die Assistenz von selbst.

Hier liegen nur Job-Daten und die Texte fuer das Haupt-LLM; Start, Ende
und Zustellung haengen an der WebSocket-Session (routers/stream.py)."""

import asyncio
import json
import time
from dataclasses import dataclass, field

# Sehr lange Agenten-Antworten kappen - der System-Prompt soll nicht
# allein von einem Rechercheergebnis gefuellt werden.
_MAX_RESULT_CHARS = 8000


@dataclass
class AgentJob:
    job_id: str
    tool: str
    task: str
    # Chip des Start-Turns (Eintrag in dessen Tool-Liste) und die ganze
    # Liste: Wird der Agent fertig, nachdem der Turn gespeichert ist,
    # zieht die Session den Status in der Historie nach.
    chip: dict
    chips: list[dict]
    started_at: float = field(default_factory=time.monotonic)
    status: str = "background"
    result: str = ""
    message_id: int | None = None
    handle: asyncio.Task | None = None

    def record(self) -> dict:
        """Form fuer die Historie (messages.agent_results_json)."""
        return {"tool": self.tool, "task": self.task, "status": self.status,
                "result": self.result}


def started_result() -> str:
    return json.dumps({
        "ok": True,
        "status": "laeuft im Hintergrund",
        "hinweis": "Der Agent arbeitet jetzt im Hintergrund - das dauert einen "
                   "Moment. Sag dem Nutzer in einem kurzen Satz, dass du dich "
                   "darum kuemmerst, und fuehre das Gespraech ganz normal "
                   "weiter. Rufe den Agenten NICHT erneut auf und erfinde KEINE "
                   "Ergebnisse: Sie kommen automatisch zu dir, sobald sie da sind.",
    }, ensure_ascii=False)


def duplicate_result() -> str:
    return json.dumps({
        "ok": True,
        "status": "laeuft bereits",
        "hinweis": "Dieser Agent arbeitet schon an genau dieser Aufgabe. Starte "
                   "ihn nicht noch einmal - sein Ergebnis kommt automatisch.",
    }, ensure_ascii=False)


def context_prompt(running: list[AgentJob], arrived: list[AgentJob],
                   proactive: bool = False) -> str:
    """System-Prompt-Absatz: was gerade im Hintergrund laeuft und welche
    Ergebnisse seit der letzten Antwort eingetroffen sind. Leer = nichts."""
    parts: list[str] = []
    if running:
        now = time.monotonic()
        jobs = "; ".join(
            f"{job.tool} (Aufgabe: {job.task}, laeuft seit {int(now - job.started_at)} s)"
            for job in running
        )
        parts.append(
            f"Hintergrund-Agenten, die gerade fuer den Nutzer arbeiten: {jobs}. "
            "Ihr Ergebnis kommt automatisch zu dir, sobald es da ist. Rufe sie "
            "NICHT erneut auf und erfinde keine Ergebnisse; fragt der Nutzer "
            "danach, sag ihm, dass sie noch laufen."
        )
    if arrived:
        lines = []
        for job in arrived:
            result = job.result
            if len(result) > _MAX_RESULT_CHARS:
                result = result[:_MAX_RESULT_CHARS] + " …"
            outcome = "fehlgeschlagen: " if job.status == "error" else ""
            lines.append(f"- {job.tool} (Aufgabe: {job.task}): {outcome}{result}")
        if proactive:
            how = "Teile dem Nutzer das Ergebnis jetzt mit."
        else:
            how = (
                "Gehe zuerst kurz auf das ein, was der Nutzer gerade gesagt hat, "
                "und baue das Ergebnis dann natuerlich ein (etwa: 'Ich habe "
                "uebrigens gerade ... gefunden')."
            )
        parts.append(
            "Neue Ergebnisse von Hintergrund-Agenten (eingetroffen seit deiner "
            "letzten Antwort):\n" + "\n".join(lines) + f"\n{how} Umfangreiches "
            "(Listen, Optionen, Routen, Details) zeigst du als Karte (show_card) "
            "und verweist nur kurz darauf. Ist ein Agent fehlgeschlagen, sag das "
            "kurz und biete an, es anders zu versuchen."
        )
    return "\n\n".join(parts)


def history_note(record: dict) -> str:
    """Kurzvermerk fuer den Gespraechsverlauf: damit Folgefragen ("die
    zweite Route?") das Agenten-Ergebnis von eben kennen."""
    outcome = "fehlgeschlagen: " if record.get("status") == "error" else ""
    return (f"[Ergebnis vom Hintergrund-Agenten {record.get('tool')} "
            f"(Aufgabe: {record.get('task')}): {outcome}{record.get('result')}]")
