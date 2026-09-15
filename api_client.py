"""
api_client.py — HTTP-клиенты для тонкого десктоп-приложения.

Десктоп больше не подключается к PostgreSQL/Ollama/Zabbix/Oxidized
напрямую — всё это делает сервер (api_server.py + realtime_service.py +
job_worker.py). Классы здесь намеренно повторяют публичный интерфейс
db.IncidentRepository / db.ConfigDiffRepository (те же имена и сигнатуры
методов, что использует gui.py), чтобы переключение клиента на API не
требовало переписывать сами вкладки — только то, откуда репозитории берутся
(main.py) и то, что раньше было прямым вызовом Ollama/Oxidized, а теперь
становится job-запросом (см. ApiJobsClient и JobPollWorker).

Долгие LLM-операции (аудит конфига, сравнение с RAG/без и т.п.) сюда не
входят как отдельные методы — вместо этого gui.py создаёт задание через
ApiJobsClient.create_job(...) и опрашивает его статус, как раньше опрашивал
поток анализа. Ровно то же самое разделение, что и на сервере между
api_server.py (быстро отвечает) и job_worker.py (выполняет долго).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from models import Incident, Severity


class ApiError(Exception):
    """Ошибка при обращении к API — текст уже подготовлен для показа
    пользователю (взят из detail ответа сервера, когда он есть)."""


class AuthError(ApiError):
    """401/403 — нужно заново войти в систему или не хватает прав."""


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _incident_from_dict(d: dict) -> Incident:
    return Incident(
        id=d["id"], host=d["host"], problem_name=d["problem_name"],
        severity=Severity(d["severity"]), timestamp=_parse_dt(d["timestamp"]),
        item_key=d.get("item_key") or "", last_value=d.get("last_value") or "",
        resolved_at=_parse_dt(d.get("resolved_at")), opened_at=_parse_dt(d.get("opened_at")),
        ai_summary=d.get("ai_summary") or "", ai_recommendation=d.get("ai_recommendation") or "",
        ai_analyzed=bool(d.get("ai_analyzed")), ai_verified=bool(d.get("ai_verified")),
        resolution=d.get("resolution") or "",
    )


class ApiSession:
    """Общая инфраструктура запроса: базовый URL, токен, единообразная
    обработка ошибок. Все *Client/*Repository-классы ниже держат одну такую
    сессию — токен, полученный при входе, сразу становится виден им всем."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()

    def request(self, method: str, path: str, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self._session.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout, **kwargs,
            )
        except requests.exceptions.ConnectionError as e:
            raise ApiError(f"Нет связи с сервером ({self.base_url}) — проверьте сеть/VPN.") from e
        except requests.exceptions.Timeout as e:
            raise ApiError("Сервер не отвечает (таймаут запроса).") from e

        if resp.status_code in (401, 403):
            detail = _extract_detail(resp)
            raise AuthError(detail or "Нужен вход в систему")
        if not resp.ok:
            raise ApiError(_extract_detail(resp) or f"Ошибка сервера ({resp.status_code})")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self.request("PUT", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self.request("DELETE", path, **kwargs)


def _extract_detail(resp: requests.Response) -> str | None:
    try:
        data = resp.json()
        return data.get("detail")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

class ApiAuthClient:
    def __init__(self, session: ApiSession):
        self.session = session

    def login(self, username: str, password: str, remember_me: bool = False) -> dict:
        """Возвращает {"token", "username", "role", "expires_at"} и сразу
        прописывает токен в сессии — последующие вызовы уже авторизованы."""
        result = self.session.post(
            "/auth/login", json={"username": username, "password": password, "remember_me": remember_me},
        )
        self.session.token = result["token"]
        return result

    def logout(self) -> None:
        try:
            self.session.post("/auth/logout")
        finally:
            self.session.token = None

    def me(self) -> dict:
        return self.session.get("/auth/me")


# ---------------------------------------------------------------------------
# Инциденты — тот же публичный интерфейс, что у db.IncidentRepository,
# урезанный до того, что реально вызывает gui.py напрямую (без записей,
# которые теперь делает realtime_service.py на сервере).
# ---------------------------------------------------------------------------

class ApiIncidentRepository:
    def __init__(self, session: ApiSession):
        self.session = session

    def get_history(self, limit: int = 200) -> list[Incident]:
        return [_incident_from_dict(d) for d in self.session.get(f"/incidents?limit={limit}")]

    def get_verification_stats(self) -> dict:
        return self.session.get("/incidents/stats")

    def get_stats_by_host(self) -> dict:
        return self.session.get("/incidents/stats-by-host")

    def mark_opened(self, incident_id: str) -> None:
        self.session.post(f"/incidents/{incident_id}/open")

    def update_correction(self, incident_id: str, resolution: str) -> None:
        self.session.post(f"/incidents/{incident_id}/resolution", json={"resolution": resolution})

    def clear_all(self) -> int:
        return self.session.delete("/incidents")["deleted"]


class ApiConfigRepository:
    def __init__(self, session: ApiSession):
        self.session = session

    def get_history(self, limit: int = 100, review_type: str | None = None) -> list[dict]:
        path = f"/configs?limit={limit}"
        if review_type:
            path += f"&review_type={review_type}"
        return self.session.get(path)

    def update_correction(self, row_id: int, resolution: str) -> None:
        self.session.post(f"/configs/{row_id}/resolution", json={"resolution": resolution})

    def clear_all(self) -> int:
        return self.session.delete("/configs")["deleted"]


# ---------------------------------------------------------------------------
# Очередь заданий — замена прямым вызовам analyzer/oxidized_client/rag,
# которые gui.py делал в фоновых QThread-воркерах. Теперь воркер клиента
# создаёт задание и опрашивает его, вместо того чтобы считать сам.
# ---------------------------------------------------------------------------

JOB_POLL_INTERVAL_SECONDS = 2.0
JOB_TERMINAL_STATUSES = ("done", "error")


class ApiJobsClient:
    def __init__(self, session: ApiSession):
        self.session = session

    def create_job(self, job_type: str, payload: dict) -> int:
        return self.session.post("/jobs", json={"job_type": job_type, "payload": payload})["job_id"]

    def get_job(self, job_id: int) -> dict:
        return self.session.get(f"/jobs/{job_id}")

    def run_job_blocking(self, job_type: str, payload: dict, poll_interval: float = JOB_POLL_INTERVAL_SECONDS,
                          should_continue=lambda: True) -> dict:
        """Создаёт задание и ждёт его завершения, опрашивая статус — для
        использования ВНУТРИ фонового QThread (см. ChatWorker-подобные
        воркеры в gui.py), не в UI-потоке. should_continue позволяет
        прервать ожидание при остановке приложения/вкладки."""
        import time

        job_id = self.create_job(job_type, payload)
        while should_continue():
            job = self.get_job(job_id)
            if job["status"] in JOB_TERMINAL_STATUSES:
                if job["status"] == "error":
                    raise ApiError(job.get("error") or f"Задание #{job_id} завершилось с ошибкой")
                return job["result"]
            time.sleep(poll_interval)
        raise ApiError("Ожидание задания прервано")


# ---------------------------------------------------------------------------
# Чат
# ---------------------------------------------------------------------------

class ApiChatClient:
    def __init__(self, session: ApiSession):
        self.session = session

    def ask(self, question: str, history: list[tuple[str, str]]) -> str:
        result = self.session.post("/chat", json={"question": question, "history": history})
        return result["answer"]


# ---------------------------------------------------------------------------
# Статус серверных служб (для строки состояния в GUI)
# ---------------------------------------------------------------------------

class ApiServiceStatusClient:
    def __init__(self, session: ApiSession):
        self.session = session

    def read_all(self) -> dict:
        return self.session.get("/service/status")


# ---------------------------------------------------------------------------
# Администрирование
# ---------------------------------------------------------------------------

class ApiAdminClient:
    def __init__(self, session: ApiSession):
        self.session = session

    def get_service_config(self) -> dict:
        return self.session.get("/admin/service-config")

    def update_service_config(self, updates: dict) -> dict:
        return self.session.put("/admin/service-config", json=updates)

    def list_users(self) -> list[dict]:
        return self.session.get("/admin/users")

    def create_user(self, username: str, password: str, role: str = "engineer") -> int:
        return self.session.post("/admin/users", json={"username": username, "password": password, "role": role})["id"]

    def set_user_active(self, user_id: int, is_active: bool) -> None:
        self.session.put(f"/admin/users/{user_id}/active", json={"is_active": is_active})

    def reset_user_password(self, user_id: int, password: str) -> None:
        self.session.post(f"/admin/users/{user_id}/password", json={"password": password})
