"""
api_server.py — единственная точка входа для десктоп-клиентов.

Десктоп больше не подключается к PostgreSQL/Ollama/Zabbix напрямую — только
сюда, по HTTPS с токеном сессии. Этот процесс сам обращается к PostgreSQL
(localhost) и, для чата, к Ollama (localhost); опрос Zabbix и длинные
LLM-задания (аудит конфига, сравнение с RAG/без) выполняют ДВЕ отдельные
службы — realtime_service.py и job_worker.py — через общую БД (см. их
докстринги). API-служба ничего из этого сама не делает: если бы она сама
дожидалась 30-120-секундного LLM-вызова внутри обработчика запроса, это
занимало бы один из немногих потоков и тормозило бы ответы всем остальным
клиентам одновременно — поэтому такие операции только СТАВЯТСЯ в очередь
(POST /jobs), а результат клиент забирает отдельным запросом (GET /jobs/{id}).

Запуск вручную:
    python api_server.py
Установка службой — см. tools/install_service.ps1 и DEPLOYMENT.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth
from backend import Backend
from chat_query import build_context as build_chat_context
from llm_client import get_analyzer
from mock_oxidized_client import MockOxidizedClient
from models import Incident, Severity
from oxidized_client import OxidizedClient
from service_common import setup_logging, connect_db_with_retry
from service_config import load_service_config, save_service_config

log = logging.getLogger("api-server")

# Заполняется в startup-обработчике — простые module-level переменные вместо
# app.state, чтобы не тащить request через каждую сигнатуру эндпоинта.
backend: Backend | None = None
chat_analyzer = None
oxidized_client = None  # только для лёгкого чтения списка узлов, см. GET /configs/nodes


# ---------------------------------------------------------------------------
# JSON-сериализация
# ---------------------------------------------------------------------------

def _incident_to_dict(inc: Incident) -> dict:
    return {
        "id": inc.id, "host": inc.host, "problem_name": inc.problem_name,
        "severity": inc.severity.value, "timestamp": inc.timestamp.isoformat(),
        "item_key": inc.item_key, "last_value": inc.last_value,
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
        "opened_at": inc.opened_at.isoformat() if inc.opened_at else None,
        "ai_summary": inc.ai_summary, "ai_recommendation": inc.ai_recommendation,
        "ai_analyzed": inc.ai_analyzed, "ai_verified": inc.ai_verified, "resolution": inc.resolution,
    }


def _row_to_json(row: dict) -> dict:
    """Строки config_diffs (RealDictCursor) содержат datetime и embedding
    (вектор pgvector) — embedding клиенту не нужен и не JSON-сериализуем
    напрямую, datetime переводим в ISO-строку."""
    out = {}
    for k, v in row.items():
        if k == "embedding":
            continue
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Нужен вход в систему")
    token = authorization.removeprefix("Bearer ").strip()
    session = backend.sessions.get_valid(auth.hash_token(token))
    if session is None:
        raise HTTPException(401, "Сессия недействительна или истекла — войдите снова")
    if not session["is_active"]:
        raise HTTPException(403, "Учётная запись отключена администратором")
    return session  # содержит user_id, username, role


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Требуются права администратора")
    return user


# ---------------------------------------------------------------------------
# Модели запросов
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class ResolutionRequest(BaseModel):
    resolution: str


class ChatRequest(BaseModel):
    question: str
    history: list[tuple[str, str]] = []


class JobRequest(BaseModel):
    job_type: str
    payload: dict = {}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "engineer"


class SetActiveRequest(BaseModel):
    is_active: bool


class SetPasswordRequest(BaseModel):
    password: str


class ServiceConfigUpdate(BaseModel):
    zabbix_url: Optional[str] = None
    zabbix_user: Optional[str] = None
    zabbix_password: Optional[str] = None
    oxidized_url: Optional[str] = None
    ollama_host: Optional[str] = None
    ollama_model: Optional[str] = None
    embedding_model: Optional[str] = None
    use_synthetic_data: Optional[bool] = None
    poll_interval_seconds: Optional[int] = None


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global backend, chat_analyzer, oxidized_client
    setup_logging("api-server", "api_server.log")

    config = load_service_config()
    backend = connect_db_with_retry(config["postgres_dsn"], lambda: True, log)

    oxidized_client = (
        MockOxidizedClient(seed=42) if config["use_synthetic_data"]
        else OxidizedClient(base_url=config["oxidized_url"])
    )

    if backend.users.count() == 0:
        # Бутстрап первого администратора — иначе войти в систему не
        # получится вообще ни у кого. Пароль печатается в лог ОДИН раз;
        # администратор должен сменить его при первом входе.
        import secrets

        bootstrap_password = secrets.token_urlsafe(9)
        backend.users.create("admin", auth.hash_password(bootstrap_password), role="admin")
        log.warning(
            "Создана первая учётная запись администратора: логин 'admin', "
            "пароль '%s'. Смените пароль после первого входа — это "
            "сообщение больше не повторится.", bootstrap_password,
        )

    chat_analyzer = get_analyzer(config["ollama_host"], config["ollama_model"])
    log.info("API-служба запущена. Анализатор для чата: %s", type(chat_analyzer).__name__)

    yield

    log.info("API-служба остановлена.")


app = FastAPI(title="NetAI Monitor API", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Без этого необработанная ошибка внутри эндпоинта уходила бы клиенту
    # голым traceback'ом (FastAPI по умолчанию это делает только в debug) —
    # логируем на сервере, клиенту отдаём общий текст.
    log.exception("Необработанная ошибка на %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

@app.post("/auth/login")
def login(body: LoginRequest):
    user = backend.users.get_by_username(body.username)
    if user is None or not user["is_active"] or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Неверный логин или пароль")

    token = auth.generate_token()
    expires_at = auth.session_expiry(body.remember_me)
    backend.sessions.create(auth.hash_token(token), user["id"], expires_at, body.remember_me)
    backend.users.mark_login(user["id"])
    return {
        "token": token, "username": user["username"], "role": user["role"],
        "expires_at": expires_at.isoformat(),
    }


@app.post("/auth/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        backend.sessions.delete(auth.hash_token(token))
    return {"ok": True}


@app.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "role": user["role"]}


# ---------------------------------------------------------------------------
# Инциденты
# ---------------------------------------------------------------------------

@app.get("/incidents")
def list_incidents(limit: int = 200, user: dict = Depends(get_current_user)):
    return [_incident_to_dict(i) for i in backend.incidents.get_history(limit=limit)]


@app.get("/incidents/stats")
def incidents_stats(user: dict = Depends(get_current_user)):
    return backend.incidents.get_verification_stats()


@app.get("/incidents/stats-by-host")
def incidents_stats_by_host(user: dict = Depends(get_current_user)):
    return backend.incidents.get_stats_by_host()


@app.post("/incidents/{incident_id}/open")
def open_incident(incident_id: str, user: dict = Depends(get_current_user)):
    backend.incidents.mark_opened(incident_id)
    return {"ok": True}


@app.post("/incidents/{incident_id}/resolution")
def set_incident_resolution(incident_id: str, body: ResolutionRequest, user: dict = Depends(get_current_user)):
    if not body.resolution.strip():
        raise HTTPException(400, "Решение не может быть пустым")
    backend.incidents.update_correction(incident_id, body.resolution)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Конфигурации
# ---------------------------------------------------------------------------

@app.get("/configs")
def list_configs(limit: int = 100, review_type: Optional[str] = None, user: dict = Depends(get_current_user)):
    return [_row_to_json(r) for r in backend.configs.get_history(limit=limit, review_type=review_type)]


@app.get("/configs/nodes")
def list_config_nodes(user: dict = Depends(get_current_user)):
    """Список устройств от Oxidized (или мок-данных) — для выпадающего списка
    в ConfigsTab. Лёгкий синхронный вызов (как /chat), не требует очереди."""
    try:
        return oxidized_client.get_nodes()
    except Exception as e:
        raise HTTPException(502, f"Не удалось получить список устройств: {str(e)[:200]}")


@app.post("/configs/{config_id}/resolution")
def set_config_resolution(config_id: int, body: ResolutionRequest, user: dict = Depends(get_current_user)):
    if not body.resolution.strip():
        raise HTTPException(400, "Решение не может быть пустым")
    backend.configs.update_correction(config_id, body.resolution)
    return {"ok": True}


@app.delete("/incidents")
def clear_incidents(user: dict = Depends(require_admin)):
    # Разрушительно и необратимо (используется только в SyntheticDataTab для
    # сброса демо-данных перед повторной генерацией) — поэтому только admin.
    deleted = backend.incidents.clear_all()
    log.warning("Администратор %s удалил все инциденты (%d записей).", user["username"], deleted)
    return {"deleted": deleted}


@app.delete("/configs")
def clear_configs(user: dict = Depends(require_admin)):
    deleted = backend.configs.clear_all()
    log.warning("Администратор %s удалил все проверки конфигураций (%d записей).", user["username"], deleted)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Чат — единственная LLM-операция, которую сама API-служба выполняет
# синхронно (не через очередь): ответ обычно укладывается в разумное время,
# а пользователь и так смотрит на экран ожидания в чате.
# ---------------------------------------------------------------------------

@app.post("/chat")
def chat(body: ChatRequest, user: dict = Depends(get_current_user)):
    context = build_chat_context(body.question, backend.incidents, backend.configs)
    answer = chat_analyzer.answer_chat(body.question, context, history=body.history)
    return {"answer": answer}


# ---------------------------------------------------------------------------
# Очередь LLM-заданий — см. job_worker.py за типами job_type и форматами
# payload/result.
# ---------------------------------------------------------------------------

@app.post("/jobs")
def create_job(body: JobRequest, user: dict = Depends(get_current_user)):
    job_id = backend.jobs.enqueue(body.job_type, body.payload, created_by=user["user_id"])
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, user: dict = Depends(get_current_user)):
    job = backend.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Задание не найдено")
    return _row_to_json(job)


# ---------------------------------------------------------------------------
# Статус служб на сервере
# ---------------------------------------------------------------------------

@app.get("/service/status")
def service_status(user: dict = Depends(get_current_user)):
    rows = backend.service_status.read_all()
    return {
        "realtime": _row_to_json(next((r for r in rows if r["id"] == 1), None) or {}),
        "job_worker": _row_to_json(next((r for r in rows if r["id"] == 2), None) or {}),
    }


# ---------------------------------------------------------------------------
# Администрирование — только role='admin'
# ---------------------------------------------------------------------------

@app.get("/admin/service-config")
def get_service_config(user: dict = Depends(require_admin)):
    config = load_service_config()
    config["postgres_dsn"] = "•••" if config.get("postgres_dsn") else ""
    config["zabbix_password"] = "•••" if config.get("zabbix_password") else ""
    return config


@app.put("/admin/service-config")
def update_service_config(body: ServiceConfigUpdate, user: dict = Depends(require_admin)):
    current = load_service_config()
    updates = body.model_dump(exclude_unset=True)
    # Маскированные значения ("•••", см. GET выше) не должны затереть
    # реальный пароль — если поле пришло как маска, оставляем прежнее.
    for secret_field in ("zabbix_password",):
        if updates.get(secret_field) == "•••":
            updates.pop(secret_field)
    current.update(updates)
    save_service_config(current)
    log.info("Администратор %s изменил серверные настройки: %s", user["username"], list(updates.keys()))
    return {"ok": True, "note": "Изменения применятся после перезапуска служб на сервере."}


@app.get("/admin/users")
def list_users(user: dict = Depends(require_admin)):
    return backend.users.list_all()


@app.post("/admin/users")
def create_user(body: CreateUserRequest, user: dict = Depends(require_admin)):
    if body.role not in ("admin", "engineer"):
        raise HTTPException(400, "role должен быть 'admin' или 'engineer'")
    if len(body.password) < 8:
        raise HTTPException(400, "Пароль должен быть не короче 8 символов")
    if backend.users.get_by_username(body.username) is not None:
        raise HTTPException(409, "Пользователь с таким логином уже существует")
    new_id = backend.users.create(body.username, auth.hash_password(body.password), role=body.role)
    log.info("Администратор %s создал пользователя %s (%s)", user["username"], body.username, body.role)
    return {"id": new_id}


@app.put("/admin/users/{target_id}/active")
def set_user_active(target_id: int, body: SetActiveRequest, user: dict = Depends(require_admin)):
    backend.users.set_active(target_id, body.is_active)
    return {"ok": True}


@app.post("/admin/users/{target_id}/password")
def reset_user_password(target_id: int, body: SetPasswordRequest, user: dict = Depends(require_admin)):
    if len(body.password) < 8:
        raise HTTPException(400, "Пароль должен быть не короче 8 символов")
    backend.users.set_password(target_id, auth.hash_password(body.password))
    return {"ok": True}


def main() -> None:
    import uvicorn

    config = load_service_config()
    uvicorn.run(
        "api_server:app",
        host=config["api_host"], port=int(config["api_port"]),
        log_config=None,  # своё логирование (setup_logging в lifespan), не дублируем форматом uvicorn
    )


if __name__ == "__main__":
    main()
