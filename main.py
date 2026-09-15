"""
main.py — точка входа тонкого десктоп-клиента NetAI Monitor.

Клиент больше не подключается ни к PostgreSQL, ни к Ollama, ни к Zabbix/
Oxidized напрямую — только к api_server.py по HTTP (см. api_client.py).
Вся логика входа (тихий автовход по запомненному токену из Диспетчера
учётных данных Windows, иначе диалог логина) — в gui.run_app().
"""
from gui import run_app


def main():
    run_app()


if __name__ == "__main__":
    main()
